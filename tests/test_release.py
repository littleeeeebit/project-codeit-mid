"""Backup and staged restore, the phase-5 reconciliation fixture and the read-only release report."""

import json
import os
import tempfile
import unittest
from datetime import date
from argparse import Namespace
from contextlib import redirect_stdout
from io import BytesIO, StringIO, TextIOWrapper
from pathlib import Path
from unittest import mock

from rfp_assistant.service import answers, auth, service
from rfp_assistant.gateway import budget, generation
from rfp_assistant import cli
from rfp_assistant.retrieval import dense
from rfp_assistant.evaluation import evaluation, release, sealed
from rfp_assistant.storage import postgres, store
from rfp_assistant.contracts import AnswerRequest
from rfp_assistant.gateway.generation import FakeTransport
from rfp_assistant.settings import DEFAULT_RATES
from tests import fixtures
from tests import release_fixtures as p4


class RestoreCLIReportTest(unittest.TestCase):
    def test_reports_pass_fail_exit_codes(self):
        args = Namespace(backup="C:/isolated/manifest.json", staging=None)
        for passed in (True, False):
            report = {"passed": passed, "checks": {"parity": passed},
                      "restore_check_version": "postgresql-restore-check-1"}
            with self.subTest(passed=passed), mock.patch.object(release, "restore_check", return_value=report), \
                    redirect_stdout(StringIO()) as output:
                code = cli.cmd_restore_check(args, None)
                self.assertEqual(code, 0 if passed else 1)
                self.assertEqual(json.loads(output.getvalue())["passed"], passed)


def _configure(settings, prior_use: int) -> None:
    budget.configure(settings.db_path, "owner", project_start=date(2026, 9, 30), project_end=date(2026, 10, 28),
                     prior_use_micro=prior_use, prior_use_evidence="dashboard 2026-09-30", rates=DEFAULT_RATES,
                     rate_version="fixture", enable_paid=True)
    budget.set_paid_enabled(settings.db_path, "owner", True, "fixture")  # with PostgreSQL paid admission


def _unknown_attempt(settings, tokens: int = 600) -> str:
    request_id = dense.ensure_job_request(settings, "owner-cli", f"job-{tokens}", {"job": "test"})
    owner = postgres.GatewayOwner(settings.db_path)  # an owner CLI job dispatches through the paid gateway
    try:
        admission = budget.reserve(settings.db_path, request_id=request_id, member_id="owner-cli", stage="generation",
                                   purpose="interactive", model="gpt-6-luna", input_tokens=tokens,
                                   max_output_tokens=0, count_method="test")
        budget.mark_dispatching(settings.db_path, admission["attempt_id"])
        budget.mark_unknown(settings.db_path, admission["attempt_id"], "timeout (test)")
    finally:
        owner.release()
    return admission["attempt_id"]


class BackupRestoreTest(unittest.TestCase):
    """Restore fixture: a settled charge, a prior-use adjustment and an unresolved attempt."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = fixtures.make_env(self.root / "live", paid=False)
        self.s = self.env.settings
        _configure(self.s, 250_000)
        res = service.Resources(self.s, transport=FakeTransport())
        try:
            r = service.answer(res, self.env.consultant, AnswerRequest("k1", "k1", "하자보수 기간은?",
                                                                     [self.env.refs["기관A"]], as_of="2026-09-30"))
            self.assertEqual(r.billing_state, "settled")
        finally:
            res.close()
        self.unknown = _unknown_attempt(self.s)
        restore = os.environ[fixtures.database(ready=False)]  # the empty isolated restore target
        self.patch = mock.patch.dict(os.environ, {"RFP_RESTORE_DATABASE_DSN": restore})
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_backup_refuses_unsafe_destinations(self):
        for bad in (Path("relative"), self.s.data_dir / "backup", self.s.source_dir / "x"):
            with self.assertRaises(release.ReleaseError):
                release.backup(self.s, bad, "owner")
        full = self.root / "full"
        full.mkdir()
        (full / "x").write_text("x", encoding="utf-8")
        with self.assertRaises(release.ReleaseError):
            release.backup(self.s, full, "owner")

    def test_restore_reproduces_conservative_accounting_with_paid_off(self):
        before = release.ledger_summary(self.s.db_path)
        out = release.backup(self.s, self.root / "backup-1", "owner")
        self.assertEqual(release.ledger_summary(self.s.db_path), before)  # the live ledger is unchanged
        report = release.restore_check(self.s, Path(out["manifest"]))
        self.assertTrue(report["passed"], [k for k, ok in report["checks"].items() if not ok])
        restored = report["restored_ledger"]
        self.assertEqual(restored["prior_use_micro_usd"], 250_000)
        self.assertEqual((restored["spent_micro_usd"], restored["pending_micro_usd"], restored["available_micro_usd"]),
                         (before["spent_micro_usd"], before["pending_micro_usd"], before["available_micro_usd"]))
        self.assertGreater(restored["unknown_micro_usd"], 0)
        self.assertTrue(report["checks"]["staging paid admission disabled"])
        self.assertFalse(report["paid_admission"])
        self.assertTrue(budget.snapshot(self.s.db_path).paid_enabled)  # staging never touches the live setting
        self.assertEqual(report["after_restart_recovery"]["unknown_micro_usd"], restored["unknown_micro_usd"])
        self.assertTrue(report["checks"]["historical evidence index"])
        self.assertEqual(len(list((self.s.data_dir / "releases" / "restore-checks").glob("*.json"))), 1)
        with self.assertRaises(ValueError):  # the restored database is no longer empty
            release.restore_check(self.s, Path(out["manifest"]))

    def test_backup_waits_for_the_serving_owner_to_stop(self):
        live = postgres.GatewayOwner(self.s.db_path)  # the serving app's own session holds the paid gateway
        try:
            with self.assertRaises(store.LockHeld):
                release.backup(self.s, self.root / "locked", "owner")
        finally:
            live.release()
        out = release.backup(self.s, self.root / "locked", "owner")  # the refused attempt left nothing behind
        self.assertTrue(Path(out["manifest"]).exists())

    def test_budget_report_reads_the_ledger_without_a_key_gateway_or_provider(self):
        before = release.ledger_summary(self.s.db_path)
        out = TextIOWrapper(BytesIO(), encoding="utf-8")  # main() reconfigures stdout
        keyless = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        refuse = AssertionError("budget-report must not open a gateway or a provider client")
        with mock.patch.dict(os.environ, keyless, clear=True), redirect_stdout(out), \
                mock.patch.object(cli, "load_settings", return_value=self.s.with_(provider="openai")), \
                mock.patch.object(service, "Resources", side_effect=refuse), \
                mock.patch.object(generation.OpenAITransport, "__init__", side_effect=refuse):
            self.assertEqual(cli.main(["budget-report"]), 0)
        out.flush()
        report = json.loads(out.buffer.getvalue().decode("utf-8"))
        self.assertEqual(release.ledger_summary(self.s.db_path), before)  # nothing written, revision unchanged
        total = report["total"]
        self.assertEqual((total["spent_micro_usd"], total["pending_micro_usd"], total["available_micro_usd"]),
                         (before["spent_micro_usd"], before["pending_micro_usd"], before["available_micro_usd"]))
        self.assertGreater(total["unknown_micro_usd"], 0)
        self.assertEqual(sum(m["settled_micro_usd"] for m in report["members"].values()) + total["adjustments_micro_usd"],
                         total["spent_micro_usd"])  # prior use is an adjustment, not a member's spending
        self.assertEqual(sum(m["pending_micro_usd"] for m in report["members"].values()), total["pending_micro_usd"])
        self.assertEqual(sum(c["used_micro_usd"] for c in report["categories"].values()),
                         total["spent_micro_usd"] - total["adjustments_micro_usd"] + total["pending_micro_usd"])
        self.assertEqual(report["watermark"], {"ledger_revision": before["ledger_revision"], "reconciled_through": None,
                                               "reconciliation_id": None, "scope": None,
                                               "unresolved_micro_usd": total["unknown_micro_usd"]})

    def test_budget_report_reads_one_ledger_state_while_an_attempt_settles(self):
        """Review round 1 (F2): an attempt settles on another connection in the middle of the report."""
        before = release.ledger_summary(self.s.db_path)
        used, settled = budget._purpose_used, []

        def settle_midway(conn, purpose):
            if not settled:
                settled.append(budget.settle(self.s.db_path, self.unknown, {"prompt_tokens": 10}, "late"))
            return used(conn, purpose)
        with store.database_lifecycle(self.s.db_path), mock.patch.object(budget, "_purpose_used", settle_midway):
            report = budget.report(self.s.db_path)
        self.assertEqual(len(settled), 1)
        total = report["total"]
        self.assertEqual(report["watermark"]["ledger_revision"], before["ledger_revision"])
        self.assertEqual((total["spent_micro_usd"], total["pending_micro_usd"]),
                         (before["spent_micro_usd"], before["pending_micro_usd"]))
        self.assertEqual(sum(m["settled_micro_usd"] for m in report["members"].values()) + total["adjustments_micro_usd"],
                         total["spent_micro_usd"])
        self.assertEqual(sum(m["pending_micro_usd"] for m in report["members"].values()), total["pending_micro_usd"])
        self.assertEqual(sum(c["used_micro_usd"] for c in report["categories"].values()),
                         total["spent_micro_usd"] - total["adjustments_micro_usd"] + total["pending_micro_usd"])
        with store.database_lifecycle(self.s.db_path):  # the next report sees the settlement whole
            after = budget.report(self.s.db_path)
        self.assertNotEqual(after["watermark"]["ledger_revision"], before["ledger_revision"])
        self.assertEqual(sum(m["settled_micro_usd"] for m in after["members"].values())
                         + after["total"]["adjustments_micro_usd"], after["total"]["spent_micro_usd"])

    def test_a_tampered_backup_fails_its_check(self):
        evaluation.assign_families(self.s)
        out = release.backup(self.s, self.root / "backup-2", "owner")
        copied = next((self.root / "backup-2" / "files").rglob("families.json"))
        copied.write_text("{}", encoding="utf-8")
        report = release.restore_check(self.s, Path(out["manifest"]))
        self.assertFalse(report["passed"])
        self.assertEqual([k for k, ok in report["checks"].items() if not ok],
                         [f"mutable file {copied.relative_to(self.root / 'backup-2' / 'files').as_posix()}"])


class ReconciliationFixtureTest(unittest.TestCase):
    def test_late_usage_after_reconciliation_is_not_counted_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = fixtures.make_env(Path(tmp), index=False)
            db = env.settings.db_path
            attempt = _unknown_attempt(env.settings, tokens=600)  # 600 input tokens at $0.10/M = 60 micro-USD
            with store.open_db(db) as conn:
                dispatched = conn.execute("SELECT dispatched_at FROM attempts WHERE attempt_id = ?",
                                          (attempt,)).fetchone()[0]
            budget.reconcile(db, "owner", "day-1", "2026-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00", 60,
                             "academy-project", "export.csv", [attempt], unscoped="include")
            self.assertLessEqual("2026-01-01", dispatched)
            snap = budget.snapshot(db)
            self.assertEqual((snap.spent_micro_usd, snap.pending_micro_usd), (60, 0))
            budget.settle(db, attempt, {"prompt_tokens": 600, "completion_tokens": 0}, None)
            budget.settle(db, attempt, {"prompt_tokens": 600, "completion_tokens": 0}, None)  # duplicate completion
            snap = budget.snapshot(db)
            self.assertEqual((snap.spent_micro_usd, snap.pending_micro_usd), (60, 0))
            with store.open_db(db) as conn:  # the late usage is on record as its own compensating adjustment
                late = conn.execute("SELECT amount_micro_usd, actor FROM adjustments WHERE correction_key = ?",
                                    (f"late-settlement:{attempt}",)).fetchall()
            self.assertEqual([tuple(r) for r in late], [(-60, "system")])
            watermark = budget.report(db)["watermark"]
            self.assertEqual((watermark["reconciliation_id"], watermark["reconciled_through"], watermark["scope"],
                              watermark["unresolved_micro_usd"]),
                             ("day-1", "2099-01-01T00:00:00+00:00", "academy-project", 0))


class ReleaseReportTest(unittest.TestCase):
    def test_status_rules(self):
        ok = [("a", True, "")]
        self.assertEqual(release.decide_status([("a", False, "x")], ok, "sealed gold")[0], "blocked")
        self.assertEqual(release.decide_status(ok, ok, "sealed gold"), ("ready", []))
        status, reasons = release.decide_status(ok, ok, "development pilot")
        self.assertEqual(status, "limited")
        self.assertIn("development pilot", reasons[0])
        self.assertEqual(release.decide_status([("a", None, "")], ok, "sealed gold")[0], "limited")
        self.assertEqual(release.decide_status(ok, [("q", None, "")], "sealed gold")[0], "limited")

    def test_a_draft_report_lists_what_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = fixtures.make_env(Path(tmp))
            path = release.write_release_report(env.settings, "latest")
            text = path.read_text(encoding="utf-8")
            self.assertIn("draft-", path.parent.name)
            self.assertIn("## Decision: **limited**", text)
            self.assertIn("no saved check results", text)
            self.assertIn("no answer run recorded", text)
            for name in ("manifest", "coverage", "evaluation", "budget"):
                self.assertTrue((path.parent / f"{name}.json").exists())
            manifest = json.loads((path.parent / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "limited")

    def _sealed_release(self, root):
        """K1 activated on the phase-4 fixtures, the development finalists run, both sets frozen, the release frozen
        and its one sealed run, then a fake latency sample (whose reuse and an eighth user are refused). Returns the
        settings, the transport, the freeze and the latency sample."""
        env = p4.make_env(root, splits={"기관A": "dev", "기관F": "dev", "기관D": "test", "기관E": "test"})
        s = env.settings
        p4.write(env, "dev", [p4.amount_row(env), p4.deadline_row(env), p4.warranty_row(env),
                              p4.absent_row(env)])
        p4.write(env, "test", [p4.row(env, "test-seat", "도서관에서 무엇을 만드는 사업인가요?", "기관D", split="test",
                                      groups=[p4.group(env, "g1", "기관D", ("%좌석%", p4.SEATS))],
                                      claims=[p4.claim("c1", ["g1"], {"type": "text", "patterns": ["좌석 예약"]})])])
        (k1,) = evaluation.evaluate_retrieval(s, fixtures.analyzer(), None, "dev", ["K1"])
        decision = root / "decision.json"
        decision.write_text(json.dumps({"run_id": k1["run_id"], "mode": "kiwi_bm25", "decided_by": "owner",
                                        "rationale": "baseline"}), encoding="utf-8")
        evaluation.activate_run(s, k1["run_id"], decision)
        transport = FakeTransport()
        res = service.Resources(s, transport=transport, recover=True)
        try:
            est = answers.plan_run(s, "answer-finalists", "dev", [k1["run_id"]])
            dev = answers.run_answers(s, res, est["estimate_id"], "owner")
            evaluation.freeze_dataset(s, "dev", "owner", "dev")
            evaluation.freeze_dataset(s, "test", "owner", "test")
            freeze = sealed.freeze_release(s, auth.OWNER_CLI, k1["run_id"], dev["run_id"], "owner", "K1")
            est = answers.plan_run(s, "sealed", freeze_id=freeze["freeze_id"])
            answers.run_answers(s, res, est["estimate_id"], "owner")
            lat = answers.plan_latency(s, waves=2, users=2)
            latency = answers.latency_run(s, res, lat["estimate_id"], "owner")
            with self.assertRaisesRegex(answers.AnswerEvalError, "already used"):
                answers.latency_run(s, res, lat["estimate_id"], "owner")
            with self.assertRaises(answers.AnswerEvalError):
                answers.plan_latency(s, waves=1, users=7)
        finally:
            res.close()
        return s, transport, freeze, latency

    def test_the_report_reads_a_sealed_release_without_calling_the_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            s, transport, freeze, latency = self._sealed_release(Path(tmp))
            self.assertEqual((latency["provider"], latency["n"], latency["failures"]), ("fake", 4, 0))
            calls = len(transport.calls)
            path = release.write_release_report(s, "latest")
            self.assertEqual(len(transport.calls), calls)
            self.assertEqual(path.parent.name, freeze["freeze_id"])
            text = path.read_text(encoding="utf-8")
            self.assertIn("sealed run `S-", text)
            self.assertIn("evidence is sealed pilot", text)  # a 1-row test set is never called gold
            self.assertIn("fake sample", text)
            self.assertIn("no real latency sample", text)
            evaluation_json = json.loads((path.parent / "evaluation.json").read_text(encoding="utf-8"))
            self.assertEqual(len(evaluation_json["sealed_runs"]), 1)
            self.assertNotIn("도서관에서 무엇을", text)  # no sealed question text in the report
            # review round 3 (F6): saved checks and answer evidence count only for the current candidate
            checks = s.data_dir / "releases" / "checks"
            checks.mkdir(parents=True, exist_ok=True)
            current = evaluation.code_fingerprint()
            saved = {"phase": "all", "ok": True, "tests_run": 243, "recorded_at": "2026-10-01T00:00:00+00:00"}
            (checks / "check-all.json").write_text(json.dumps({**saved, "code": {"source_sha256": "f" * 64}}),
                                                    encoding="utf-8")
            manifest = lambda: json.loads((release.write_release_report(s, "latest").parent / "manifest.json")  # noqa: E731
                                          .read_text(encoding="utf-8"))
            m = manifest()
            self.assertTrue(any("saved check-all" in x for x in m["stale_evidence"]))
            self.assertIn("hard check not verified: automated invariants", " ".join(m["reasons"]))
            (checks / "check-all.json").write_text(json.dumps({**saved, "code": current}), encoding="utf-8")
            m = manifest()
            self.assertNotIn("automated invariants", " ".join(m["reasons"]))
            self.assertEqual(m["evidence_label"], "sealed pilot")
            with mock.patch.object(generation, "PROMPT_VERSION", "grounded-answer-changed"):
                m = manifest()
            self.assertEqual(m["evidence_label"], "no answer evaluation for the current candidate")
            self.assertTrue(any(x.startswith("sealed run S-") for x in m["stale_evidence"]))
            self.assertTrue(any(x.startswith("development answer run A-") for x in m["stale_evidence"]))
            self.assertNotEqual(m["status"], "ready")
            self.assertEqual(len(json.loads((path.parent / "evaluation.json").read_text(encoding="utf-8"))
                                 ["sealed_runs"]), 1)  # the old result stays on record


class CheckCommandTest(unittest.TestCase):
    def test_unknown_phase_is_refused(self):
        self.assertEqual(cli.main(["check", "--phase", "9", "--provider", "fake"]), 2)
        self.assertEqual(cli.main(["check", "--phase", "4", "--provider", "openai"]), 2)


if __name__ == "__main__":
    unittest.main()
