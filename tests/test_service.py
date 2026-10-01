"""Phase 3 gate: request ownership, authorization, background execution, six-user budget and recovery behavior.

Real SQLite transactions and persisted request state; only the provider (FakeTransport) and the clock are
controlled. Gates make races deterministic instead of sleeping.
"""

import json
import sqlite3
import tempfile
import threading
import unittest
import uuid
from datetime import date
from pathlib import Path
from unittest import mock

from rfp_assistant import auth, budget, service, store, ui
from rfp_assistant.contracts import AnswerRequest, Principal
from rfp_assistant.generation import FakeTransport, ProviderResponse
from rfp_assistant.settings import DEFAULT_RATES
from tests import fixtures

Q = "하자보수 기간은 얼마인가요?"


class GatedTransport(FakeTransport):
    """chat() announces entry, then waits for the test to open the gate."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.entered = threading.Semaphore(0)
        self.gate = threading.Event()

    def chat(self, **kw):
        self.entered.release()
        if not self.gate.wait(15):
            raise AssertionError("gate never opened")
        return super().chat(**kw)


def req(ref, question=Q, key=None, mode="single", gen="g1", scope=None):
    return AnswerRequest(idempotency_key=key or str(uuid.uuid4()), generation_id=gen, question=question,
                         scope=scope or [ref], mode=mode, as_of="2026-09-30")


class Base(unittest.TestCase):
    workers = 6
    admission = 12

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = fixtures.make_env(Path(self.tmp.name))
        self.settings = self.env.settings.with_(request_workers=self.workers, request_admission=self.admission,
                                                shutdown_wait_seconds=0.3)
        self.transport = GatedTransport()
        self.res = service.Resources(self.settings, transport=self.transport)
        self.a, self.c, self.d, self.e = (self.env.refs[k] for k in ("기관A", "기관C", "기관D", "기관E"))

    def tearDown(self):
        self.transport.gate.set()
        self.res.close()
        if self.res._runner is not None:  # a deliberately orphaned worker must finish before the files go
            self.res._runner._executor.shutdown(wait=True)
        self.tmp.cleanup()

    def wait_done(self, request_id, principal=None, timeout=10):
        import time

        end = time.monotonic() + timeout
        while time.monotonic() < end:
            view = service.request_status(self.res, principal or self.env.consultant, request_id)
            if view.status not in ("queued", "running"):
                return view
            time.sleep(0.02)
        raise AssertionError(f"request {request_id} did not finish")

    def attempts(self):
        with store.open_db(self.settings.db_path) as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM attempts ORDER BY created_at")]

    def request_row(self, request_id):
        with store.open_db(self.settings.db_path) as conn:
            return dict(conn.execute("SELECT * FROM requests WHERE request_id = ?", (request_id,)).fetchone())


# ---------------------------------------------------------------- no login: roles are declarations


class VisitorTest(Base):
    def test_the_visitor_name_attributes_work_and_every_screen_is_open(self):
        v = auth.visitor("  박  ")
        self.assertEqual(v.member_id, "박")
        self.assertEqual(auth.visitor("").member_id, "owner")
        self.assertTrue(all(v.can(c) for c in ("consultant", "verifier", "budget_admin", "sealed_evaluator")))
        self.transport.gate.set()
        rid = service.submit_answer(self.res, v, req(self.a))
        view = self.wait_done(rid, principal=v)
        self.assertEqual((view.member_id, view.result.status), ("박", "answered"))
        self.assertEqual(budget.snapshot(self.settings.db_path).per_member, {"박": view.settled_micro_usd})
        self.assertEqual(service.unresolved_attempts(self.res, v), [])  # the admin page works for a visitor
        with self.assertRaises(auth.AuthError):  # sealed rows stay out of the verifier screen for everyone
            service.dataset_rows(self.res, v, "test")


class AuthorizationTest(Base):
    # Without login the UI visitor holds every role; these checks keep each service function's declared role
    # enforceable for narrower in-process principals (CLI jobs, tests, a future login).
    def test_consultant_is_denied_every_privileged_action_at_the_service(self):
        c = self.env.consultant
        calls = [
            lambda: service.verifier_trace(self.res, c, Q, [self.a], "2026-09-30"),
            lambda: service.verifier_runs(self.res, c),
            lambda: service.dataset_rows(self.res, c, "dev-pilot"),
            lambda: service.sealed_status(self.res, c),
            lambda: service.unresolved_attempts(self.res, c),
            lambda: service.reconcile(self.res, c, {}),
            lambda: service.add_external_adjustment(self.res, c, "x-1", 1, "e", "r"),
            lambda: service.set_paid_enabled(self.res, c, False, "r"),
            lambda: service.audit_events(self.res, c),
            lambda: service.list_corrections(self.res, c),
            lambda: service.list_requests(self.res, c, all_members=True),
            lambda: service.ingestion_overview(self.res, c),
        ]
        for call in calls:
            with self.assertRaises(auth.AuthError):
                call()

    def test_verifier_cannot_open_sealed_rows_or_administer_budget(self):
        v = self.env.verifier
        with self.assertRaises(auth.AuthError):
            service.dataset_rows(self.res, v, "test")
        with self.assertRaises(auth.AuthError):
            service.sealed_status(self.res, v)
        with self.assertRaises(auth.AuthError):
            service.unresolved_attempts(self.res, v)

    def test_requests_stay_with_their_member_unless_a_verifier_reads_them(self):
        rid = service.submit_answer(self.res, self.env.consultant, req(self.a, mode="metadata", question="기본 정보"))
        other = Principal("c2", frozenset({"consultant"}))
        for call in (lambda: service.request_status(self.res, other, rid),
                     lambda: service.cancel_request(self.res, other, rid),
                     lambda: service.export_request(self.res, other, rid),
                     lambda: service.open_evidence(self.res, other, rid, "E1")):
            with self.assertRaises(auth.AuthError):
                call()
        self.assertEqual(service.request_status(self.res, self.env.verifier, rid).request_id, rid)
        self.assertEqual([v.request_id for v in service.list_requests(self.res, other)], [])


# ---------------------------------------------------------------- background execution


class SubmissionTest(Base):
    def test_rerun_resubmission_returns_one_request_and_one_paid_attempt(self):
        self.transport.gate.set()
        r = req(self.a, key="k1")
        first = service.submit_answer(self.res, self.env.consultant, r)
        second = service.submit_answer(self.res, self.env.consultant, r)
        self.assertEqual(first, second)
        view = self.wait_done(first)
        self.assertEqual((view.status, view.result.status, view.billing_state), ("completed", "answered", "settled"))
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(len(self.attempts()), 1)
        with self.assertRaises(service.ServiceError):  # same key, different payload
            service.submit_answer(self.res, self.env.consultant, req(self.a, question="다른 질문", key="k1"))

    def test_concurrent_duplicate_submissions_create_one_request(self):
        self.transport.gate.set()
        r = req(self.a, key="dup")
        barrier = threading.Barrier(6)
        ids = []

        def go():
            barrier.wait()
            ids.append(service.submit_answer(self.res, self.env.consultant, r))

        threads = [threading.Thread(target=go) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(set(ids)), 1)
        self.wait_done(ids[0])
        self.assertEqual(len(self.transport.calls), 1)
        with store.open_db(self.settings.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM requests WHERE idempotency_key = 'dup'").fetchone()[0], 1)

    def test_workers_never_touch_streamlit(self):
        import inspect

        # The worker path (service and everything it imports) has no UI dependency at all.
        for module in (service, budget, auth, store, __import__("rfp_assistant.generation", fromlist=["x"])):
            self.assertNotIn("streamlit", inspect.getsource(module))


class QueueTest(Base):
    workers = 1
    admission = 2

    def test_queued_cancellation_never_dispatches_and_saturation_rejects_before_paid_work(self):
        first = service.submit_answer(self.res, self.env.consultant, req(self.a))
        self.assertTrue(self.transport.entered.acquire(timeout=10))  # first holds the only worker
        second = service.submit_answer(self.res, self.env.consultant, req(self.a))
        self.assertEqual(service.request_status(self.res, self.env.consultant, second).status, "queued")
        with self.assertRaises(service.ServiceError):  # admission bound reached: local rejection
            service.submit_answer(self.res, self.env.consultant, req(self.a))
        self.assertEqual(len(self.attempts()), 1)
        self.assertEqual(service.cancel_request(self.res, self.env.consultant, second), "cancelled")
        self.transport.gate.set()
        self.assertEqual(self.wait_done(first).status, "completed")
        view = self.wait_done(second)
        self.assertEqual((view.status, view.billing_state, view.attempts), ("cancelled", "none", []))
        self.assertEqual(len(self.transport.calls), 1)
        third = service.submit_answer(self.res, self.env.consultant, req(self.a))  # slots were released
        done = self.wait_done(third)
        self.assertEqual(done.status, "completed", done.result)


class CancellationTest(Base):
    def test_running_cancellation_keeps_the_active_call_billed_and_unattached(self):
        rid = service.submit_answer(self.res, self.env.consultant, req(self.a))
        self.assertTrue(self.transport.entered.acquire(timeout=10))
        self.assertEqual(service.cancel_request(self.res, self.env.consultant, rid), "cancel_requested")
        view = service.request_status(self.res, self.env.consultant, rid)
        self.assertEqual(view.billing_state, "pending")  # the dispatched call keeps its reservation
        self.assertGreater(view.reserved_micro_usd, 0)
        self.transport.gate.set()
        view = self.wait_done(rid)
        self.assertEqual((view.status, view.billing_state), ("cancelled", "settled"))
        self.assertTrue(view.cancel_requested)
        self.assertFalse(ui.may_attach({"request_id": rid, "generation_id": "g1", "target": "t"}, "t", view))

    def test_cancellation_before_generation_releases_nothing_because_nothing_was_reserved(self):
        real = service.prepare_answer

        def cancel_during_retrieval(res, principal, *a, **kw):
            out = real(res, principal, *a, **kw)
            service.cancel_request(res, principal, kw["request_id"])
            return out

        self.transport.gate.set()
        with mock.patch.object(service, "prepare_answer", cancel_during_retrieval):
            rid = service.submit_answer(self.res, self.env.consultant, req(self.a))
            view = self.wait_done(rid)
        self.assertEqual((view.status, view.result.status), ("cancelled", "cancelled"))
        self.assertEqual((self.attempts(), self.transport.calls), ([], []))


class ScopeOwnershipTest(Base):
    def test_a_late_answer_cannot_attach_to_a_changed_scope_but_still_settles(self):
        target_a = ui.target_key([(self.a.doc_id, self.a.source_hash)], Q, "single", "2026-09-30")
        rid = service.submit_answer(self.res, self.env.consultant, req(self.a, gen="gen-a"))
        owned = {"request_id": rid, "generation_id": "gen-a", "target": target_a}
        self.assertTrue(self.transport.entered.acquire(timeout=10))
        target_d = ui.target_key([(self.d.doc_id, self.d.source_hash)], Q, "single", "2026-09-30")  # user moved on
        self.transport.gate.set()
        view = self.wait_done(rid)
        self.assertEqual((view.result.status, view.billing_state), ("answered", "settled"))
        self.assertTrue(ui.may_attach(owned, target_a, view))
        self.assertFalse(ui.may_attach(owned, target_d, view))
        changed_question = ui.target_key([(self.a.doc_id, self.a.source_hash)], "다른 질문", "single", "2026-09-30")
        self.assertFalse(ui.may_attach(owned, changed_question, view))
        self.assertFalse(ui.may_attach({**owned, "generation_id": "gen-b"}, target_a, view))
        self.assertEqual(view.scope, [{"doc_id": self.a.doc_id, "source_hash": self.a.source_hash}])


class ShutdownRestartTest(Base):
    def test_controlled_stop_then_restart_recovers_conservatively_without_replay(self):
        running = service.submit_answer(self.res, self.env.consultant, req(self.a))
        self.assertTrue(self.transport.entered.acquire(timeout=10))
        worker_attempt = self.attempts()[0]["attempt_id"]
        closer = threading.Thread(target=self.res.close)
        closer.start()
        closer.join(5)
        with self.assertRaises(service.ServiceError):  # no new submissions after stop
            service.submit_answer(self.res, self.env.consultant, req(self.a))
        self.assertEqual(self.request_row(running)["status"], "interrupted")
        self.assertEqual(self.attempts()[0]["state"], "dispatching")
        # a crash-like leftover: a queued row and a never-dispatched reservation from another process
        queued = str(uuid.uuid4())
        with store.open_db(self.settings.db_path) as conn:
            conn.execute("INSERT INTO requests(request_id, member_id, idempotency_key, input_hash, config_hash, "
                         "scope_json, status, created_at, updated_at, request_json, mode) VALUES (?, 'c1', ?, 'h', "
                         "'c', '[]', 'queued', 't', 't', '{}', 'single')", (queued, queued))
        res2 = service.Resources(self.settings, transport=FakeTransport(), recover=True)
        try:
            self.assertEqual(self.request_row(queued)["status"], "interrupted")
            self.assertEqual({a["attempt_id"]: a["state"] for a in self.attempts()}[worker_attempt], "unknown")
            self.assertEqual(res2.transport.calls, [])  # nothing replayed
            snap = budget.snapshot(self.settings.db_path)
            self.assertEqual(snap.pending_micro_usd, self.attempts()[0]["reserved_micro_usd"])
            # the old worker's call returns after restart: settles exactly once; a duplicate changes nothing
            self.transport.gate.set()
            usage = {"prompt_tokens": 500, "completion_tokens": 40, "cached_tokens": 0}
            first = budget.settle(self.settings.db_path, worker_attempt, usage, "resp-x")
            again = budget.settle(self.settings.db_path, worker_attempt, usage, "resp-x")
            self.assertTrue(again["duplicate"])
            spent = budget.snapshot(self.settings.db_path).spent_micro_usd
            self.assertEqual(spent, first["settled_micro_usd"])
        finally:
            res2.close()

    def test_a_second_owner_of_the_same_data_directory_is_refused(self):
        res2 = service.Resources(self.settings, transport=FakeTransport(), recover=True)
        try:
            with self.assertRaises(service.GatewayLockError):
                service.Resources(self.settings, transport=FakeTransport(), recover=True)
        finally:
            res2.close()


# ---------------------------------------------------------------- shared budget under six users


class SixUserBudgetTest(Base):
    def test_six_users_near_cap_admit_only_affordable_dispatches(self):
        est = service.prepare_answer(self.res, self.env.consultant, Q, [self.a], "2026-09-30")["estimate_micro_usd"]
        with store.open_db(self.settings.db_path) as conn:  # room for exactly two maximum reservations
            conn.execute("UPDATE budget_settings SET cap_micro_usd = ?", (2 * est + est // 2,))
        users = [Principal(f"m{i}", frozenset({"consultant"})) for i in range(6)]
        ids = []
        barrier = threading.Barrier(6)

        def go(p):
            barrier.wait()
            ids.append((p, service.submit_answer(self.res, p, req(self.a))))

        threads = [threading.Thread(target=go, args=(p,)) for p in users]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for _ in range(2):
            self.assertTrue(self.transport.entered.acquire(timeout=10))
        import time

        end = time.monotonic() + 10  # every other member reaches admission while two calls hold their reserves
        while time.monotonic() < end:
            blocked = [rid for p, rid in ids if service.request_status(self.res, p, rid).status == "completed"]
            if len(blocked) == 4:
                break
            time.sleep(0.02)
        snap = budget.snapshot(self.settings.db_path)
        self.assertLessEqual(snap.spent_micro_usd + snap.pending_micro_usd, snap.cap_micro_usd)
        self.assertEqual(snap.open_attempts, 2)
        self.transport.gate.set()
        views = [self.wait_done(rid, principal=p) for p, rid in ids]
        statuses = sorted(v.result.status for v in views)
        self.assertEqual(statuses.count("answered"), 2)
        self.assertEqual(statuses.count("budget_blocked"), 4)
        self.assertEqual(len(self.transport.calls), 2)
        self.assertEqual(len(self.attempts()), 2)
        snap = budget.snapshot(self.settings.db_path)
        self.assertEqual(sum(snap.per_member.values()), snap.spent_micro_usd)  # one ledger, member attribution
        self.assertEqual(snap.pending_micro_usd, 0)

    def test_billed_retry_is_a_new_attempt_and_the_old_cost_stays(self):
        self.transport.gate.set()
        self.transport.responder = lambda m: ProviderResponse("{not json", None, "stop",
                                                              {"prompt_tokens": 800, "completion_tokens": 30}, "r1")
        first = self.wait_done(service.submit_answer(self.res, self.env.consultant, req(self.a)))
        self.assertEqual((first.result.status, first.billing_state), ("technical_error", "settled"))
        cost1 = first.settled_micro_usd
        self.transport.responder = FakeTransport().responder
        retry = self.wait_done(service.submit_answer(self.res, self.env.consultant, req(self.a)))  # new key
        self.assertEqual(retry.result.status, "answered")
        self.assertNotEqual(first.request_id, retry.request_id)
        self.assertEqual(budget.snapshot(self.settings.db_path).spent_micro_usd, cost1 + retry.settled_micro_usd)

    def test_unknown_price_frozen_ledger_and_overrun_fail_closed(self):
        self.transport.gate.set()
        with store.open_db(self.settings.db_path) as conn:
            conn.execute("UPDATE budget_settings SET rates_json = ?", (store.dumps({}),))
        v = self.wait_done(service.submit_answer(self.res, self.env.consultant, req(self.a)))
        self.assertEqual((v.result.status, v.result.error), ("budget_blocked", "unknown_rate"))
        with store.open_db(self.settings.db_path) as conn:
            conn.execute("UPDATE budget_settings SET rates_json = ?", (store.dumps(DEFAULT_RATES),))
        # reported cost above the reservation: recorded, then new paid work freezes
        self.transport.responder = lambda m: ProviderResponse(
            FakeTransport().responder(m).content, None, "stop", {"prompt_tokens": 10_000_000, "completion_tokens": 1},
            "big")
        v = self.wait_done(service.submit_answer(self.res, self.env.consultant, req(self.a)))
        self.assertEqual(v.billing_state, "settled")
        self.assertTrue(budget.snapshot(self.settings.db_path).frozen_reason)
        self.transport.responder = FakeTransport().responder
        v = self.wait_done(service.submit_answer(self.res, self.env.consultant, req(self.a)))
        self.assertEqual((v.result.status, v.result.error), ("budget_blocked", "frozen"))

    def test_unavailable_ledger_blocks_paid_work_and_is_not_shown_as_live(self):
        self.transport.gate.set()
        real = store.open_db

        def flaky(db_path):
            raise sqlite3.OperationalError("database is locked")

        with mock.patch.object(budget, "open_db", flaky):
            with self.assertRaises(service.ServiceError):
                service.budget_snapshot(self.res, self.env.consultant)
            admission = budget.reserve(self.settings.db_path, request_id="x", member_id="m", stage="generation",
                                       purpose="interactive", model=self.settings.generation_model, input_tokens=1,
                                       max_output_tokens=1, count_method="t")
        self.assertFalse(admission["admitted"])
        self.assertTrue(admission["reason"].startswith("ledger_unavailable"))
        self.assertIs(store.open_db, real)

    def test_changed_prices_settle_at_the_reserved_snapshot(self):
        rid = service.submit_answer(self.res, self.env.consultant, req(self.a))
        self.assertTrue(self.transport.entered.acquire(timeout=10))
        model = self.settings.generation_model
        doubled = {**DEFAULT_RATES, model: {k: str(2 * float(v)) for k, v in DEFAULT_RATES[model].items()}}
        with store.open_db(self.settings.db_path) as conn:
            conn.execute("UPDATE budget_settings SET rates_json = ?, rate_version = 'doubled'", (store.dumps(doubled),))
        self.transport.gate.set()
        view = self.wait_done(rid)
        [a] = self.attempts()
        usage = json.loads(a["raw_usage_json"])
        expected = budget.micro_cost(DEFAULT_RATES[model], usage["prompt_tokens"], usage.get("cached_tokens", 0),
                                     usage["completion_tokens"])
        self.assertEqual(view.settled_micro_usd, expected)
        self.assertEqual(json.loads(a["price_json"])["rate_version"], "fixture")

    def test_exhausted_cap_keeps_free_routes_and_polling_never_dispatches(self):
        self.transport.gate.set()
        budget.add_adjustment(self.settings.db_path, "owner", "spent-elsewhere", 16 * budget.MICRO, "export", "test")
        v = self.wait_done(service.submit_answer(self.res, self.env.consultant, req(self.a)))
        self.assertEqual((v.result.status, v.result.error), ("budget_blocked", "cap_exhausted"))
        snap = service.budget_snapshot(self.res, self.env.consultant)
        self.assertIn("cap_exhausted", snap.warnings)
        c = self.env.consultant
        self.assertTrue(service.search_projects(self.res, c, {}, "하자보수"))
        self.assertTrue(service.retrieve(self.res, c, Q, [self.a]).evidence)
        self.assertEqual(service.original_download(self.res, c, self.a.doc_id, self.a.source_hash).data,
                         fixtures.PDF_A)
        meta = self.wait_done(service.submit_answer(self.res, c, req(self.a, mode="metadata", question="기본")))
        self.assertEqual(meta.status, "completed")
        self.assertEqual(service.open_evidence(self.res, c, v.request_id, "E1").evidence_id, "E1")
        before = (len(self.transport.calls), len(self.attempts()), snap.ledger_revision)
        for _ in range(20):  # the polling fragment's reads
            service.budget_snapshot(self.res, c)
            service.request_status(self.res, c, v.request_id)
        after = service.budget_snapshot(self.res, c)
        self.assertEqual(before, (len(self.transport.calls), len(self.attempts()), after.ledger_revision))


class ReconciliationTest(Base):
    def unknown_attempt(self):
        self.transport.responder = lambda m: __import__("rfp_assistant.generation", fromlist=["x"]).ProviderError(
            "APITimeoutError", pre_execution=False)
        self.transport.gate.set()
        v = self.wait_done(service.submit_answer(self.res, self.env.consultant, req(self.a)))
        self.assertEqual(v.billing_state, "unknown")
        return v.attempts[0]

    def test_double_import_and_late_usage_do_not_double_count(self):
        admin = Principal("owner", frozenset({"budget_admin"}))
        a = self.unknown_attempt()
        record = {"reconciliation_id": "2026-10-01", "interval_start": "2026-01-01T00:00:00+00:00",
                  "interval_end": "2099-01-01T00:00:00+00:00", "provider_total_micro_usd": 700, "scope": "proj",
                  "evidence": "usage export 2026-10-01", "covered_attempt_ids": [a["attempt_id"]]}
        snap = service.reconcile(self.res, admin, record)
        self.assertEqual((snap.spent_micro_usd, snap.pending_micro_usd), (700, 0))
        snap = service.reconcile(self.res, admin, record)  # harmless re-import
        self.assertEqual(snap.spent_micro_usd, 700)
        with self.assertRaises(budget.BudgetError):  # a changed total under the same ID needs a correction
            service.reconcile(self.res, admin, {**record, "provider_total_micro_usd": 900})
        budget.settle(self.settings.db_path, a["attempt_id"], {"prompt_tokens": 400, "completion_tokens": 10}, "late")
        budget.settle(self.settings.db_path, a["attempt_id"], {"prompt_tokens": 400, "completion_tokens": 10}, "late")
        self.assertEqual(budget.snapshot(self.settings.db_path).spent_micro_usd, 700)
        self.assertEqual(len(service.audit_events(self.res, admin)), 1)

    def test_reconciliation_covers_only_unknown_attempts_inside_its_interval(self):
        admin = Principal("owner", frozenset({"budget_admin"}))
        a = self.unknown_attempt()
        with self.assertRaises(budget.BudgetError):
            service.reconcile(self.res, admin, {
                "reconciliation_id": "r2", "interval_start": "2001-01-01T00:00:00+00:00",
                "interval_end": "2001-12-31T00:00:00+00:00", "provider_total_micro_usd": 1, "scope": "proj",
                "evidence": "e", "covered_attempt_ids": [a["attempt_id"]]})
        self.assertEqual(budget.snapshot(self.settings.db_path).pending_micro_usd, a["reserved_micro_usd"])

    def test_owner_settles_an_unknown_attempt_from_evidence_with_an_audit_event(self):
        admin = Principal("owner", frozenset({"budget_admin"}))
        a = self.unknown_attempt()
        view = service.unresolved_attempts(self.res, admin)
        self.assertEqual([x["attempt_id"] for x in view], [a["attempt_id"]])
        with self.assertRaises(service.ServiceError):  # reason and evidence are required
            service.settle_from_evidence(self.res, admin, a["attempt_id"], {"prompt_tokens": 1}, None, "", "")
        out = service.settle_from_evidence(self.res, admin, a["attempt_id"], {"prompt_tokens": 900,
                                           "completion_tokens": 20}, None, "dashboard 2026-10-01", "timeout check")
        self.assertFalse(out["duplicate"])
        self.assertEqual(service.unresolved_attempts(self.res, admin), [])
        self.assertTrue(service.add_external_adjustment(self.res, admin, "notebook-0930", 1234, "export", "direct key"))
        self.assertFalse(service.add_external_adjustment(self.res, admin, "notebook-0930", 1234, "export", "again"))
        actions = [e["action"] for e in service.audit_events(self.res, admin)]
        self.assertEqual(sorted(actions), ["external_adjustment", "settle_from_evidence"])


class PacingTest(unittest.TestCase):
    def test_cap_warnings_and_pacing_use_the_cap_not_the_allowance(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "rfp.sqlite3"
            store.init_schema(db)
            budget.ensure_budget_row(db)
            budget.configure(db, "owner", project_start=date(2026, 10, 1), project_end=date(2026, 10, 31),
                             prior_use_micro=0, prior_use_evidence="t", rates=DEFAULT_RATES, rate_version="t",
                             enable_paid=True)
            budget.add_adjustment(db, "owner", "k", 12 * budget.MICRO, "e", "r")  # 75% of the $16 cap
            snap = budget.snapshot(db, today=date(2026, 10, 2))
            self.assertEqual(snap.warnings[:2], ["cap_50", "cap_75"])
            self.assertNotIn("cap_90", snap.warnings)
            self.assertIn("ahead_of_pace", snap.warnings)
            self.assertAlmostEqual(snap.spent_percent, 60.0)  # the $20 percentage stays separate
            self.assertNotIn("ahead_of_pace", budget.snapshot(db, today=date(2026, 10, 30)).warnings)


# ---------------------------------------------------------------- deterministic modes and comparisons


class ModesTest(Base):
    def setUp(self):
        super().setUp()
        self.transport.gate.set()

    def run_req(self, r, principal=None):
        return self.wait_done(service.submit_answer(self.res, principal or self.env.consultant, r)).result

    def test_metadata_mode_is_free_and_keeps_unknown_zero_and_conflict_states(self):
        r = self.run_req(req(self.a, mode="metadata", question="발주 기관과 마감일", scope=[self.a, self.d]))
        self.assertEqual(r.billing_state, "none")
        facts = {(f["doc_id"], f["field"]): f for f in r.facts}
        self.assertEqual(facts[(self.a.doc_id, "institution")]["state"], "conflict")  # byte-identical copy disagrees
        self.assertEqual(facts[(self.d.doc_id, "amount_krw")]["state"], "unknown")
        self.assertEqual(r.status, "conflicting_evidence")
        self.assertIn({"doc_id": self.d.doc_id, "field": "bid_close", "reason": "unknown_metadata"}, r.missing_fields)
        e = self.run_req(req(self.e, mode="metadata", question="금액"))
        self.assertEqual({f["field"]: f["state"] for f in e.facts}["amount_krw"], "zero_review")
        self.assertEqual(self.transport.calls, [])

    def test_inventory_lists_structured_rows_in_source_order_with_its_declaration(self):
        idx = self.res.index()
        doc = self.a
        with store.open_db(self.settings.db_path) as conn:
            x = conn.execute("SELECT active_extraction_id FROM documents d JOIN sources s ON s.source_hash = "
                             "d.active_source_hash WHERE d.doc_id = ?", (doc.doc_id,)).fetchone()[0]
            els = sorted((e for (xx, _), e in idx.elements.items() if xx == x), key=lambda e: e["source_order"])
            for code, kind, el in (("SFR-002", "detail", els[-1]), ("SFR-001", "detail", els[1]),
                                   ("SFR-003", "summary", els[0])):
                conn.execute("INSERT INTO requirements VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (idx.version, x, code, code, kind, el["element_id"], code + " 이름"))
        r = self.run_req(req(doc, mode="inventory", question=""))
        codes = [i["code"] for i in r.inventory["items"]]
        self.assertEqual(codes, ["SFR-003", "SFR-001", "SFR-002"])  # source order, not code order
        self.assertEqual(r.inventory["summary_only"], ["SFR-003"])
        self.assertIn("번호 없는 서술형 요구사항은 포함되지 않습니다", r.inventory["completeness_text"])
        view = service.open_evidence(self.res, self.env.consultant, r.request_id, "R1")
        self.assertTrue(view.context)
        self.assertEqual((r.billing_state, self.transport.calls), ("none", []))
        q = self.run_req(req(self.e, mode="inventory", question=""))
        self.assertEqual(q.status, "ingestion_unavailable")

    def test_comparison_retrieves_each_side_within_its_share(self):
        r = self.run_req(req(self.a, mode="compare", question="시스템 구축 내용은?", scope=[self.a, self.d]))
        self.assertEqual(r.status, "answered", r.error)
        sides = {c["doc_id"]: c for c in r.coverage}
        self.assertGreaterEqual(sides[self.a.doc_id]["evidence"], 1)
        self.assertGreaterEqual(sides[self.d.doc_id]["evidence"], 1)
        self.assertEqual(sorted(r.evidence), [f"E{i}" for i in range(1, len(r.evidence) + 1)])
        self.assertLessEqual(sum(e["token_count"] for e in r.evidence.values()), self.settings.evidence_max_tokens)
        self.assertEqual({c["doc_id"] for c in r.claims}, {self.a.doc_id, self.d.doc_id})
        sent = json.loads(self.transport.calls[0]["messages"][1]["content"])
        self.assertEqual(sent["mode"], "compare")

    def test_an_answer_for_only_one_side_is_rejected(self):
        def one_side(messages):
            body = json.loads(messages[-1]["content"])
            ev = body["evidence"][0]
            return ProviderResponse(json.dumps({"status": "answered", "summary": "한쪽", "claims": [
                {"text": "x", "kind": "source_fact", "doc_id": ev["doc_id"], "evidence_ids": [ev["evidence_id"]]}],
                "missing_fields": [], "conflicts": [], "next_action": None}), None, "stop",
                {"prompt_tokens": 100, "completion_tokens": 10}, "one")

        self.transport.responder = one_side
        r = self.run_req(req(self.a, mode="compare", question="시스템 구축 내용은?", scope=[self.a, self.d]))
        self.assertEqual(r.status, "technical_error")
        self.assertIn("comparison_side_missing", r.error)

    def test_an_unavailable_side_is_an_explicit_limitation(self):
        r = self.run_req(req(self.a, mode="compare", question="하자보수 기간은?", scope=[self.a, self.e]))
        self.assertEqual(r.status, "answered", r.error)
        self.assertIn({"doc_id": self.e.doc_id, "field": "document", "reason": "ingestion_unavailable"},
                      r.missing_fields)

    def test_shared_bytes_bind_evidence_to_each_selected_association(self):
        r = self.run_req(req(self.a, mode="compare", question="하자보수 기간은?", scope=[self.a, self.c]))
        self.assertEqual(r.status, "answered", r.error)
        self.assertEqual({e["doc_id"] for e in r.evidence.values()}, {self.a.doc_id, self.c.doc_id})
        self.assertEqual(self.a.source_hash, self.c.source_hash)

    def test_invalid_comparison_shapes_are_rejected_locally(self):
        for bad in ([self.a], [self.a, self.a], [self.a, self.c, self.d]):
            with self.assertRaises(service.ServiceError):
                service.submit_answer(self.res, self.env.consultant, req(self.a, mode="compare", scope=bad))
        self.assertEqual(self.attempts(), [])

    def test_a_wrong_document_code_stays_inside_the_selected_scope(self):
        r = self.run_req(req(self.d, question="SFR-001 요구사항은?"))
        self.assertTrue(all(e["doc_id"] == self.d.doc_id for e in r.evidence.values()))


# ---------------------------------------------------------------- sources, exports and verifier work


class SearchFilterTest(Base):
    def test_unknown_or_conflicting_values_are_excluded_or_flagged_never_satisfied(self):
        c = self.env.consultant
        f = {"amount_min": 1, "closing_from": "2024-01-01"}
        strict = {i["doc_id"] for i in service.search_projects(self.res, c, f, "")}
        self.assertNotIn(self.d.doc_id, strict)  # unknown amount and closing date
        self.assertNotIn(self.a.doc_id, strict)  # bid_close conflicts with its byte-identical copy
        shown = {i["doc_id"]: i for i in service.search_projects(self.res, c, {**f, "include_unknown": True}, "")}
        self.assertEqual(sorted(shown[self.d.doc_id]["filter_undecided"]), ["amount_krw:unknown", "bid_close:unknown"])
        self.assertIn("bid_close:conflict", shown[self.a.doc_id]["filter_undecided"])
        self.assertNotIn(self.e.doc_id, shown)  # a known 0원 amount fails amount_min; it is not "unknown"

    def test_only_the_highest_cap_warning_is_shown(self):
        self.assertEqual(ui.visible_warnings(["cap_50", "cap_75", "ahead_of_pace"]), ["cap_75", "ahead_of_pace"])
        self.assertEqual(ui.visible_warnings(["cap_50", "cap_75", "cap_90", "cap_exhausted"]), ["cap_exhausted"])


class SourceAndExportTest(Base):
    def setUp(self):
        super().setUp()
        self.transport.gate.set()

    def test_downloads_accept_only_managed_current_versions(self):
        c = self.env.consultant
        for doc_id, h in (("../../etc/passwd", self.a.source_hash), (self.a.doc_id, "0" * 64),
                          (self.a.doc_id, self.d.source_hash), (self.a.doc_id, "../" + self.a.source_hash[3:])):
            with self.assertRaises(service.ServiceError):
                service.original_download(self.res, c, doc_id, h)
        with store.open_db(self.settings.db_path) as conn:
            conn.execute("UPDATE sources SET original_path = ? WHERE source_hash = ?",
                         (str(self.settings.data_dir / "x.pdf"), self.d.source_hash))
        with self.assertRaises(service.ServiceError):
            service.original_download(self.res, c, self.d.doc_id, self.d.source_hash)

    def test_evidence_view_carries_review_state_and_exports_are_redacted(self):
        p = auth.visitor("kim")
        v = self.wait_done(service.submit_answer(self.res, p, req(self.a)), principal=p)
        view = service.open_evidence(self.res, p, v.request_id, "E1")
        self.assertEqual(view.source_hash, self.a.source_hash)
        self.assertTrue(view.download_available)
        export = service.export_request(self.res, p, v.request_id)
        text = json.dumps(export, ensure_ascii=False)
        self.assertNotIn("sk-", text)
        self.assertNotIn(str(self.settings.data_dir), text)
        self.assertNotIn(str(self.settings.source_dir), text)
        ev = export["result"]["evidence"]["E1"]
        self.assertEqual((ev["source_hash"], bool(ev["element_ids"])), (self.a.source_hash, True))
        self.assertIn("location", ev)


class VerifierTest(Base):
    def test_runs_are_frozen_and_comparable_and_free(self):
        v = self.env.verifier
        a = service.verifier_trace(self.res, v, Q, [self.a], "2026-09-30")
        b = service.verifier_trace(self.res, v, Q, [self.a], "2026-09-30", limits={"evidence_max_units": 1})
        self.assertNotEqual(a["config"]["config_id"], b["config"]["config_id"])
        self.assertEqual(len(b["retrieval"]["evidence"]), 1)
        again = service.verifier_run(self.res, v, a["run_id"])
        self.assertEqual(again["config"], a["config"])  # a later control change does not relabel the old run
        diff = service.compare_verifier_runs(self.res, v, a["run_id"], b["run_id"])
        self.assertIn("limits", diff["config_changes"])
        self.assertTrue(diff["same_question"])
        both = service.verifier_trace(self.res, v, "시스템 구축", [self.a, self.d], "2026-09-30")
        self.assertEqual(len(both["coverage"]), 2)
        self.assertEqual((self.transport.calls, self.attempts()), ([], []))
        export = service.export_verifier_run(self.res, v, a["run_id"])
        self.assertEqual(export["run_id"], a["run_id"])

    def test_corrections_append_with_quoted_original_and_never_touch_sealed_labels(self):
        v = self.env.verifier
        run = service.verifier_trace(self.res, v, Q, [self.a], "2026-09-30")
        ev = run["retrieval"]["evidence"][0]
        target = {"doc_id": self.a.doc_id, "source_hash": self.a.source_hash, "extraction_id": ev["extraction_id"],
                  "element_id": ev["element_ids"][0]}
        quote = self.res.index().elements[(ev["extraction_id"], ev["element_ids"][0])]["raw_text"][:20]
        with self.assertRaises(service.ServiceError):  # a quote that is not in the original element
            service.record_correction(self.res, v, reason="오타", evidence=target, quote="원문에 없는 문장",
                                      proposal={"field": "x"}, run_id=run["run_id"])
        cid = service.record_correction(self.res, v, reason="기대 근거 위치 수정", evidence=target, quote=quote,
                                        proposal={"expected_element": target["element_id"]}, run_id=run["run_id"])
        [c] = service.list_corrections(self.res, v)
        self.assertEqual((c["correction_id"], c["reviewer"], c["run_id"]), (cid, "v1", run["run_id"]))
        with self.assertRaises(auth.AuthError):
            service.record_correction(self.res, v, reason="r", evidence=target, quote=quote, proposal={},
                                      dataset="test", row_id="t-1")


if __name__ == "__main__":
    unittest.main()
