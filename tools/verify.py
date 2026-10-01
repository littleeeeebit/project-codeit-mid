"""Executable flows of `verification.json` for the local verification service.

    python -B tools/verify.py <flow-id>      # runs one flow; its last output is exactly one local-evidence fence
    python -B tools/verify.py --list

Every flow uses the fake provider and never needs an API key: OPENAI_API_KEY is removed from every child process.
Command flows run repository tests and scripts against fixture corpora. Dataset flows (`environments` contains
`dataset`) use RFP_SOURCE_DIR / RFP_DATA_DIR when the service's environment supplies them, through an isolated
copy: the SQLite database is backed up through a read-only connection into a temporary runtime and the read-only
artifact folders are linked, so verifier runs, requests and fake ledger rows never reach the configured runtime.
(Like any reader of a WAL database, that connection may create the empty `-wal`/`-shm` sidecar files; the database
content is not changed.) Without them, the fixture corpus is used and the observation says so.

The service supplies WIKI_VERIFICATION_HEAD, WIKI_VERIFICATION_ENVIRONMENT, WIKI_VERIFICATION_SCOPE and
WIKI_VERIFICATION_BROWSER, and copies the owner's env_file to the checkout's `.env` without exporting it. The
runner therefore reads the supported keys (CONFIG_KEYS) from `.env` first and from the process environment only
for keys `.env` does not set; every observation names where its corpus came from. OPENAI_API_KEY is never read.
Browser flows serve the app on RFP_VERIFY_ORIGIN (default http://127.0.0.1:8765) and drive it with Playwright
(`pip install -e .[verify]`); RFP_VERIFY_BROWSER_EXECUTABLE selects a browser binary, otherwise Playwright's
Chromium and then the installed Chrome are tried. Under the service, a dataset flow without a configured corpus
fails with that prerequisite named instead of silently using fixtures.

The evidence block is printed compactly as the last three lines, so it survives a tail of the output.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import closing
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "src"), str(REPO)]
CONFIG_KEYS = ("RFP_SOURCE_DIR", "RFP_DATA_DIR", "RFP_VERIFY_ORIGIN", "RFP_VERIFY_BROWSER_EXECUTABLE",
               "RFP_VERIFY_QUESTION", "RFP_VERIFY_HWP_DOC_ID", "RFP_VERIFY_PDF_DOC_ID")
TERMINAL = ("completed", "failed", "cancelled", "interrupted")
MANIFEST = REPO / "verification.json"
FAKE_QUESTION = "하자보수 기간은 얼마인가요?"
DATASET_QUESTION = "하자보수 기간과 유지보수 조건을 비교해 주세요."
READ_ONLY_DIRS = ("indexes", "extracted", "recovered", "reviews", "ocr", "datasets")  # linked, never written here
FLOWS: dict = {}


def flow(name):
    def register(fn):
        FLOWS[name] = fn
        return fn
    return register


class Context:
    def __init__(self, flow_id: str):
        self.flow = flow_id
        self.python = sys.executable
        self.work = Path(tempfile.mkdtemp(prefix=f"verify-{flow_id}-"))
        self.links: list[Path] = []
        self.server: subprocess.Popen | None = None
        self.requests: list[dict] = []
        self.actions: list[dict] = []
        self.browser_tool = ""
        self.build_head = ""
        self.config, self.config_source = owner_config()
        self.env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY" and not k.startswith("RFP_")}
        self.env.update(PYTHONPATH=os.pathsep.join([str(REPO / "src"), str(REPO)]), PYTHONDONTWRITEBYTECODE="1",
                        PYTHONUTF8="1")

    # -------------------------------------------------------------- child processes

    def run(self, name: str, argv: list[str], timeout: int = 1800, env: dict | None = None) -> tuple[int, str]:
        log = self.work / f"{name}.log"
        started = time.monotonic()
        try:
            proc = subprocess.run(argv, cwd=REPO, env=env or self.env, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout)
            code, out = proc.returncode, proc.stdout + proc.stderr
        except subprocess.TimeoutExpired as exc:
            code, out = -1, f"timeout after {timeout} s\n{exc.stdout or ''}{exc.stderr or ''}"
        log.write_text(out, encoding="utf-8")
        print(f"[{name}] exit {code} in {time.monotonic() - started:.1f} s (log {log})", flush=True)
        return code, out

    def unit(self, name: str, tests: list[str], timeout: int = 1800) -> tuple[bool, str]:
        code, out = self.run(name, [self.python, "-B", "-m", "unittest", *tests], timeout)
        return code == 0, unittest_summary(out, code)

    def cli(self, name: str, args: list[str], timeout: int = 900) -> tuple[int, str]:
        return self.run(name, [self.python, "-B", "-m", "rfp_assistant.cli", *args], timeout)

    # -------------------------------------------------------------- corpora

    def dataset(self) -> dict:
        """An isolated runtime for the configured corpus, or a fresh fixture corpus when none is configured."""
        source, data = self.config.get("RFP_SOURCE_DIR"), self.config.get("RFP_DATA_DIR")
        origin = self.config_source.get("RFP_DATA_DIR") or self.config_source.get("RFP_SOURCE_DIR")
        if source or data:
            missing = [k for k, v in (("RFP_SOURCE_DIR", source), ("RFP_DATA_DIR", data)) if not v]
            if missing:
                raise RuntimeError(f"incomplete corpus configuration in {origin}: {', '.join(missing)} not set")
            source, data = Path(source).resolve(), Path(data).resolve()
            db = data / "rfp.sqlite3"
            if not source.is_dir() or not db.is_file():
                raise RuntimeError(f"configured corpus from {origin} is unavailable: "
                                   f"{'source dir missing' if not source.is_dir() else db.name + ' missing'}")
            copy = self.work / "dataset-runtime"
            copy.mkdir()
            with closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)) as src, \
                    closing(sqlite3.connect(copy / "rfp.sqlite3")) as dst:
                src.backup(dst)
            for name in READ_ONLY_DIRS:
                if (data / name).is_dir():
                    self.link(data / name, copy / name)
            return {"kind": f"configured corpus from {origin} (isolated copy)", "source_dir": str(source),
                    "data_dir": str(copy), "question": self.config.get("RFP_VERIFY_QUESTION") or DATASET_QUESTION}
        if os.environ.get("WIKI_VERIFICATION_ENVIRONMENT"):
            raise RuntimeError("dataset flow without a corpus: set RFP_SOURCE_DIR and RFP_DATA_DIR in the env_file "
                               "(copied to .env)")
        from tests import fixtures

        env = fixtures.make_env(self.work / "fixture")
        return {"kind": "fixture corpus (no dataset configured)", "source_dir": str(env.settings.source_dir),
                "data_dir": str(env.settings.data_dir), "question": FAKE_QUESTION,
                "pair": (env.refs["기관A"].doc_id, env.refs["기관D"].doc_id), "pair_question": "시스템 구축"}

    def link(self, target: Path, link: Path) -> None:
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(target), str(link))  # needs no privilege, unlike a symlink
        else:
            link.symlink_to(target, target_is_directory=True)
        self.links.append(link)

    # -------------------------------------------------------------- served app

    def serve(self, corpus: dict, delay: float = 1.0) -> str:
        origin = (self.config.get("RFP_VERIFY_ORIGIN") or "http://127.0.0.1:8765").rstrip("/")
        found = re.match(r"https?://([^:/]+):(\d+)$", origin)
        if not found:
            raise RuntimeError(f"RFP_VERIFY_ORIGIN must be scheme://host:port, got {origin!r}")
        host, port = found.groups()
        config = self.work / "fake-config.json"
        config.write_text(json.dumps({"provider": "fake", "fake_delay_seconds": delay}), encoding="utf-8")
        env = {**self.env, "RFP_SOURCE_DIR": corpus["source_dir"], "RFP_DATA_DIR": corpus["data_dir"],
               "RFP_CONFIG_FILE": str(config)}
        log = (self.work / "server.log").open("w", encoding="utf-8")
        self.server = subprocess.Popen(
            [self.python, "-B", "-m", "streamlit", "run", "app.py", f"--server.address={host}",
             f"--server.port={port}", "--server.headless=true", "--server.fileWatcherType=none",
             "--browser.gatherUsageStats=false"], cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.server.poll() is not None:
                raise RuntimeError(f"the app exited with {self.server.returncode}; see {self.work / 'server.log'}")
            try:
                with urllib.request.urlopen(f"{origin}/_stcore/health", timeout=2) as r:
                    if r.status == 200:
                        return origin
            except OSError:
                time.sleep(0.5)
        raise RuntimeError("the app did not become healthy within 90 s")

    def close(self) -> None:
        if self.server is not None and self.server.poll() is None:
            if os.name == "nt":
                self.server.terminate()
            else:
                self.server.send_signal(signal.SIGINT)  # the controlled stop path
            try:
                self.server.wait(30)
            except subprocess.TimeoutExpired:
                self.server.kill()
        for link in self.links:  # remove the links themselves; never what they point to
            if os.name == "nt":
                os.rmdir(link)
            else:
                link.unlink()
        self.links.clear()

    # -------------------------------------------------------------- browser

    def browser(self, playwright):
        exe = self.config.get("RFP_VERIFY_BROWSER_EXECUTABLE")
        if exe:
            browser = playwright.chromium.launch(executable_path=exe)
        else:
            try:
                browser = playwright.chromium.launch()
            except Exception:  # noqa: BLE001 - no bundled Chromium: use the installed Chrome
                browser = playwright.chromium.launch(channel="chrome")
        self.browser_tool = (os.environ.get("WIKI_VERIFICATION_BROWSER")
                             or f"playwright-chromium {browser.version}")
        return browser

    def page(self, browser, origin: str):
        page = browser.new_context(viewport={"width": 1400, "height": 1000}, accept_downloads=True).new_page()
        page.on("response", lambda r: self.requests.append(
            {"method": r.request.method, "url": r.url, "status": r.status}) if r.url.startswith("http") else None)
        return page

    def act(self, action: str, expected: str, fn) -> tuple[bool, str]:
        try:
            ok, actual = fn()
        except Exception as exc:  # noqa: BLE001 - a failed interaction is an observation
            ok, actual = False, f"{type(exc).__name__}: {str(exc)[:300]}"
        self.actions.append({"action": action, "expected": expected, "actual": actual or "(nothing observed)"})
        print(f"[browser] {action}: {'ok' if ok else 'FAILED'} - {actual}", flush=True)
        return ok, actual

    def open_app(self, page, origin: str, name: str) -> tuple[bool, str]:
        page.goto(origin + "/")
        box = page.get_by_label("이름 (사용·검토 기록용)")
        box.wait_for(timeout=60000)
        sidebar = page.locator('section[data-testid="stSidebar"]').inner_text()
        found = re.search(r"빌드 ([0-9a-f]{40}|unknown)", sidebar)
        self.build_head = found.group(1) if found else "not shown"
        login = page.get_by_text("접속 토큰").count() + page.get_by_label("비밀번호").count()
        box.fill(name)
        box.press("Enter")
        page.wait_for_timeout(1200)
        main = page.locator('[data-testid="stMain"]').inner_text()
        return login == 0 and "질문" in main, f"login controls {login}; served build {self.build_head}; consultant page shown"


# ---------------------------------------------------------------- helpers


def unittest_summary(out: str, code: int) -> str:
    ran = re.findall(r"Ran (\d+) tests? in ([\d.]+)s", out)
    final = [line for line in out.splitlines() if line.startswith(("OK", "FAILED"))]
    return (f"{ran[-1][0]} tests in {ran[-1][1]} s: {final[-1] if final else 'no result line'} (exit {code})"
            if ran else f"no unittest summary (exit {code})")


def load_check_summary(code: int, out: str) -> tuple[bool, str]:  # takes Context.cli's result
    try:
        data = json.loads(out[out.index("{"):out.rindex("}") + 1])
    except ValueError:
        return False, f"no JSON result (exit {code})"
    keys = {k: v for k, v in data.items() if isinstance(v, (bool, int, float, str)) and k != "passed"}
    return code == 0 and data.get("passed") is True, f"passed={data.get('passed')} exit {code}; " + json.dumps(
        keys, ensure_ascii=False)[:400]


def db_rows(data_dir: str, sql: str, args=()) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(Path(data_dir) / "rfp.sqlite3")) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, args).fetchall()


def owner_config(repo: Path | None = None) -> tuple[dict, dict]:
    """Supported keys from the copied env_file (`.env`), then the process environment. Returns (values, source)."""
    values, source = {}, {}
    env_file = Path(os.environ.get("RFP_VERIFY_ENV_FILE") or (repo or REPO) / ".env")  # override: tests, manual runs
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.removeprefix("export ").partition("=")
            key, value = key.strip(), value.strip().strip("'\"")
            if key in CONFIG_KEYS and value:
                values[key], source[key] = value, ".env"
    for key in CONFIG_KEYS:
        if key not in values and os.environ.get(key):
            values[key], source[key] = os.environ[key], "the process environment"
    return values, source


def wait_request(data_dir: str, where: str, args=(), timeout: float = 120) -> sqlite3.Row | None:
    """The newest matching request once it is terminal (None at the deadline)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = db_rows(data_dir, f"SELECT r.request_id, r.status, r.result_json, (SELECT group_concat(a.stage || ':' "
                                 f"|| a.state) FROM attempts a WHERE a.request_id = r.request_id) AS attempts "
                                 f"FROM requests r WHERE {where} ORDER BY r.created_at DESC LIMIT 1", args)
        if rows and rows[0]["status"] in TERMINAL:
            return rows[0]
        time.sleep(0.25)
    return None


def rendered(page, row) -> bool:
    """The request's own result is on the page: its status label and, for an answer, its summary."""
    from rfp_assistant.ui import STATUS_TEXT

    result = json.loads(row["result_json"]) if row and row["result_json"] else None
    if not result:
        return False
    return wait_text(page, re.escape(f"{STATUS_TEXT.get(result['status'], result['status'])}: "), 30000)


def wait_text(page, pattern: str, timeout_ms: int = 60000) -> bool:
    try:
        page.get_by_text(re.compile(pattern)).first.wait_for(timeout=timeout_ms)
        return True
    except Exception:  # noqa: BLE001
        return False


def ask(page, question: str) -> None:
    page.get_by_role("textbox", name="질문").wait_for(timeout=30000)
    page.get_by_role("textbox", name="질문").fill(question)
    page.get_by_role("button", name=re.compile("근거 기반 답변 받기")).click()


def ask_and_wait(page, data_dir: str, member: str, question: str) -> sqlite3.Row | None:
    """Submits once and waits for that new request to finish and render."""
    known = [r["request_id"] for r in member_requests(data_dir, member)]
    ask(page, question)
    marks = ",".join("?" * len(known))
    row = wait_request(data_dir, "r.member_id = ?" + (f" AND r.request_id NOT IN ({marks})" if known else ""),
                       (member, *known))
    if row is not None:
        rendered(page, row)  # let the page show it before the next step
    return row


def member_requests(data_dir: str, member: str) -> list[sqlite3.Row]:
    return db_rows(data_dir, "SELECT r.request_id, r.status, (SELECT group_concat(a.stage || ':' || a.state) "
                             "FROM attempts a WHERE a.request_id = r.request_id) AS attempts FROM requests r "
                             "WHERE r.member_id = ? ORDER BY r.created_at", (member,))


# ---------------------------------------------------------------- command flows


@flow("access")
def access(ctx: Context) -> dict:
    return {"role-declarations": ctx.unit("access-tests", [
        "tests.test_service.VisitorTest", "tests.test_service.AuthorizationTest",
        "tests.test_generation.AnswerFlowTest.test_verifier_actions_require_the_role"])}


@flow("question-modes")
def question_modes(ctx: Context) -> dict:
    return {"answer-modes": ctx.unit("mode-tests", ["tests.test_service.ModesTest", "tests.test_service.SearchFilterTest",
                                                    "tests.test_generation.AnswerFlowTest"]),
            "sources-and-exports": ctx.unit("source-tests", ["tests.test_service.SourceAndExportTest",
                                                             "tests.test_generation.SafetyTest"])}


@flow("request-lifecycle")
def request_lifecycle(ctx: Context) -> dict:
    return {"submission-and-cancellation": ctx.unit("request-tests", [
                "tests.test_service.SubmissionTest", "tests.test_service.QueueTest", "tests.test_service.CancellationTest"]),
            "ownership-and-history": ctx.unit("ownership-tests", ["tests.test_service.ScopeOwnershipTest"]),
            "six-user-load": load_check_summary(*ctx.cli("load-check", ["load-check", "--users", "6", "--provider", "fake"]))}


@flow("controlled-stop")
def controlled_stop(ctx: Context) -> dict:
    tests = ctx.unit("stop-tests", ["tests.test_service.ShutdownRestartTest", "tests.test_service.SignalStopTest"])
    code, out = ctx.run("signal-stop", [ctx.python, "-B", "tools/verification/signal_stop.py", str(ctx.work)], 300)
    try:
        result = json.loads((ctx.work / "signal-stop.json").read_text(encoding="utf-8"))
        actual = (f"{result.get('signal')}: request {result.get('status')}, result {result.get('result_status')}, "
                  f"attempts before {result.get('attempts_before_signal')} after {len(result.get('attempts') or [])}, "
                  f"child exit {result.get('exit_code')}" + (f", error {result['error']}" if result.get("error") else ""))
        native = (code == 0 and result.get("passed") is True, actual)
    except (OSError, ValueError):
        native = (False, f"no signal-stop result (exit {code})")
    return {"stop-and-recovery": tests, "native-ctrl-c": native}


@flow("shared-budget")
def shared_budget(ctx: Context) -> dict:
    return {"budget-ledger": ctx.unit("budget-tests", [
                "tests.test_service.SixUserBudgetTest", "tests.test_service.PacingTest",
                "tests.test_service.SearchFilterTest.test_only_the_highest_cap_warning_is_shown", "tests.test_budget"]),
            "six-user-load": load_check_summary(*ctx.cli("load-check", ["load-check", "--users", "6", "--provider", "fake"])),
            "injected-timeouts": load_check_summary(*ctx.cli("load-check-timeouts", [
                "load-check", "--users", "6", "--provider", "fake", "--fail-every", "3"]))}


@flow("budget-recovery")
def budget_recovery(ctx: Context) -> dict:
    return {"reconciliation": ctx.unit("reconciliation-tests", ["tests.test_service.ReconciliationTest"])}


def pick_documents(ctx: Context, corpus: dict) -> tuple[str, str]:
    """One parsed HWP and one parsed PDF in the active index (RFP_VERIFY_HWP_DOC_ID / _PDF_DOC_ID override)."""
    hwp, pdf = ctx.config.get("RFP_VERIFY_HWP_DOC_ID"), ctx.config.get("RFP_VERIFY_PDF_DOC_ID")
    if hwp and pdf:
        return hwp, pdf
    rows = db_rows(corpus["data_dir"], "SELECT d.doc_id, d.filename FROM documents d JOIN sources s "
                                       "ON s.source_hash = d.active_source_hash WHERE s.parse_status = 'parsed' "
                                       "ORDER BY d.csv_row_id")

    def first(ext):
        return next((r["doc_id"] for r in rows if r["filename"].lower().endswith(ext)), None)
    return hwp or first(".hwp") or rows[0]["doc_id"], pdf or first(".pdf") or rows[-1]["doc_id"]


@flow("verifier-runs")
def verifier_runs(ctx: Context) -> dict:
    unit = ctx.unit("verifier-tests", ["tests.test_service.VerifierTest", "tests.test_dense.ServingTest"])
    corpus = ctx.dataset()
    if "pair" in corpus:  # fixture: two parsed documents with shared evidence
        (hwp, pdf), corpus = corpus["pair"], {**corpus, "question": corpus["pair_question"]}
    else:
        hwp, pdf = pick_documents(ctx, corpus)
    (ctx.work / "inputs.json").write_text(json.dumps({"real_corpus": {
        "source_dir": corpus["source_dir"], "data_dir": corpus["data_dir"], "isolated_copy": True,
        "hwp_doc_id": hwp, "pdf_doc_id": pdf, "question": corpus["question"]}}, ensure_ascii=False), encoding="utf-8")
    code, out = ctx.run("frozen-generation", [ctx.python, "-B", "tools/verification/real_corpus_frozen.py",
                                              str(ctx.work)], 900)
    try:
        report = json.loads((ctx.work / "real-corpus-frozen.json").read_text(encoding="utf-8"))
        cases = "; ".join(f"{c['case']}: {'pass' if c['passed'] else 'FAIL ' + json.dumps(c.get('checks') or c.get('error'))}"
                          f" ({c.get('evidence_units')} units, {c.get('input_tokens')} tokens, max "
                          f"{c.get('displayed_maximum_micro_usd')} µUSD)" for c in report["cases"])
        frozen = (code == 0 and report["passed"], f"{corpus['kind']}, serving {report['serving'].get('mode')}: {cases}")
    except (OSError, ValueError, KeyError):
        frozen = (False, f"{corpus['kind']}: no result (exit {code}): {out[-300:]}")
    return {"verifier-unit": unit, "frozen-generation-on-corpus": frozen}


@flow("repository-gates")
def repository_gates(ctx: Context) -> dict:
    code, out = ctx.run("diff-check", ["git", "diff", "--check"], 60)
    whitespace = (code == 0 and not out.strip(), "no whitespace errors" if code == 0 and not out.strip()
                  else out.strip()[:300] or f"exit {code}")
    code, out = ctx.cli("phase3-gate", ["check", "--phase", "3", "--provider", "fake"])
    gate = (code == 0, unittest_summary(out, code))
    code, out = ctx.cli("phase4-gate", ["check", "--phase", "4", "--provider", "fake"])
    gate4 = (code == 0, unittest_summary(out, code))
    code, out = ctx.run("full-suite", [ctx.python, "-B", "-m", "unittest", "discover", "-s", "tests", "-t", "."])
    return {"whitespace": whitespace, "phase3-gate": gate, "phase4-gate": gate4,
            "full-suite": (code == 0, unittest_summary(out, code))}


@flow("evaluation-release")
def evaluation_release(ctx: Context) -> dict:
    """Unit gates plus the operator's CLI path on a temporary fixture corpus (fake provider, never RFP_DATA_DIR)."""
    gold_answers = ctx.unit("evaluation-tests", ["tests.test_evaluation"])
    release_tests = ctx.unit("release-tests", ["tests.test_release"])
    code, out = ctx.run("cli-walkthrough", [ctx.python, "-B", "tools/verification/phase4_walkthrough.py",
                                            str(ctx.work)], 900)
    try:
        report = json.loads((ctx.work / "phase4-walkthrough.json").read_text(encoding="utf-8"))
        failed = [s["step"] for s in report["steps"] if not s["ok"]]
        walk = (code == 0 and report["passed"], f"{len(report['steps'])} CLI steps, failed {failed or 'none'}; "
                                                f"release status {report.get('release_status')}")
    except (OSError, ValueError, KeyError):
        walk = (False, f"no walkthrough result (exit {code}): {out[-300:]}")
    return {"gold-and-answers": gold_answers, "backup-and-report": release_tests, "cli-walkthrough": walk}


# ---------------------------------------------------------------- browser flows


def with_browser(ctx: Context, body, delay: float = 1.0) -> dict:
    from playwright.sync_api import sync_playwright

    corpus = ctx.dataset()
    origin = ctx.serve(corpus, delay)
    with sync_playwright() as p:
        browser = ctx.browser(p)
        try:
            return body(ctx, browser, origin, corpus)
        finally:
            browser.close()


@flow("consultant-answer")
def consultant_answer(ctx: Context) -> dict:
    def body(ctx, browser, origin, corpus):
        page = ctx.page(browser, origin)
        member = f"verify-{os.getpid()}"
        out = {"no-login": ctx.act("open the app and type a name", "consultant page without a login form",
                                   lambda: ctx.open_app(page, origin, member))}

        def answer():
            page.get_by_text("선택", exact=True).first.click()
            last = ask_and_wait(page, corpus["data_dir"], member, corpus["question"])
            result = json.loads(last["result_json"]) if last and last["result_json"] else {}
            shown = bool(last) and rendered(page, last)
            ok = bool(shown and last["status"] == "completed" and result.get("status") == "answered"
                      and last["attempts"] == "generation:settled")
            return ok, (f"{corpus['kind']}; request {last['status'] if last else 'not finished in 120 s'}; "
                        f"result {result.get('status')}; attempts {last['attempts'] if last else None}; "
                        f"rendered {shown}")
        out["grounded-answer"] = ctx.act("select the first document and ask", "a settled grounded answer", answer)

        def evidence():
            page.get_by_role("button", name="근거 E1").first.click()
            opened = wait_text(page, r"^근거 E1 ·", 15000) or wait_text(page, "근거 E1 ·", 5000)
            return opened, f"evidence panel {'opened' if opened else 'did not open'} after one click"
        out["evidence-first-click"] = ctx.act("click 근거 E1 once", "the evidence panel opens", evidence)

        def download():
            with page.expect_download(timeout=30000) as info:
                page.get_by_role("button", name="이 근거의 원문 파일 받기").first.click()
            path = ctx.work / "download.bin"
            info.value.save_as(path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            match = db_rows(corpus["data_dir"], "SELECT d.filename FROM documents d WHERE d.active_source_hash = ?",
                            (digest,))
            return bool(match), (f"{path.stat().st_size} bytes, sha256 {digest[:16]}…, suggested name "
                                 f"{info.value.suggested_filename!r}; managed source {match[0]['filename'] if match else 'none'}")
        out["managed-download"] = ctx.act("download the evidence's original", "bytes equal a managed source", download)
        return out
    return with_browser(ctx, body)


@flow("request-history")
def request_history(ctx: Context) -> dict:
    def body(ctx, browser, origin, corpus):
        page = ctx.page(browser, origin)
        member = f"verify-history-{os.getpid()}"
        ctx.act("open the app and type a name", "consultant page", lambda: ctx.open_app(page, origin, member))

        def twice():
            page.get_by_text("선택", exact=True).first.click()
            for _ in range(2):
                ask_and_wait(page, corpus["data_dir"], member, corpus["question"])
            page.get_by_text("내 최근 요청").click()
            page.wait_for_timeout(800)
            page.locator('[data-testid="stExpander"] [data-testid="stSelectbox"]').last.click()
            options = page.get_by_role("option").all_inner_texts()
            rows = member_requests(corpus["data_dir"], member)
            ids = {r["request_id"][:8] for r in rows}
            shown = {i for i in ids if any(i in o for o in options)}
            return len(rows) == 2 and shown == ids, f"{len(rows)} requests stored; {len(options)} history options: {options}"
        out = {"separate-requests": ctx.act("submit the same question twice and open the history",
                                            "two separate history choices", twice)}

        def older_evidence():
            rows = member_requests(corpus["data_dir"], member)
            page.get_by_role("option", name=re.compile(rows[0]["request_id"][:8])).click()
            page.wait_for_timeout(1500)
            expander = page.locator('[data-testid="stExpander"]').filter(has_text="내 최근 요청")
            expander.get_by_role("button", name="근거 E1").first.click()
            opened = wait_text(page, "근거 E1 ·", 15000)
            panels = page.get_by_text(re.compile("근거 E1 ·")).count()
            return opened, f"older request {rows[0]['request_id'][:8]} selected; evidence panels shown {panels} after one click"
        out["history-evidence-first-click"] = ctx.act("open the older request's evidence once",
                                                      "its evidence opens", older_evidence)
        return out
    return with_browser(ctx, body)


@flow("verifier-generation")
def verifier_generation(ctx: Context) -> dict:
    def body(ctx, browser, origin, corpus):
        page = ctx.page(browser, origin)
        member = f"verify-verifier-{os.getpid()}"
        ctx.act("open the app and type a name", "consultant page", lambda: ctx.open_app(page, origin, member))

        def freeze():
            page.get_by_role("link", name="검증").click()
            page.get_by_text("검색 경로와 근거 추적").wait_for(timeout=30000)
            page.get_by_role("textbox", name="질문").fill(corpus["question"])
            page.get_by_role("textbox", name="질문").press("Enter")
            page.locator('[data-testid="stMultiSelect"]').first.click()
            page.get_by_role("option").first.click()
            page.keyboard.press("Escape")
            page.get_by_role("button", name="검색만 실행 (무료)").click()
            shown = wait_text(page, r"실행 vr-", 60000)
            runs = db_rows(corpus["data_dir"], "SELECT run_id, trace_json FROM verifier_runs WHERE member_id = ?",
                           (member,))
            est = json.loads(runs[-1]["trace_json"])["estimate_micro_usd"] if runs else None
            units = len(json.loads(runs[-1]["trace_json"])["retrieval"]["evidence"]) if runs else 0
            return bool(shown and runs and units), f"run {runs[-1]['run_id'] if runs else None}; {units} evidence units; max {est} µUSD"
        out = {"frozen-run": ctx.act("run a free search-only verification", "a frozen run with evidence", freeze)}

        def generate():
            run = db_rows(corpus["data_dir"], "SELECT run_id, trace_json FROM verifier_runs WHERE member_id = ? "
                                              "ORDER BY created_at DESC LIMIT 1", (member,))[0]
            page.get_by_text(re.compile("유료 답변 생성을 1회 실행")).click()  # Streamlit hides the input itself
            page.wait_for_timeout(800)
            page.get_by_role("button", name="유료 답변 생성").click()
            key = f"vgen-{run['run_id']}"
            done = wait_request(corpus["data_dir"], "r.idempotency_key = ?", (key,))  # this request, not page text
            shown = rendered(page, done) if done else False
            req = db_rows(corpus["data_dir"], "SELECT request_id, status FROM requests WHERE idempotency_key = ?", (key,))
            attempts = db_rows(corpus["data_dir"], "SELECT stage, state, reserved_micro_usd FROM attempts WHERE request_id = ?",
                               (req[0]["request_id"],)) if req else []
            maximum = json.loads(run["trace_json"])["estimate_micro_usd"]
            reserved = sum(a["reserved_micro_usd"] for a in attempts)
            ok = bool(shown and req and req[0]["status"] == "completed" and [a["stage"] for a in attempts] == ["generation"]
                      and maximum is not None and reserved <= maximum)
            return ok, (f"request {req[0]['status'] if req else None}; rendered {shown}; attempts "
                        f"{[(a['stage'], a['state']) for a in attempts]}; reserved {reserved} µUSD of max {maximum}")
        out["generation-within-consent"] = ctx.act("consent and generate from the frozen run",
                                                   "one generation attempt within the displayed maximum", generate)
        return out
    return with_browser(ctx, body)


def sidebar_pending(page) -> float | None:
    found = re.search(r"진행 중 예약 \$([0-9.,]+)", page.locator('section[data-testid="stSidebar"]').inner_text())
    return float(found.group(1).replace(",", "")) if found else None


def attempt_count(data_dir: str) -> int:
    return db_rows(data_dir, "SELECT COUNT(*) AS n FROM attempts")[0]["n"]


@flow("six-sessions")
def six_sessions(ctx: Context) -> dict:
    def body(ctx, browser, origin, corpus):
        members = [f"verify-six-{i}-{os.getpid()}" for i in range(1, 7)]
        pages = []

        def open_all():
            for m in members:
                page = ctx.page(browser, origin)
                ctx.open_app(page, origin, m)
                page.get_by_text("선택", exact=True).first.click()
                page.get_by_role("textbox", name="질문").wait_for(timeout=30000)
                page.get_by_role("textbox", name="질문").fill(corpus["question"])
                pages.append(page)
            return len(pages) == 6, f"{len(pages)} browser sessions, each with its own typed name, document and question"
        ctx.act("open six sessions with different names", "six ready sessions", open_all)

        def watch():
            started = time.monotonic()
            pages[0].get_by_role("button", name=re.compile("근거 기반 답변 받기")).click()
            seen = None
            while time.monotonic() - started < 10 and seen is None:
                value = sidebar_pending(pages[5])
                if value:
                    seen = (round(time.monotonic() - started, 1), value)
                pages[5].wait_for_timeout(200)
            return seen is not None, (f"session 6 showed a shared reservation of ${seen[1]} {seen[0]} s after session 1 "
                                      "submitted" if seen else "session 6 never showed a reservation within 10 s")
        visible = ctx.act("submit in session 1 and watch session 6's sidebar", "the shared reservation appears",
                          watch)

        def all_submit():
            for page in pages[1:]:
                page.get_by_role("button", name=re.compile("근거 기반 답변 받기")).click()
            rows = [wait_request(corpus["data_dir"], "r.member_id = ?", (m,), 180) for m in members]
            per = db_rows(corpus["data_dir"], "SELECT r.member_id, COUNT(a.attempt_id) AS n, "
                          "SUM(a.state IN ('reserved', 'dispatching')) AS open, SUM(a.member_id != r.member_id) AS foreign_ "
                          "FROM requests r LEFT JOIN attempts a ON a.request_id = r.request_id "
                          f"WHERE r.member_id IN ({','.join('?' * 6)}) GROUP BY r.member_id", members)
            statuses = [json.loads(r["result_json"])["status"] if r and r["result_json"] else None for r in rows]
            ok = (all(r is not None and r["status"] == "completed" for r in rows) and len(per) == 6
                  and all(x["n"] <= 1 and not x["open"] and not x["foreign_"] for x in per))
            return ok, (f"results {statuses}; attempts per member {[x['n'] for x in per]}; open "
                        f"{sum(x['open'] or 0 for x in per)}; attributed to another name {sum(x['foreign_'] or 0 for x in per)}")
        attributed = ctx.act("submit in the other five sessions", "six finished, attributed requests", all_submit)
        return {"six-attributed": attributed, "shared-reservation-visible": visible}
    return with_browser(ctx, body, delay=4.0)


@flow("cap-exhaustion")
def cap_exhaustion(ctx: Context) -> dict:
    def body(ctx, browser, origin, corpus):
        data = corpus["data_dir"]
        owner = ctx.page(browser, origin)
        ctx.open_app(owner, origin, f"verify-owner-{os.getpid()}")
        cap = db_rows(data, "SELECT cap_micro_usd FROM budget_settings WHERE id = 1")[0]["cap_micro_usd"]
        amount = f"{cap / 1_000_000 + 1:.6f}"

        def adjust(reason: str):
            owner.get_by_role("link", name="사용량 관리").click()
            owner.get_by_text("미확정 비용").first.wait_for(timeout=30000)
            owner.get_by_role("tab", name="외부 사용 조정").click()
            panel = owner.get_by_role("tabpanel", name="외부 사용 조정")
            panel.get_by_label("조정 키(중복 방지)").fill(f"verify-cap-{os.getpid()}")
            panel.get_by_label("금액(USD, 음수는 정정)").fill(amount)
            panel.get_by_label("증빙").fill("verification: synthetic cap exhaustion on an isolated runtime")
            panel.get_by_label("사유").fill(reason)
            before = db_rows(data, "SELECT COUNT(*) AS n FROM audit_events")[0]["n"]
            panel.get_by_role("button", name="조정 기록").click()
            owner.wait_for_timeout(2500)
            after = db_rows(data, "SELECT COUNT(*) AS n FROM audit_events")[0]["n"]
            return panel, after - before

        def reasonless():
            panel, added = adjust("")
            errors = panel.locator('[data-testid="stAlertContentError"]').all_inner_texts()
            return bool(errors) and added == 0, f"error shown {errors[:1]}; audit events added {added}"
        refused = ctx.act("record an adjustment without a reason", "refused and nothing recorded", reasonless)

        def with_reason():
            panel, added = adjust("cap exhaustion verification")
            ok_text = panel.get_by_text("기록했습니다.").count()
            owner.get_by_role("tab", name="감사 기록").click()
            owner.wait_for_timeout(1000)
            listed = owner.get_by_role("tabpanel", name="감사 기록").inner_text().count("cap exhaustion verification")
            return bool(ok_text and added == 1 and listed), f"confirmation {bool(ok_text)}; audit events added {added}; audit tab rows {listed}"
        audited = ctx.act("record the adjustment with a reason", "recorded and listed in the audit log", with_reason)

        member = f"verify-cap-{os.getpid()}"
        consultant = ctx.page(browser, origin)
        ctx.open_app(consultant, origin, member)

        def blocked():
            warned = wait_text(consultant, "운영 한도에 도달", 15000)
            consultant.get_by_text("선택", exact=True).first.click()
            row = ask_and_wait(consultant, data, member, corpus["question"])
            result = json.loads(row["result_json"])["status"] if row and row["result_json"] else None
            return bool(warned and result == "budget_blocked" and not row["attempts"]), (
                f"cap warning shown {warned}; result {result}; attempts {row['attempts'] if row else None}")
        paid = ctx.act("ask at the exhausted cap", "blocked before any attempt", blocked)

        def free_and_polling():
            count = attempt_count(data)
            with consultant.expect_download(timeout=30000) as info:
                consultant.get_by_role("button", name="원문 파일 받기").first.click()
            path = ctx.work / "cap-download.bin"
            info.value.save_as(path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            managed = bool(db_rows(data, "SELECT 1 FROM documents WHERE active_source_hash = ?", (digest,)))
            consultant.wait_for_timeout(6000)  # several budget and request polling intervals
            consultant.reload()
            consultant.get_by_label("이름 (사용·검토 기록용)").wait_for(timeout=60000)
            consultant.wait_for_timeout(4000)
            after = attempt_count(data)
            return managed and after == count, (f"original downloaded ({path.stat().st_size} bytes, managed {managed}); "
                                                f"attempts {count} before and {after} after 10 s of polling and a reload")
        free = ctx.act("download an original, keep polling and reload", "works with no new attempt", free_and_polling)
        return {"reasonless-refused": refused, "adjustment-audited": audited, "paid-blocked-at-cap": paid,
                "free-routes-and-polling": free}
    return with_browser(ctx, body)


FOCUS_JS = """async () => {
  const e = document.activeElement; if (!e || e === document.body) return null;
  const chain = []; for (let x = e, i = 0; x && i < 5; x = x.parentElement, i++) chain.push(x);
  const look = () => chain.map(x => { const c = getComputedStyle(x);
    return [c.outlineStyle, c.outlineWidth, c.outlineColor, c.boxShadow, c.borderColor, c.backgroundColor].join('|'); }).join('/');
  // Some widgets (BaseWeb inputs) restyle on focus/blur events through React state: let them re-render.
  const settle = () => new Promise(r => setTimeout(r, 120));
  const focused = look(); e.blur(); await settle(); const plain = look(); e.focus(); await settle();
  const label = e.closest('label');
  const name = (e.getAttribute('aria-label') || (label && label.innerText) || e.innerText || e.placeholder || '').trim();
  return {tag: e.tagName, type: e.type || '', name: name.slice(0, 40), ring: focused !== plain};
}"""

CONTRAST_JS = """() => {
  const lum = c => { const v = c.match(/[\\d.]+/g).slice(0, 3).map(Number).map(x => { x /= 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; }); return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]; };
  const bg = el => { for (let x = el; x; x = x.parentElement) { const c = getComputedStyle(x).backgroundColor; const a = c.match(/[\\d.]+/g); if (a && (a.length < 4 || Number(a[3]) > 0.5)) return c; } return 'rgb(255,255,255)'; };
  const out = [];
  for (const el of document.querySelectorAll('[data-testid="stMain"] *, [data-testid="stSidebar"] *')) {
    const own = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
    if (!own || !el.offsetParent) continue;
    const s = getComputedStyle(el); if (s.visibility === 'hidden' || Number(s.opacity) < 0.5) continue;
    const a = lum(s.color), b = lum(bg(el)); const r = (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
    out.push({text: el.innerText.trim().slice(0, 30), ratio: Math.round(r * 100) / 100, size: s.fontSize, color: s.color});
  }
  return out;
}"""


@flow("accessibility")
def accessibility(ctx: Context) -> dict:
    def body(ctx, browser, origin, corpus):
        page = ctx.page(browser, origin)
        member = f"verify-a11y-{os.getpid()}"
        ctx.open_app(page, origin, member)
        focused = []

        def tab_to(match, limit=150):
            for _ in range(limit):
                page.keyboard.press("Tab")
                info = page.evaluate(FOCUS_JS)
                if info:
                    focused.append(info)
                    if match(info):
                        return info
            return None

        def keyboard():
            # Styles are compared focused vs. unfocused; transitions would report a value mid-animation.
            page.add_style_tag(content="*, *::before, *::after { transition: none !important; }")
            page.get_by_label("이름 (사용·검토 기록용)").focus()
            pick = tab_to(lambda i: i["type"] == "checkbox" and i["name"] == "선택")
            if pick:
                page.keyboard.press("Space")
                page.wait_for_timeout(1500)
            box = tab_to(lambda i: i["tag"] == "TEXTAREA")
            if box:
                page.keyboard.type(corpus["question"])
            known = [r["request_id"] for r in member_requests(corpus["data_dir"], member)]
            submit = tab_to(lambda i: i["tag"] == "BUTTON" and "답변 받기" in i["name"])
            if submit:
                page.keyboard.press("Enter")
            row = wait_request(corpus["data_dir"], "r.member_id = ?", (member,), 120) if submit else None
            new = bool(row and row["request_id"] not in known)
            opened = False
            if new and rendered(page, row):
                if tab_to(lambda i: i["tag"] == "BUTTON" and i["name"] == "근거 E1"):
                    page.keyboard.press("Enter")
                    opened = wait_text(page, "근거 E1 ·", 15000)
            return bool(pick and box and submit and new and opened), (
                f"document selected {bool(pick)}; question typed {bool(box)}; submitted {bool(submit)}; "
                f"request {row['status'] if row else None}; evidence opened {opened}; {len(focused)} Tab stops")
        keys = ctx.act("select, ask and open evidence with the keyboard only", "all done by keyboard", keyboard)

        def rings():
            controls = [f for f in focused if f["tag"] in ("INPUT", "TEXTAREA", "BUTTON", "A", "SELECT")]
            missing = [f"{f['tag']}:{f['name']}" for f in controls if not f["ring"]]
            return bool(controls) and not missing, f"{len(controls)} focused controls; without a visible ring: {missing[:8]}"
        ring = ctx.act("inspect every keyboard focus stop", "a visible focus indicator", rings)

        def contrast():
            items = page.evaluate(CONTRAST_JS)
            low = sorted((i for i in items if i["ratio"] < 4.5), key=lambda i: i["ratio"])
            return bool(items) and not low, (f"{len(items)} text elements; minimum ratio "
                                              f"{min((i['ratio'] for i in items), default=None)}; below 4.5: "
                                              f"{[(i['text'], i['ratio']) for i in low[:6]]}")
        readable = ctx.act("measure text contrast", "every text at least 4.5:1", contrast)

        def narrow():
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(1500)
            width = page.evaluate("() => [document.documentElement.scrollWidth, window.innerWidth]")
            status = page.get_by_text(re.compile(r"^(답변|근거 부족|확인 필요|기술 오류): ")).count()
            return width[0] <= width[1] and status > 0, f"scroll width {width[0]} for a {width[1]} px viewport; status labels in text {status}"
        layout = ctx.act("narrow the viewport to 390 px", "no horizontal scroll and text status", narrow)
        return {"keyboard-only": keys, "focus-visible": ring, "contrast": readable, "narrow-layout": layout}
    return with_browser(ctx, body)


# ---------------------------------------------------------------- evidence


def git_head() -> str:
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else ""


def evidence(spec: dict, head: str, observed: dict, ctx: Context | None, error: str | None) -> dict:
    expected_head = os.environ.get("WIKI_VERIFICATION_HEAD", "")
    stale = bool(expected_head) and expected_head != head
    observations = []
    for a in spec["assertions"]:
        ok, actual = observed.get(a["id"], (False, f"not observed{': ' + error if error else ''}"))
        if stale:
            ok, actual = False, f"checked {head}, not the requested {expected_head}: {actual}"
        observations.append({"id": a["id"], "expected": a["expected"], "actual": actual or "(empty)",
                             "pass": bool(ok)})
    out = {"head": head, "flow": spec["id"], "environment_id": os.environ.get("WIKI_VERIFICATION_ENVIRONMENT", ""),
           "test_scope": os.environ.get("WIKI_VERIFICATION_SCOPE", ""), "observations": observations}
    if spec["kind"] in ("api", "browser"):
        out["requests"] = ctx.requests if ctx else []
    if spec["kind"] == "browser":
        out.update(browser_tool=ctx.browser_tool if ctx else "", build_head=ctx.build_head if ctx else "",
                   actions=ctx.actions if ctx else [])
    return out


def main(argv: list[str]) -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    specs = {f["id"]: f for f in manifest["flows"]}
    if not argv or argv[0] in ("-h", "--help", "--list"):
        print(__doc__)
        for f in manifest["flows"]:
            print(f"  {f['id']:<22} {f['kind']:<8} {f['title']}")
        return 0
    if argv[0] not in specs or argv[0] not in FLOWS:
        print(f"unknown flow {argv[0]!r}", file=sys.stderr)
        return 2
    spec, head = specs[argv[0]], git_head()
    ctx, observed, error = None, {}, None
    try:
        ctx = Context(spec["id"])
        observed = FLOWS[spec["id"]](ctx)
    except Exception as exc:  # noqa: BLE001 - reported as unobserved assertions
        error = f"{type(exc).__name__}: {str(exc)[:300]}"
        print(f"[flow] {error}", flush=True)
    finally:
        if ctx is not None:
            ctx.close()
    result = evidence(spec, head, observed, ctx, error)
    if ctx is not None:
        print(f"[flow] work directory {ctx.work}", flush=True)
    print("```local-evidence\n" + json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n```",
          flush=True)  # three lines: the whole block stays inside any tail of the output
    return 0 if all(o["pass"] for o in result["observations"]) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
