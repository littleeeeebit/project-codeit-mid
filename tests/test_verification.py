"""The managed verification contract (`verification.json`) and its runner (`tools/verify.py`)."""

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import fixtures

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("verify", REPO / "tools" / "verify.py")
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)

FORBIDDEN = ("--allow-paid-queries", "build-dense", "trial-reranker", "activate-run", "configure-budget",
             "--provider=openai", "openai")


class ContractTest(unittest.TestCase):
    def setUp(self):
        self.contract = verify.load_contract()
        self.commands = {c["id"]: c for c in self.contract["commands"]}

    def test_referenced_contracts_and_sections_exist(self):
        docs = {}
        for c in self.contract["contracts"]:
            path = REPO / c["path"]
            self.assertTrue(path.exists(), c["path"])
            headings = {h.strip() for h in re.findall(r"^#+ (.+)$", path.read_text(encoding="utf-8"), re.M)}
            self.assertFalse(set(c["sections"]) - headings, c["path"])
            docs[c["id"]] = headings
        for flow in self.contract["flows"]:
            for ref in flow["contracts"]:
                doc, _, section = ref.partition("#")
                self.assertIn(doc, docs, ref)
                self.assertTrue(not section or section in docs[doc], ref)

    def test_every_test_id_loads_and_every_command_is_safe_and_used(self):
        used = {c for f in self.contract["flows"] for c in f["commands"]}
        self.assertEqual(set(self.commands), used)  # no orphan command, no unknown reference
        for c in self.contract["commands"]:
            if "tests" in c:
                suite = unittest.defaultTestLoader.loadTestsFromNames(c["tests"])
                self.assertGreater(suite.countTestCases(), 0, c["id"])
            else:
                self.assertIn(c["argv"][0], ("{python}", "git"), c["id"])
                if "-m" in c["argv"] and "rfp_assistant.cli" in c["argv"]:
                    self.assertIn("fake", c["argv"], c["id"])
                for script in [a for a in c["argv"] if a.endswith(".py")]:
                    self.assertTrue((REPO / script).exists(), script)
            text = json.dumps(c).lower()
            self.assertFalse([f for f in FORBIDDEN if f in text], c["id"])

    def test_every_automated_gate_scenario_of_the_plan_is_mapped(self):
        plan = (REPO / "docs/plan/end-to-end/3-workflows-and-operations.md").read_text(encoding="utf-8")
        gate = plan.split("## Automated gate", 1)[1].split("\n## ", 1)[0]
        cells = [r.split("|")[1].strip() for r in gate.splitlines() if r.startswith("| ")]
        rows = [c for c in cells[1:] if not c.startswith("---")]  # minus header and separator
        mapping = self.contract["automated_gate_scenarios"]
        self.assertEqual(sorted(rows), sorted(mapping))
        for ids in mapping.values():
            self.assertTrue(ids and set(ids) <= set(self.commands), ids)

    def test_every_flow_is_checked_and_manual_steps_are_complete(self):
        ids = [m["id"] for f in self.contract["flows"] for m in f["manual"]]
        self.assertEqual(len(ids), len(set(ids)))
        for flow in self.contract["flows"]:
            self.assertTrue(flow["commands"] or flow["manual"], flow["id"])
            for m in flow["manual"]:
                self.assertIn(m["scope"], ("fixture", "real-corpus"))
                self.assertTrue(m["do"] and m["expect"])

    def test_local_settings_and_receipts_are_ignored_by_git(self):
        for path in (self.contract["local_settings_file"], self.contract["receipts_dir"] + "/x/receipt.json"):
            out = subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO)
            self.assertEqual(out.returncode, 0, path)


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.contract = {"local_settings_file": "verification.local.json", "receipts_dir": "receipts",
                         "commands": [
                             {"id": "ok", "argv": ["{python}", "-c", "import os; assert 'OPENAI_API_KEY' not in os.environ"],
                              "timeout_s": 60, "purpose": "ok"},
                             {"id": "bad", "argv": ["{python}", "-c", "raise SystemExit(3)"], "timeout_s": 60,
                              "purpose": "bad"},
                             {"id": "rc", "argv": ["{python}", "-c", "0"], "timeout_s": 60, "purpose": "rc",
                              "requires": ["real_corpus"]}],
                         "flows": [{"id": "A", "title": "a", "commands": ["ok", "rc"],
                                    "manual": [{"id": "A-m1", "scope": "fixture", "do": "d", "expect": "e"}]},
                                   {"id": "B", "title": "b", "commands": ["bad"], "manual": []}]}
        self.path = self.repo / "verification.json"
        self.path.write_text(json.dumps(self.contract), encoding="utf-8")
        self.sha = verify.contract_sha256(self.path)
        self.local = {"approval": {"approved_by": "owner", "contract_sha256": self.sha, "commands": ["ok", "rc", "bad"]}}

    def tearDown(self):
        self.tmp.cleanup()

    def run_flows(self, local, flows):
        os.environ["OPENAI_API_KEY"] = "sk-test-never-passed"
        try:
            return verify.run(self.contract, local, flows=flows, commands=[], repo=self.repo,
                              contract_path=self.path)
        finally:
            del os.environ["OPENAI_API_KEY"]

    def test_nothing_runs_without_a_matching_approval(self):
        for local in ({}, {"approval": {**self.local["approval"], "approved_by": ""}},
                      {"approval": {**self.local["approval"], "contract_sha256": "0" * 64}},
                      {"approval": {**self.local["approval"], "commands": ["ok"]}}):
            with self.assertRaises(verify.VerifyError):
                self.run_flows(local, ["A"])
        self.assertFalse((self.repo / "receipts").exists())

    def test_receipt_records_commands_flows_and_pending_manual_steps(self):
        receipt = json.loads((self.run_flows(self.local, []) / "receipt.json").read_text(encoding="utf-8"))
        status = {c["id"]: c["status"] for c in receipt["commands"]}
        self.assertEqual(status, {"ok": "pass", "rc": "skipped", "bad": "fail"})  # no key leaked: 'ok' passed
        flows = {f["id"]: f["automated"] for f in receipt["flows"]}
        self.assertEqual(flows, {"A": "incomplete", "B": "fail"})
        self.assertEqual(receipt["summary"]["automated"], "fail")
        self.assertEqual(receipt["flows"][0]["manual"][0]["result"], "pending")
        self.assertFalse(receipt["summary"]["complete"])
        self.assertEqual((receipt["contract_sha256"], receipt["approved_by"], receipt["paid_calls"]),
                         (self.sha, "owner", 0))

    def test_manual_results_must_match_the_commit_and_be_observed(self):
        run_dir = self.run_flows(self.local, ["A"])
        template = json.loads((run_dir / "manual-template.json").read_text(encoding="utf-8"))
        results = self.repo / "manual.json"

        def record(**change):
            data = json.loads(json.dumps(template))
            data.update(observer="kim")
            data["steps"]["A-m1"].update(result="pass", observed="no login form", at="2026-10-01T09:00:00Z")
            for k, v in change.items():
                data[k] = v
            results.write_text(json.dumps(data), encoding="utf-8")
            return verify.record_manual(run_dir, results)

        with self.assertRaises(verify.VerifyError):
            record(head="another-commit")
        with self.assertRaises(verify.VerifyError):
            record(observer="")
        summary = record()
        self.assertEqual(summary["manual"], {"pass": 1})
        receipt = json.loads((run_dir / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["flows"][0]["manual"][0]["observer"], "kim")

    def test_the_authoritative_runtime_is_never_a_real_corpus_copy(self):
        base = {"source_dir": "/x/src", "hwp_doc_id": "h", "pdf_doc_id": "p", "question": "q"}
        for rc in ({**base, "data_dir": str(self.repo / ".runtime"), "isolated_copy": True},
                   {**base, "data_dir": "/copy/data", "isolated_copy": False},
                   {**base, "data_dir": "relative/data", "isolated_copy": True}):
            with self.assertRaises(verify.VerifyError):
                verify.real_corpus({"real_corpus": rc}, self.repo)


class ScriptTest(unittest.TestCase):
    """The executable checks themselves, against the fixture corpus."""

    def env(self, run_dir: Path) -> dict:
        return verify.child_env({}, run_dir, REPO)

    @unittest.skipIf(sys.platform == "win32", "the runner exercises CTRL_C_EVENT on Windows")
    def test_signal_stop_script_reports_an_interrupted_request_without_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            out = subprocess.run([sys.executable, "-B", "tools/verification/signal_stop.py", str(run_dir)],
                                 cwd=REPO, env=self.env(run_dir), capture_output=True, text=True, timeout=300)
            result = json.loads((run_dir / "signal-stop.json").read_text())
            self.assertEqual(out.returncode, 0, result)
            self.assertEqual((result["status"], result["attempts"], result["signal"]), ("interrupted", [], "SIGINT"))

    def test_real_corpus_script_on_a_fixture_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            env = fixtures.make_env(Path(tmp) / "copy")
            a, d = env.refs["기관A"], env.refs["기관D"]
            (run_dir / "inputs.json").write_text(json.dumps({"real_corpus": {
                "source_dir": str(env.settings.source_dir), "data_dir": str(env.settings.data_dir),
                "isolated_copy": True, "hwp_doc_id": a.doc_id, "pdf_doc_id": d.doc_id, "question": "시스템 구축"}}))
            out = subprocess.run([sys.executable, "-B", "tools/verification/real_corpus_frozen.py", str(run_dir)],
                                 cwd=REPO, env=self.env(run_dir), capture_output=True, text=True, timeout=600)
            report = json.loads((run_dir / "real-corpus-frozen.json").read_text(encoding="utf-8"))
            self.assertEqual(out.returncode, 0, json.dumps(report, ensure_ascii=False)[:2000] + out.stderr[-2000:])
            self.assertEqual([c["case"] for c in report["cases"]], ["hwp_single", "pair", "pair_whitespace_units_2"])


if __name__ == "__main__":
    unittest.main()
