import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path

from rfp_assistant import budget, postgres, store
from tests import fixtures

RATES = {"test-model": {"input": "1", "cached_input": "0.5", "output": "1"}}  # 1 micro-USD per token


class BudgetLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = postgres.Target(fixtures.database())
        budget.ensure_budget_row(self.db)
        budget.configure(self.db, "owner", project_start=date(2026, 9, 30), project_end=date(2026, 10, 28),
                         prior_use_micro=0, prior_use_evidence="test", rates=RATES, rate_version="t",
                         enable_paid=True)
        budget.set_paid_enabled(self.db, "owner", True, "test")
        self.owner = postgres.GatewayOwner(self.db)  # the paid gateway this ledger admits through
        with store.open_db(self.db) as conn:  # exactly 100 microdollars available
            conn.execute("UPDATE budget_settings SET cap_micro_usd = 100")
        self.request_id = self._request()

    def tearDown(self):
        self.owner.release()
        self.tmp.cleanup()

    def _request(self) -> str:
        import uuid

        rid = str(uuid.uuid4())
        with store.open_db(self.db) as conn:
            conn.execute("INSERT INTO requests(request_id, member_id, idempotency_key, input_hash, config_hash, "
                         "scope_json, status, created_at, updated_at) VALUES (?, 'm1', ?, 'h', 'c', '[]', 'running', "
                         "'t', 't')", (rid, rid))
        return rid

    def reserve(self, tokens=60):
        return budget.reserve(self.db, request_id=self.request_id, member_id="m1", stage="generation",
                              purpose="interactive", model="test-model", input_tokens=tokens, max_output_tokens=0,
                              count_method="test")

    def test_six_concurrent_reservations_admit_only_the_affordable_one(self):
        barrier = threading.Barrier(6)
        results = []

        def worker():
            barrier.wait()
            results.append(self.reserve(60))

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        admitted = [r for r in results if r["admitted"]]
        self.assertEqual(len(admitted), 1)
        self.assertTrue(all(r["reason"] == "cap_exhausted" for r in results if not r["admitted"]))
        with store.open_db(self.db) as conn:
            rows = conn.execute("SELECT attempt_id, state FROM attempts").fetchall()
        self.assertEqual([(r[0], r[1]) for r in rows], [(admitted[0]["attempt_id"], "reserved")])
        snap = budget.snapshot(self.db)
        self.assertEqual((snap.pending_micro_usd, snap.available_micro_usd), (60, 40))

    def test_unused_release_and_cheaper_settlement(self):
        r = self.reserve(60)
        self.assertTrue(budget.release(self.db, r["attempt_id"], "never dispatched"))
        self.assertEqual(budget.snapshot(self.db).available_micro_usd, 100)
        r = self.reserve(60)
        budget.mark_dispatching(self.db, r["attempt_id"])
        out = budget.settle(self.db, r["attempt_id"], {"prompt_tokens": 30, "completion_tokens": 0}, "resp-1")
        self.assertEqual(out["settled_micro_usd"], 30)
        snap = budget.snapshot(self.db)
        self.assertEqual((snap.spent_micro_usd, snap.pending_micro_usd, snap.available_micro_usd), (30, 0, 70))

    def test_duplicate_settlement_charges_once_and_adjustments_are_idempotent(self):
        r = self.reserve(60)
        budget.mark_dispatching(self.db, r["attempt_id"])
        budget.settle(self.db, r["attempt_id"], {"prompt_tokens": 40, "completion_tokens": 5}, "resp-2")
        again = budget.settle(self.db, r["attempt_id"], {"prompt_tokens": 40, "completion_tokens": 5}, "resp-2")
        self.assertTrue(again["duplicate"])
        self.assertEqual(budget.snapshot(self.db).spent_micro_usd, 45)
        self.assertTrue(budget.add_adjustment(self.db, "owner", "export-2026-10-01", 7, "export", "manual"))
        self.assertFalse(budget.add_adjustment(self.db, "owner", "export-2026-10-01", 7, "export", "manual"))
        with self.assertRaises(budget.BudgetError):
            budget.add_adjustment(self.db, "owner", "export-2026-10-01", 8, "export", "manual")
        self.assertEqual(budget.snapshot(self.db).spent_micro_usd, 52)

    def test_timeout_and_restart_keep_unknown_cost_pending(self):
        r = self.reserve(60)
        budget.mark_dispatching(self.db, r["attempt_id"])
        stale = self.reserve(10)  # reserved, never dispatched
        self.assertEqual(budget.recover(self.db), {"unknown": 1, "released": 1})
        snap = budget.snapshot(self.db)
        self.assertEqual((snap.pending_micro_usd, snap.open_attempts), (60, 1))
        self.assertFalse(budget.release(self.db, r["attempt_id"], "timer"))  # no refund by time
        with store.open_db(self.db) as conn:
            self.assertEqual(conn.execute("SELECT state FROM attempts WHERE attempt_id = ?",
                                          (stale["attempt_id"],)).fetchone()[0], "released")

    def test_reconciled_attempt_late_settlement_compensates_once(self):
        r = self.reserve(60)
        budget.mark_dispatching(self.db, r["attempt_id"])
        budget.mark_unknown(self.db, r["attempt_id"], "timeout")
        budget.reconcile(self.db, "owner", "rec-1", "2026-01-01T00:00:00", "2099-01-01T00:00:00", 50, "proj",
                         "provider export", [r["attempt_id"]])
        snap = budget.snapshot(self.db)
        self.assertEqual((snap.spent_micro_usd, snap.pending_micro_usd), (50, 0))
        budget.settle(self.db, r["attempt_id"], {"prompt_tokens": 50, "completion_tokens": 0}, "resp-3")
        budget.settle(self.db, r["attempt_id"], {"prompt_tokens": 50, "completion_tokens": 0}, "resp-3")
        self.assertEqual(budget.snapshot(self.db).spent_micro_usd, 50)
        self.assertFalse(budget.reconcile(self.db, "owner", "rec-1", "2026-01-01T00:00:00", "2099-01-01T00:00:00",
                                          50, "proj", "provider export", [r["attempt_id"]]))

    def test_a_reconciliation_subtracts_only_attempts_billed_to_its_project(self):
        def settled(scope, tokens):
            token = budget.BILLING_SCOPE.set(scope)
            try:
                r = self.reserve(tokens)
            finally:
                budget.BILLING_SCOPE.reset(token)
            budget.mark_dispatching(self.db, r["attempt_id"])
            budget.settle(self.db, r["attempt_id"], {"prompt_tokens": tokens, "completion_tokens": 0}, None)

        settled("proj_a", 30)  # two browsers' keys from different projects, same interval
        settled("proj_b", 30)
        self.assertEqual(budget.snapshot(self.db).spent_micro_usd, 60)
        budget.reconcile(self.db, "owner", "a-1", "2026-01-01T00:00:00", "2099-01-01T00:00:00", 30, "proj_a",
                         "project A export", [])
        self.assertEqual(budget.snapshot(self.db).spent_micro_usd, 60)  # A's own $30 against A's total: no change
        settled(None, 10)  # the server-environment key: no recorded project
        with self.assertRaises(budget.BudgetError):  # whose bill it is must be said, not assumed
            budget.reconcile(self.db, "owner", "b-1", "2026-01-01T00:00:00", "2099-01-01T00:00:00", 30, "proj_b",
                             "project B export", [])
        budget.reconcile(self.db, "owner", "b-1", "2026-01-01T00:00:00", "2099-01-01T00:00:00", 30, "proj_b",
                         "project B export", [], unscoped="exclude")
        self.assertEqual(budget.snapshot(self.db).spent_micro_usd, 70)
        with self.assertRaises(budget.BudgetError):  # the same ID with another choice is a different import
            budget.reconcile(self.db, "owner", "b-1", "2026-01-01T00:00:00", "2099-01-01T00:00:00", 30, "proj_b",
                             "project B export", [], unscoped="include")

    def test_init_never_resets_balance_and_paid_gates(self):
        budget.add_adjustment(self.db, "owner", "k", 20, "e", "r")
        store.init_schema(self.db)
        budget.ensure_budget_row(self.db)
        self.assertEqual(budget.snapshot(self.db).spent_micro_usd, 20)
        with self.assertRaises(budget.BudgetError):  # a different baseline cannot overwrite the first
            budget.configure(self.db, "owner", project_start=date(2026, 9, 30), project_end=date(2026, 10, 28),
                             prior_use_micro=5, prior_use_evidence="x", rates=RATES, rate_version="t",
                             enable_paid=True)
        budget.set_paid_enabled(self.db, "owner", False, "test")  # closes PostgreSQL paid admission too
        self.assertEqual(self.reserve(1)["reason"], "paid_disabled")
        with store.open_db(self.db) as conn:  # admission alone closed (a restored database): still refused
            conn.execute("UPDATE budget_settings SET paid_enabled = 1")
        self.assertEqual(self.reserve(1)["reason"], "postgresql_maintenance")

    def test_overrun_freezes_new_paid_work(self):
        r = self.reserve(10)
        budget.mark_dispatching(self.db, r["attempt_id"])
        self.assertTrue(budget.settle(self.db, r["attempt_id"], {"prompt_tokens": 30}, None)["overrun"])
        self.assertEqual(self.reserve(1)["reason"], "frozen")

    def test_cache_writes_bill_at_their_rate_and_reservation_covers_them(self):
        luna = {"input": "0.10", "cached_input": "0.01", "cache_write": "0.125", "output": "0.50"}
        # 500 uncached * 0.10 + 1500 written * 0.125 + 100 out * 0.50 = 287.5 micro-USD, rounded up
        self.assertEqual(budget.micro_cost(luna, 2000, 0, 100, cache_write_tokens=1500), 288)
        self.assertEqual(budget.micro_cost(luna, 2000, 2000, 0), 20)
        self.assertEqual(budget.max_cost(luna, 2000, 100), 300)  # every input token at the write rate

    def test_owner_allowance_and_cap_resplit_envelopes(self):
        budget.configure(self.db, "owner", project_start=date(2026, 9, 30), project_end=date(2026, 10, 28),
                         prior_use_micro=0, prior_use_evidence="test", rates=RATES, rate_version="t",
                         enable_paid=True, allowance_micro=5 * budget.MICRO, cap_micro=5 * budget.MICRO)
        with store.open_db(self.db) as conn:
            row = conn.execute("SELECT * FROM budget_settings").fetchone()
        import json

        self.assertEqual((row["allowance_micro_usd"], row["cap_micro_usd"]), (5 * budget.MICRO, 5 * budget.MICRO))
        self.assertEqual(sum(json.loads(row["envelopes_json"]).values()), 5 * budget.MICRO)
        with self.assertRaises(budget.BudgetError):
            budget.configure(self.db, "owner", project_start=date(2026, 9, 30), project_end=date(2026, 10, 28),
                             prior_use_micro=0, prior_use_evidence="test", rates=RATES, rate_version="t",
                             enable_paid=True, allowance_micro=5 * budget.MICRO, cap_micro=6 * budget.MICRO)

    def test_unknown_model_rate_fails_closed(self):
        r = budget.reserve(self.db, request_id=self.request_id, member_id="m1", stage="generation",
                           purpose="interactive", model="unpriced", input_tokens=1, max_output_tokens=1,
                           count_method="t")
        self.assertEqual(r["reason"], "unknown_rate")


if __name__ == "__main__":
    unittest.main()
