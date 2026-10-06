"""Native Ctrl+C stops a running request before its next paid stage.

A child process owns a fixture runtime with the fake provider, submits one request whose retrieval finishes and
then pauses, and polls its main loop like uvicorn's server loop. The parent sends a real Ctrl+C: Windows
`CTRL_C_EVENT` to the child's own console, POSIX SIGINT. Expected: the request ends `interrupted` with no attempt,
and the child exits 0. Writes `<run_dir>/signal-stop.json`; exit 0 only on the expected outcome.

    python tools/verification/signal_stop.py <run_dir>
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

CHILD = r'''
import atexit, json, os, signal, sys, threading, time
from pathlib import Path
if os.name == "nt":  # a new console may inherit "ignore Ctrl+C"; accept it explicitly
    import ctypes
    ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)
from rfp_assistant.service import service
from rfp_assistant.storage import store
from rfp_assistant.contracts import AnswerRequest
from tests import fixtures

root = Path(sys.argv[1])
env = fixtures.make_env(root / "fixture")  # its PostgreSQL database is dropped when this process exits
res = service.Resources(env.settings.with_(shutdown_wait_seconds=10), recover=True)  # provider fake
prepare = service.prepare_answer
rid = None

def attempts():
    with store.open_db(env.settings.db_path) as conn:
        return [list(a) for a in conn.execute("SELECT stage, state FROM attempts WHERE request_id = ?", (rid,))]

def slow(*a, **kw):
    out = prepare(*a, **kw)
    (root / "prepared").write_text(json.dumps(len(attempts())))
    time.sleep(2.0)
    return out

def record():  # atexit, newest first: after the controlled stop (a threading exit hook), before the drop
    with store.open_db(env.settings.db_path) as conn:
        row = conn.execute("SELECT status, result_json FROM requests WHERE request_id = ?", (rid,)).fetchone()
    (root / "child-result.json").write_text(json.dumps({"status": row[0], "result_json": row[1],
                                                        "attempts": attempts()}))

service.prepare_answer = slow
stopping = threading.Event()
signal.signal(signal.SIGINT, lambda *a: stopping.set())  # like uvicorn: the handler ends the server loop
rid = service.submit_answer(res, env.consultant, AnswerRequest(
    "signal-stop", "signal-stop", "하자보수 기간은 얼마인가요?", [env.refs["기관A"]], as_of="2026-09-30"))
atexit.register(record)
(root / "child.json").write_text(json.dumps({"request_id": rid}))
deadline = time.monotonic() + 60
while not stopping.is_set() and time.monotonic() < deadline:  # poll, as an event loop does
    time.sleep(0.05)
'''

SENDER = r'''
import ctypes, sys, time
k = ctypes.WinDLL("kernel32", use_last_error=True)
k.FreeConsole()
if not k.AttachConsole(int(sys.argv[1])):
    raise ctypes.WinError(ctypes.get_last_error())
k.SetConsoleCtrlHandler(None, True)  # the sender itself ignores the event it raises
if not k.GenerateConsoleCtrlEvent(0, 0):
    raise ctypes.WinError(ctypes.get_last_error())
time.sleep(0.5)
k.FreeConsole()
'''


def main(run_dir: Path) -> int:
    root = run_dir / "signal-stop"
    root.mkdir(parents=True, exist_ok=True)
    log = (root / "child.log").open("w", encoding="utf-8")
    kwargs = {}
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        kwargs = {"creationflags": subprocess.CREATE_NEW_CONSOLE, "startupinfo": startup}
    child = subprocess.Popen([sys.executable, "-B", "-c", CHILD, str(root)], stdout=log, stderr=log, **kwargs)
    outcome: dict = {"platform": sys.platform}
    try:
        deadline = time.monotonic() + 120
        while not ((root / "prepared").exists() and (root / "child.json").exists()):
            if child.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("the child never reached the pause after retrieval; see child.log")
            time.sleep(0.02)
        info = json.loads((root / "child.json").read_text())
        before = json.loads((root / "prepared").read_text())
        sent_at = time.time()
        if os.name == "nt":
            subprocess.run([sys.executable, "-B", "-c", SENDER, str(child.pid)], check=True, timeout=30)
            outcome["signal"] = "CTRL_C_EVENT"
        else:
            os.kill(child.pid, signal.SIGINT)
            outcome["signal"] = "SIGINT"
        child.wait(timeout=90)
        after = json.loads((root / "child-result.json").read_text())  # read by the child before its database went
        result = after["result_json"]
        outcome.update(request_id=info["request_id"], attempts_before_signal=before, exit_code=child.returncode,
                       status=after["status"], result_status=json.loads(result)["status"] if result else None,
                       attempts=after["attempts"], seconds_to_exit=round(time.time() - sent_at, 2))
    except Exception as exc:  # noqa: BLE001 - recorded as a failed check
        outcome["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if child.poll() is None:
            child.kill()
        log.close()
    outcome["passed"] = (outcome.get("attempts_before_signal") == 0 and outcome.get("exit_code") == 0
                         and outcome.get("status") == "interrupted" and outcome.get("attempts") == [])
    (run_dir / "signal-stop.json").write_text(json.dumps(outcome, indent=1), encoding="utf-8")
    print(json.dumps(outcome))
    return 0 if outcome["passed"] else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
