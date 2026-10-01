"""Backup and staged restore, the phase-5 reconciliation fixture and the read-only release report."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from rfp_assistant import answers, auth, budget, cli, dense, evaluation, generation, release, sealed, service, store
from rfp_assistant.contracts import AnswerRequest
from rfp_assistant.generation import FakeTransport
from rfp_assistant.settings import DEFAULT_RATES
from tests import fixtures
from tests import phase4_fixtures as p4


def _configure(settings, prior_use: int) -> None:
    budget.configure(settings.db_path, "owner", project_start=date(2026, 9, 30), project_end=date(2026, 10, 28),
                     prior_use_micro=prior_use, prior_use_evidence="dashboard 2026-09-30", rates=DEFAULT_RATES,
                     rate_version="fixture", enable_paid=True)


def _unknown_attempt(settings, tokens: int = 600) -> str:
    request_id = dense.ensure_job_request(settings, "owner-cli", f"job-{tokens}", {"job": "test"})
    admission = budget.reserve(settings.db_path, request_id=request_id, member_id="owner-cli", stage="generation",
                               purpose="interactive", model="gpt-6-luna", input_tokens=tokens, max_output_tokens=0,
                               count_method="test")
    budget.mark_dispatching(settings.db_path, admission["attempt_id"])
    budget.mark_unknown(settings.db_path, admission["attempt_id"], "timeout (test)")
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

    def tearDown(self):
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
        report = release.restore_check(self.s, Path(out["manifest"]), self.root / "staging")
        self.assertTrue(report["passed"], {k: v for k, v in report["checks"].items() if not v["ok"]})
        restored = report["restored_ledger"]
        self.assertEqual(restored["prior_use_micro_usd"], 250_000)
        self.assertEqual((restored["spent_micro_usd"], restored["pending_micro_usd"], restored["available_micro_usd"]),
                         (before["spent_micro_usd"], before["pending_micro_usd"], before["available_micro_usd"]))
        self.assertGreater(restored["unknown_micro_usd"], 0)
        self.assertFalse(restored["paid_enabled"])
        self.assertTrue(budget.snapshot(self.s.db_path).paid_enabled)  # staging never touches the live setting
        self.assertEqual(report["after_restart_recovery"]["unknown_micro_usd"], restored["unknown_micro_usd"])
        self.assertTrue(report["checks"]["active index and row mapping"]["ok"])
        self.assertEqual(len(list((self.s.data_dir / "releases" / "restore-checks").glob("*.json"))), 1)
        with self.assertRaises(release.ReleaseError):  # staging inside the live runtime
            release.restore_check(self.s, Path(out["manifest"]), self.s.data_dir / "stage")

    def test_a_tampered_backup_fails_its_check(self):
        evaluation.assign_families(self.s)
        out = release.backup(self.s, self.root / "backup-2", "owner")
        copied = next((self.root / "backup-2" / "files").rglob("families.json"))
        copied.write_text("{}", encoding="utf-8")
        report = release.restore_check(self.s, Path(out["manifest"]))
        self.assertFalse(report["passed"])
        self.assertFalse(report["checks"]["copied datasets, sealed files, runs and releases"]["ok"])


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
                             "academy-project", "export.csv", [attempt])
            self.assertLessEqual("2026-01-01", dispatched)
            snap = budget.snapshot(db)
            self.assertEqual((snap.spent_micro_usd, snap.pending_micro_usd), (60, 0))
            budget.settle(db, attempt, {"prompt_tokens": 600, "completion_tokens": 0}, None)
            budget.settle(db, attempt, {"prompt_tokens": 600, "completion_tokens": 0}, None)  # duplicate completion
            snap = budget.snapshot(db)
            self.assertEqual((snap.spent_micro_usd, snap.pending_micro_usd), (60, 0))


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

    def test_the_report_reads_a_sealed_release_without_calling_the_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
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
