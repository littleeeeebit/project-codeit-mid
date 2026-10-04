"""The verification manifest (`verification.json`) and its executable flows (`tools/verify.py`).

The manifest models below restate the local verification service's schema as the reviewer described it (extra
fields forbidden, lowercase IDs, `api`/`browser`/`command` kinds, a command, impact paths and assertions per flow).
"""

import importlib.util
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Literal
from unittest import mock

import psycopg
from pydantic import BaseModel, ConfigDict, Field

from tests import fixtures

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("verify", REPO / "tools" / "verify.py")
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)

ID = r"^[a-z0-9][a-z0-9-]{0,63}$"
FORBIDDEN = ("--allow-paid-queries", "build-dense", "trial-reranker", "activate-run", "configure-budget", "openai")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Assertion(_Strict):
    id: str = Field(pattern=ID)
    expected: str = Field(min_length=1)


class Flow(_Strict):
    id: str = Field(pattern=ID)
    title: str = Field(min_length=1)
    kind: Literal["api", "browser", "command"]
    command: str = Field(min_length=1)
    paths: list[str] = Field(min_length=1)
    environments: list[str]
    assertions: list[Assertion] = Field(min_length=1)


class Manifest(_Strict):
    version: Literal[1]
    contracts: list[str] = Field(min_length=1)
    prose_paths: list[str]
    flows: list[Flow] = Field(min_length=1)


def fenced(out: str, info: str) -> list[dict]:
    return [json.loads(b) for b in re.findall(rf"```{info}\n(.*?)\n```", out, re.S)]


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads((REPO / "verification.json").read_text(encoding="utf-8"))
        self.manifest = Manifest.model_validate(self.raw)

    def test_paths_are_committed_and_ids_unique(self):
        tracked = set(subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True).stdout.split())
        for path in self.manifest.contracts + self.manifest.prose_paths:
            self.assertIn(path, tracked, path)
        ids = [f.id for f in self.manifest.flows]
        self.assertEqual(len(ids), len(set(ids)))
        for f in self.manifest.flows:
            self.assertEqual(len({a.id for a in f.assertions}), len(f.assertions), f.id)
            for path in f.paths:
                if "*" not in path:
                    self.assertTrue((REPO / path).exists(), f"{f.id}: {path}")

    def test_every_flow_runs_through_the_runner_and_observes_each_assertion(self):
        for f in self.manifest.flows:
            self.assertEqual(f.command, f"python -B tools/verify.py {f.id}")
            self.assertIn(f.id, verify.FLOWS)
            source = inspect.getsource(verify.FLOWS[f.id])
            for a in f.assertions:
                self.assertIn(f'"{a.id}"', source, f"{f.id} never observes {a.id}")
            self.assertFalse([x for x in FORBIDDEN if x in json.dumps(self.raw["flows"]).lower()])
        self.assertEqual(set(verify.FLOWS), {f.id for f in self.manifest.flows})

    def test_owner_settings_and_secrets_are_ignored_by_git(self):
        for path in (".wiki/verification.local.json", ".wiki/verification-receipts/x.json", ".env"):
            self.assertEqual(subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO).returncode, 0, path)


class EvidenceTest(unittest.TestCase):
    spec = {"id": "x", "kind": "browser", "assertions": [{"id": "a", "expected": "A"}, {"id": "b", "expected": "B"}]}

    def test_a_flow_prints_exactly_one_local_evidence_block_last(self):
        env = {**os.environ, "WIKI_VERIFICATION_ENVIRONMENT": "env-1", "WIKI_VERIFICATION_SCOPE": "scope-1",
               "WIKI_VERIFICATION_HEAD": verify.git_head(), "OPENAI_API_KEY": "sk-never-passed"}
        out = subprocess.run([sys.executable, "-B", "tools/verify.py", "budget-recovery"], cwd=REPO, env=env,
                             capture_output=True, text=True, timeout=600).stdout
        tail = "\n".join(out.splitlines()[-80:])  # what the service keeps of a command's output
        [block] = fenced(tail, "local-evidence")
        self.assertEqual(fenced(out, "local-evidence"), [block])
        self.assertTrue(out.rstrip().endswith("```"))
        last = out.rstrip().splitlines()[-3:]  # opening fence, one compact JSON line, closing fence
        self.assertEqual((last[0], last[2], json.loads(last[1])), ("```local-evidence", "```", block))
        self.assertEqual(set(block), {"head", "flow", "environment_id", "test_scope", "observations"})
        self.assertEqual((block["flow"], block["environment_id"], block["test_scope"], block["head"]),
                         ("budget-recovery", "env-1", "scope-1", verify.git_head()))
        [obs] = block["observations"]
        self.assertEqual(set(obs), {"id", "expected", "actual", "pass"})
        self.assertEqual((obs["id"], obs["pass"]), ("reconciliation", True), obs)
        self.assertRegex(obs["actual"], r"^\d+ tests in .*OK")

    def test_unobserved_assertions_and_another_head_never_pass(self):
        with mock.patch.dict(os.environ, {"WIKI_VERIFICATION_HEAD": "abc"}):  # the service's head, if it runs us
            ev = verify.evidence(self.spec, "abc", {"a": (True, "seen")}, None, "RuntimeError: boom")
        self.assertEqual([(o["id"], o["pass"]) for o in ev["observations"]], [("a", True), ("b", False)])
        self.assertIn("boom", ev["observations"][1]["actual"])
        self.assertEqual(set(ev) - {"head", "flow", "environment_id", "test_scope", "observations"},
                         {"requests", "browser_tool", "build_head", "actions"})
        with mock.patch.dict(os.environ, {"WIKI_VERIFICATION_HEAD": "def"}):
            ev = verify.evidence(self.spec, "abc", {"a": (True, "seen"), "b": (True, "seen")}, None, None)
        self.assertEqual([o["pass"] for o in ev["observations"]], [False, False])


class DatasetCopyTest(unittest.TestCase):
    def test_a_configured_corpus_is_used_through_an_isolated_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = fixtures.make_env(Path(tmp))
            data = env.settings.data_dir
            source_dsn = os.environ[env.settings.database_dsn_env]
            (data / "indexes").mkdir(exist_ok=True)
            with mock.patch.dict(os.environ, {"RFP_SOURCE_DIR": str(env.settings.source_dir),
                                              "RFP_DATA_DIR": str(data),  # not a checkout .env that may exist
                                              "RFP_DATABASE_DSN": source_dsn,
                                              "RFP_VERIFY_ENV_FILE": str(Path(tmp) / "no-env-file")}):
                ctx = verify.Context("t")
                corpus = ctx.dataset()
            try:
                self.assertTrue(corpus["kind"].startswith("configured"))
                copy = Path(corpus["data_dir"])
                self.assertNotEqual(copy.resolve(), data.resolve())
                self.assertEqual(ctx.env["RFP_DATABASE_DSN"], corpus["dsn"])  # what the served app and tools use
                self.assertNotEqual(corpus["dsn"], source_dsn)
                with psycopg.connect(corpus["dsn"], autocommit=True) as conn:
                    self.assertEqual(conn.execute("SELECT count(*) FROM documents").fetchone()[0], 4)
                    conn.execute("INSERT INTO audit_events(event_id, actor, action, target, reason, details_json, "
                                 "created_at) VALUES ('e', 'a', 'x', 't', 'r', '{}', 'now')")
                with psycopg.connect(source_dsn) as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM audit_events WHERE event_id = 'e'")
                                     .fetchone()[0], 0)
                self.assertEqual((copy / "indexes").resolve(), (data / "indexes").resolve())
            finally:
                ctx.close()
            self.assertFalse((copy / "indexes").exists())  # the link is gone ...
            self.assertTrue((data / "indexes").is_dir())  # ... and what it pointed to is not


class OwnerConfigTest(unittest.TestCase):
    """The service copies the owner's env_file to the checkout's `.env` and does not export it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "checkout"
        self.repo.mkdir()
        self.env = fixtures.make_env(Path(self.tmp.name) / "corpus")
        self.dsn = os.environ[self.env.settings.database_dsn_env]  # the env clean below drops RFP_* keys
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("RFP_", "WIKI_VERIFICATION_"))}
        self.patches = [mock.patch.dict(os.environ, clean, clear=True), mock.patch.object(verify, "REPO", self.repo)]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def write_env(self, **values):
        lines = ["# owner env_file", "OPENAI_API_KEY=sk-must-not-be-read"]
        lines += [f'{k}="{v}"' for k, v in values.items()]
        (self.repo / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_the_copied_env_file_alone_selects_the_corpus_and_origin(self):
        self.write_env(RFP_SOURCE_DIR=self.env.settings.source_dir, RFP_DATA_DIR=self.env.settings.data_dir,
                       RFP_DATABASE_DSN=self.dsn,
                       RFP_VERIFY_ORIGIN="http://127.0.0.1:8799")
        ctx = verify.Context("t")
        try:
            self.assertNotIn("OPENAI_API_KEY", ctx.config)
            self.assertEqual((ctx.config["RFP_VERIFY_ORIGIN"], ctx.config_source["RFP_DATA_DIR"]),
                             ("http://127.0.0.1:8799", ".env"))
            corpus = ctx.dataset()
            self.assertEqual(corpus["kind"], "configured corpus from .env (isolated copy)")
            self.assertEqual(corpus["source_dir"], str(self.env.settings.source_dir.resolve()))
            self.assertNotIn("OPENAI_API_KEY", ctx.env)
        finally:
            ctx.close()

    def test_the_env_file_wins_over_an_inherited_variable(self):
        self.write_env(RFP_SOURCE_DIR=self.env.settings.source_dir, RFP_DATA_DIR=self.env.settings.data_dir)
        with mock.patch.dict(os.environ, {"RFP_DATA_DIR": "/somewhere/else"}):
            values, source = verify.owner_config()
        self.assertEqual((values["RFP_DATA_DIR"], source["RFP_DATA_DIR"]), (str(self.env.settings.data_dir), ".env"))

    def test_missing_prerequisites_fail_instead_of_falling_back_to_fixtures(self):
        cases = [({"RFP_SOURCE_DIR": self.env.settings.source_dir}, "RFP_DATA_DIR, RFP_DATABASE_DSN not set"),
                 ({"RFP_SOURCE_DIR": self.env.settings.source_dir, "RFP_DATA_DIR": self.env.settings.data_dir},
                  "RFP_DATABASE_DSN not set"),
                 ({"RFP_SOURCE_DIR": self.env.settings.source_dir, "RFP_DATA_DIR": Path(self.tmp.name) / "none",
                   "RFP_DATABASE_DSN": self.dsn}, "data dir missing"),
                 ({}, "dataset flow without a corpus")]
        for values, message in cases:
            self.write_env(**values)
            with mock.patch.dict(os.environ, {"WIKI_VERIFICATION_ENVIRONMENT": "owner-env"}):
                ctx = verify.Context("t")
                try:
                    with self.assertRaisesRegex(RuntimeError, message):
                        ctx.dataset()
                finally:
                    ctx.close()


def _browser_available() -> bool:
    if importlib.util.find_spec("playwright") is None:
        return False
    exe = os.environ.get("RFP_VERIFY_BROWSER_EXECUTABLE", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    return Path(exe).exists()


@unittest.skipUnless(_browser_available(), "Playwright and a Chromium binary are needed for browser flows")
class BrowserFlowTest(unittest.TestCase):
    def test_consultant_answer_flow_records_requests_actions_and_the_served_build(self):
        env = {**os.environ, "RFP_VERIFY_BROWSER_EXECUTABLE": os.environ.get(
            "RFP_VERIFY_BROWSER_EXECUTABLE", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"),
            "RFP_VERIFY_ORIGIN": "http://127.0.0.1:8779"}
        # Hermetic: the fixture corpus, whatever the checkout's .env or the calling service configured.
        env = {k: v for k, v in env.items() if not k.startswith("WIKI_VERIFICATION_")
               and k not in ("RFP_SOURCE_DIR", "RFP_DATA_DIR")}
        env["RFP_VERIFY_ENV_FILE"] = str(REPO / ".runtime" / "no-such-env-file")
        proc = subprocess.run([sys.executable, "-B", "tools/verify.py", "consultant-answer"], cwd=REPO, env=env,
                              capture_output=True, text=True, timeout=900)
        [block] = fenced("\n".join(proc.stdout.splitlines()[-80:]), "local-evidence")  # the service's tail
        self.assertEqual(proc.returncode, 0, json.dumps(block["observations"], ensure_ascii=False))
        self.assertEqual(block["build_head"], verify.git_head())
        self.assertEqual({a["action"] for a in block["actions"]} >= {"click a citation chip once"}, True)
        self.assertTrue(block["requests"] and all(r["url"].startswith("http://127.0.0.1:8779")
                                                  for r in block["requests"]))
        self.assertTrue(all(set(r) == {"method", "url", "status"} for r in block["requests"]))


class ScriptTest(unittest.TestCase):
    """The scripts the flows call, against the fixture corpus."""

    def env(self) -> dict:
        return {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO / "src"), str(REPO)])}

    @unittest.skipIf(sys.platform == "win32", "the controlled-stop flow exercises CTRL_C_EVENT on Windows")
    def test_signal_stop_script_reports_an_interrupted_request_without_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            out = subprocess.run([sys.executable, "-B", "tools/verification/signal_stop.py", str(run_dir)],
                                 cwd=REPO, env=self.env(), capture_output=True, text=True, timeout=300)
            result = json.loads((run_dir / "signal-stop.json").read_text())
            self.assertEqual(out.returncode, 0, result)
            self.assertEqual((result["status"], result["attempts"], result["signal"]), ("interrupted", [], "SIGINT"))

    def test_frozen_generation_script_on_a_fixture_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            env = fixtures.make_env(Path(tmp) / "copy")
            a, d = env.refs["기관A"], env.refs["기관D"]
            (run_dir / "inputs.json").write_text(json.dumps({"real_corpus": {
                "source_dir": str(env.settings.source_dir), "data_dir": str(env.settings.data_dir),
                "isolated_copy": True, "hwp_doc_id": a.doc_id, "pdf_doc_id": d.doc_id, "question": "시스템 구축"}}))
            copy = {"RFP_DATABASE_DSN": os.environ[env.settings.database_dsn_env]}  # what tools/verify.py passes
            out = subprocess.run([sys.executable, "-B", "tools/verification/real_corpus_frozen.py", str(run_dir)],
                                 cwd=REPO, env={**self.env(), **copy}, capture_output=True, text=True, timeout=600)
            report = json.loads((run_dir / "real-corpus-frozen.json").read_text(encoding="utf-8"))
            self.assertEqual(out.returncode, 0, json.dumps(report, ensure_ascii=False)[:2000] + out.stderr[-2000:])
            self.assertEqual([c["case"] for c in report["cases"]], ["hwp_single", "pair", "pair_whitespace_units_2"])


if __name__ == "__main__":
    unittest.main()
