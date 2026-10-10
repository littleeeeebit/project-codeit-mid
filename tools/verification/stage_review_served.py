"""review/server.py itself serving a fixture run: import, view, a tampered import and a decision.

    python -B tools/verification/stage_review_served.py <work dir>

Run by the `stage-review` flow in tools/verify.py, with src/ and the repository on PYTHONPATH. Starts the server
on a free loopback port with RFP_DATA_DIR inside <work dir>, stops it before exiting, and prints one JSON line last:
{"served-import": [ok, actual], "tampered-view-refused": [...], "served-decision": [...], "requests": [...]}.
Needs no database, npm or network beyond loopback; the fixture run stands in for corpus text.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from tests import test_stage_review as contract

REPO = Path(__file__).resolve().parents[2]
OTHER = "chunking-20261010T000000Z-def456"


def tampered(exported: bytes, run_id: str) -> bytes:
    """Another run whose own run.json is consistent but whose view.json names `run_id`: its decision would land on
    that run's files."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(exported)) as original, zipfile.ZipFile(out, "w") as z:
        for name in original.namelist():
            data = original.read(name)
            if name.endswith("/run.json"):
                data = json.dumps({**json.loads(data), "run_id": OTHER}).encode()
            z.writestr(OTHER + name[len(run_id):], data)
    return out.getvalue()


def main(work: Path) -> dict:
    source, served = work / "source", work / "served"
    run_id, _ = contract.make_run(source)
    exported = contract.server.export_run(source, run_id)
    requests: list[dict] = []
    log = (work / "review-server.log").open("w+", encoding="utf-8")
    server = subprocess.Popen([sys.executable, "-B", str(REPO / "review" / "server.py"), "--no-build", "--no-browser"],
                              cwd=REPO, env={**os.environ, "RFP_DATA_DIR": str(served)}, stdout=log,
                              stderr=subprocess.STDOUT)
    try:
        origin, deadline = None, time.monotonic() + 30
        while origin is None and time.monotonic() < deadline and server.poll() is None:
            log.seek(0)
            found = re.search(r"http://127\.0\.0\.1:\d+", log.read())
            if found:
                origin = found.group(0)
            else:
                time.sleep(0.2)
        if origin is None:
            raise RuntimeError(f"review/server.py did not print its URL; see {work / 'review-server.log'}")

        def call(method: str, path: str, body: bytes | None = None, ctype: str = "application/json",
                 page: str | None = None):
            req = urllib.request.Request(origin + path, body, method=method, headers={
                "Content-Type": ctype, "Origin": page or origin})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    status, raw = r.status, r.read()
            except urllib.error.HTTPError as refused:
                status, raw = refused.code, refused.read()
            requests.append({"method": method, "url": origin + path, "status": status})
            return status, json.loads(raw) if raw else None

        status, imported = call("POST", "/api/import", exported, "application/zip")
        listed = [r["run_id"] for r in call("GET", "/api/runs")[1] or []]
        view = call("GET", f"/api/runs/{run_id}")[1] or {}
        served_import = [status == 200 and (imported or {}).get("run_id") == run_id and listed == [run_id]
                         and view.get("run_id") == run_id and len(view.get("candidates") or []) == 3,
                         f"import {status}, listed {listed}, view of {view.get('run_id')} with "
                         f"{len(view.get('candidates') or [])} candidates"]

        refused, said = call("POST", "/api/import", tampered(exported, run_id), "application/zip")
        error = str((said or {}).get("error"))
        still = [r["run_id"] for r in call("GET", "/api/runs")[1] or []]
        tampered_refused = [refused == 400 and "view.json" in error and still == [run_id],
                            f"tampered view.json import answered {refused} ({error}); runs {still}"]

        decision = json.dumps({"chosen": None, "reasons": {"c1": "loses the table title"},
                               "notes": {"0": "check row 2"}}).encode()
        foreign = call("POST", f"/api/runs/{run_id}/decision", decision, page="http://evil.example")[0]
        status, saved = call("POST", f"/api/runs/{run_id}/decision", decision)
        files = served / "review" / "decisions"
        md = files / f"{run_id}.md"
        record = json.loads((files / f"{run_id}.json").read_text(encoding="utf-8")) if status == 200 else {}
        pointed = str(md) in (saved or {}).get("prompt", "")
        served_decision = [foreign == 403 and status == 200 and md.is_file() and record.get("run_id") == run_id
                           and record.get("chosen") is None and pointed,
                           f"foreign-origin decision {foreign}; same-origin {status} wrote "
                           f"{sorted(p.name for p in files.iterdir()) if files.is_dir() else []}; prompt points at "
                           f"the .md {pointed}"]
    finally:
        server.terminate()
        server.wait(30)
        log.close()
    return {"served-import": served_import, "tampered-view-refused": tampered_refused,
            "served-decision": served_decision, "requests": requests}


if __name__ == "__main__":
    print(json.dumps(main(Path(sys.argv[1])), ensure_ascii=False))
