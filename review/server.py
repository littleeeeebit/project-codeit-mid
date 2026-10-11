"""The local stage review app: the static export in review/out plus the run folders under <data>/review.

    python review/server.py                  build when review/ changed, serve on a free loopback port, open it
    python review/server.py --port 8520 --no-browser --no-build

Standard library only, so a machine without the project's Python environment can still view imported runs. It reads
run folders, imports and exports them as zip files, writes decision files and answer notes, and records the person's
decisions on failure types grouped from those notes (src/rfp_assistant/evaluation/error_analysis.py, also standard
library only; the coding agent does the grouping from an exported bundle). It never runs a pipeline, loads a model
or calls a paid service; runs come from `python -m rfp_assistant.cli stage-review run ...`, and a paid run (generation,
ocr) appears here only after its approved estimate was paid (`stage-review resume`).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import uuid
import webbrowser
import zipfile
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP = Path(__file__).resolve().parent
REPO = APP.parent
# The note store and the failure taxonomy are shared with the CLI and the release report; the module is standard
# library only, so this server still runs without the project's environment.
sys.path.insert(0, str(REPO / "src"))
from rfp_assistant.evaluation import error_analysis as ea  # noqa: E402
from rfp_assistant.evaluation.error_analysis import (  # noqa: E402
    ReviewInputError, annotation_state, annotations_file, read_annotations, write_lock)

OUT = APP / "out"
RUN_SCHEMA = "review-run-1"
DECISION_SCHEMA = "review-decision-1"
RUN_ID = re.compile(r"^(retriever|chunking|generation|ocr)-\d{8}T\d{6}Z-[0-9a-f]{6}$")
SKIP = {"node_modules", "out", ".next", "__pycache__"}
MAX_IMPORT = 2 * 1024 ** 3
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css", ".json": "application/json",
         ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2",
         ".txt": "text/plain; charset=utf-8"}


def data_dir() -> Path:
    """RFP_DATA_DIR from the environment or the checkout's .env, as the CLI runner resolves it; else .runtime."""
    value = os.environ.get("RFP_DATA_DIR")
    env = REPO / ".env"
    if not value and env.exists():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("RFP_DATA_DIR="):
                value = line.split("=", 1)[1].strip().strip('"')
    return Path(value) if value and Path(value).is_absolute() else REPO / ".runtime"


# ---------------------------------------------------------------- build


def source_digest() -> str:
    h = hashlib.sha256()
    files = []
    for folder, dirs, names in os.walk(APP):
        dirs[:] = [d for d in dirs if d not in SKIP]  # never descend into node_modules: walking it is most of a start
        files += [Path(folder, n) for n in names if not n.endswith(".py") and n != "tsconfig.tsbuildinfo"]
    for path in sorted(files):
        h.update(path.relative_to(APP).as_posix().encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return h.hexdigest()


def ensure_build() -> None:
    """npm ci when the lockfile changed, npm run build when any app source changed; otherwise nothing."""
    npm = shutil.which("npm")
    lock = hashlib.sha256((APP / "package-lock.json").read_bytes()).hexdigest()
    installed, built = APP / "node_modules" / ".review-lock", OUT / ".review-build"
    digest = source_digest()
    if installed.exists() and installed.read_text() == lock and built.exists() and built.read_text() == digest:
        return
    if npm is None:
        raise SystemExit("npm is not on PATH: install Node.js 20 or newer, then start the review app again")
    if not installed.exists() or installed.read_text() != lock:
        print("Installing the review app (npm ci) ...", flush=True)
        subprocess.run([npm, "ci"], cwd=APP, check=True)
        installed.write_text(lock)
    print("Building the review app (npm run build) ...", flush=True)
    subprocess.run([npm, "run", "build"], cwd=APP, check=True)
    # next build writes next-env.d.ts and may touch tsconfig.json, so stamp what the next start will see.
    built.write_text(source_digest())


# ---------------------------------------------------------------- runs


def runs_dir(root: Path) -> Path:
    return root / "review" / "runs"


def decisions_dir(root: Path) -> Path:
    return root / "review" / "decisions"


def run_folder(root: Path, run_id: str) -> Path:
    if not RUN_ID.match(run_id):
        raise ReviewInputError("unknown run")
    folder = runs_dir(root) / run_id
    if not (folder / "run.json").exists():
        raise FileNotFoundError(run_id)
    return folder


def list_runs(root: Path) -> list[dict]:
    out = []
    base = runs_dir(root)
    for folder in sorted(base.iterdir(), reverse=True) if base.exists() else []:
        if not (folder / "view.json").exists():  # a paid run still waiting for its estimate's approval
            continue
        try:
            run = json.loads((folder / "run.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        imported = folder / ".imported.json"
        decision = decisions_dir(root) / f"{run['run_id']}.json"
        out.append({"run_id": run["run_id"], "stage": run["stage"], "created_at": run["created_at"],
                    "imported": json.loads(imported.read_text(encoding="utf-8")) if imported.exists() else None,
                    "decided": decision.exists(),
                    "candidates": [{k: c.get(k) for k in ("id", "label", "role", "commit", "status", "host")}
                                   for c in run["candidates"]]})
    return sorted(out, key=lambda r: r["created_at"], reverse=True)


def export_run(root: Path, run_id: str) -> bytes:
    """The run folder, plus its annotations (kept outside the folder) as <run-id>/annotations.jsonl."""
    folder = run_folder(root, run_id)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(folder.rglob("*")):
            if path.is_file() and path.name not in (".imported.json", ANNOTATIONS_IN_ZIP):
                z.write(path, f"{run_id}/{path.relative_to(folder).as_posix()}")
        if annotations_file(root, run_id).exists():
            z.write(annotations_file(root, run_id), f"{run_id}/{ANNOTATIONS_IN_ZIP}")
    return buffer.getvalue()


def check_run(run_id: str, run: dict, view: dict) -> None:
    """run.json and view.json both describe the run in folder run_id, with the same candidates and commits: the
    app sends decisions by view.run_id and writes the view's candidate commits into the decision."""
    def cands(record: dict) -> list:
        return [(c.get("id"), c.get("role"), c.get("commit"), c.get("status")) for c in record.get("candidates") or []]
    for name, record in (("run.json", run), ("view.json", view)):
        if not isinstance(record, dict) or record.get("schema") != RUN_SCHEMA or record.get("run_id") != run_id \
                or record.get("stage") != run_id.split("-", 1)[0]:
            raise ReviewInputError(f"{name} is not a {RUN_SCHEMA} record of {run_id}")
    if not cands(run) or cands(run) != cands(view):
        raise ReviewInputError(f"view.json lists other candidates than run.json of {run_id}")


def read_view(root: Path, run_id: str) -> dict:
    folder = run_folder(root, run_id)
    view = json.loads((folder / "view.json").read_text(encoding="utf-8"))
    check_run(run_id, json.loads((folder / "run.json").read_text(encoding="utf-8")), view)
    return view


def import_run(root: Path, data: bytes) -> str:
    """A zip holding one run folder (as exported) becomes runs/<run-id>; an existing run is never replaced."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ReviewInputError("not a zip file") from None
    names = [n for n in z.namelist() if not n.endswith("/")]
    tops = {n.split("/", 1)[0] for n in names}
    if len(tops) != 1 or not RUN_ID.match(next(iter(tops))):
        raise ReviewInputError("the zip must hold exactly one run folder named like retriever-20261010T120000Z-abc123")
    run_id = tops.pop()
    if any(".." in Path(n).parts or Path(n).is_absolute() or "\\" in n for n in names):
        raise ReviewInputError("the zip holds a path outside its run folder")
    try:
        run, view = json.loads(z.read(f"{run_id}/run.json")), json.loads(z.read(f"{run_id}/view.json"))
    except (KeyError, ValueError):
        raise ReviewInputError("run.json or view.json is missing or unreadable") from None
    check_run(run_id, run, view)
    target = runs_dir(root) / run_id
    if target.exists():
        raise ReviewInputError(f"{run_id} is already here")
    carried = []
    if f"{run_id}/{ANNOTATIONS_IN_ZIP}" in names:
        try:
            carried = [json.loads(line) for line in z.read(f"{run_id}/{ANNOTATIONS_IN_ZIP}").decode().splitlines()
                       if line.strip()]
        except ValueError:
            raise ReviewInputError(f"{ANNOTATIONS_IN_ZIP} is unreadable") from None
        check_annotations(view, carried)
    runs_dir(root).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runs_dir(root)) as tmp:
        z.extractall(tmp)
        (Path(tmp) / run_id / ANNOTATIONS_IN_ZIP).unlink(missing_ok=True)  # they live in the store, not the run
        (Path(tmp) / run_id / ".imported.json").write_text(json.dumps(
            {"imported_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
        # The notes go into the store before the run is installed: a failure here leaves no run behind, and a run
        # that then fails to install is imported again with its notes recognised as already stored.
        with write_lock(root):
            if target.exists():  # again under the lock: another server may have imported it meanwhile
                raise ReviewInputError(f"{run_id} is already here")
            existing = read_annotations(root, run_id)
            have = {r["id"]: r for r in existing}
            new = [r for r in carried if have.get(r["id"]) != r]
            check_annotations(view, existing + new)
            _append(root, run_id, new)
            (Path(tmp) / run_id).rename(target)
    return run_id


# ---------------------------------------------------------------- annotations

# A person's notes on spans of generated answers, and which answers they read and found fine. One append-only JSONL
# file per run under <data>/review/annotations, outside the run folder and outside git: an edit or a deletion is a
# new record naming the note it changes, so the history of every note stays readable to the agent that fixes it.
# Reading and folding the store lives in error_analysis, which groups the notes into failure types.

ANNOTATION_SCHEMA = "review-annotation-1"
ANNOTATIONS_IN_ZIP = "annotations.jsonl"
FIELD = re.compile(r"^(summary|next_action|claim\.(\d+)|conflict\.(\d+)\.(\d+)|missing\.(\d+))$")
OPS = ("note", "edit", "delete", "reviewed")


def _append(root: Path, run_id: str, records: list[dict]) -> None:
    ea.append_jsonl(annotations_file(root, run_id), records)


def field_text(answer: dict, field: str) -> str | None:
    """The stored answer text a span lies in: what the app renders for that field, character for character."""
    m = FIELD.match(field or "")
    try:
        if not m:
            return None
        if field in ("summary", "next_action"):
            return answer.get(field)
        if m.group(2):
            return answer["claims"][int(m.group(2))]["text"]
        if m.group(3):
            return answer["conflicts"][int(m.group(3))]["alternatives"][int(m.group(4))]["value"]
        return answer["missing"][int(m.group(5))]["field"]
    except (KeyError, IndexError, TypeError):
        return None


def _answer(view: dict, question: str, candidate: str) -> dict:
    if view["stage"] != "generation":
        raise ReviewInputError("only generated answers take span notes")
    q = next((q for q in view["questions"] if q["key"] == question), None)
    if q is None or not isinstance(q["candidates"].get(candidate), dict):
        raise ReviewInputError("no such answer in this run")
    return q["candidates"][candidate]


def check_annotations(view: dict, records: list[dict]) -> None:
    """Every record names this run, a known operation and, for a note, a span whose offsets select exactly its
    text in the stored answer; an edit or deletion names a note recorded earlier."""
    commits = {c["id"]: c["commit"] for c in view["candidates"]}
    notes, ids = set(), set()
    for r in records:
        if not isinstance(r, dict) or r.get("schema") != ANNOTATION_SCHEMA or r.get("run_id") != view["run_id"] \
                or r.get("op") not in OPS or not isinstance(r.get("id"), str) or not r.get("author") or not r.get("at"):
            raise ReviewInputError(f"not a {ANNOTATION_SCHEMA} record of {view['run_id']}")
        if r["id"] in ids:  # an edit or deletion names its note by id, so two records may never share one
            raise ReviewInputError(f"two records share the id {r['id']}")
        ids.add(r["id"])
        if r["op"] in ("note", "reviewed"):
            _answer(view, r.get("question"), r.get("candidate"))
            if r.get("commit") != commits.get(r["candidate"]):
                raise ReviewInputError("the record names another commit than the run's candidate")
        if r["op"] == "note":
            text = field_text(_answer(view, r["question"], r["candidate"]), r.get("field"))
            start, end = r.get("start"), r.get("end")
            if text is None:
                raise ReviewInputError("no such field in that answer")
            if not (isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(text)) \
                    or text[start:end] != r.get("text"):
                raise ReviewInputError("the offsets do not select that text in the stored answer")
            if not str(r.get("note") or "").strip():
                raise ReviewInputError("write what is wrong with the selected text")
            notes.add(r["id"])
        elif r["op"] in ("edit", "delete"):
            if r.get("target") not in notes:
                raise ReviewInputError("that note does not exist or was deleted")
            if r["op"] == "edit" and not str(r.get("note") or "").strip():
                raise ReviewInputError("an edited note cannot be empty; delete it instead")
            if r["op"] == "delete":
                notes.discard(r["target"])
        elif not isinstance(r.get("value"), bool):
            raise ReviewInputError("reviewed is true or false")


def add_annotation(root: Path, view: dict, body: dict, author: str) -> dict:
    """Validate one operation from the app against the stored answers and the existing records, then append it."""
    op = body.get("op")
    record = {"schema": ANNOTATION_SCHEMA, "id": uuid.uuid4().hex, "run_id": view["run_id"], "op": op,
              "author": author, "at": datetime.now(timezone.utc).isoformat()}
    if op in ("note", "reviewed"):
        commit = next((c["commit"] for c in view["candidates"] if c["id"] == body.get("candidate")), None)
        record.update(question=body.get("question"), candidate=body.get("candidate"), commit=commit)
    if op == "note":
        record.update({k: body.get(k) for k in ("field", "start", "end", "text")}, note=str(body.get("note") or "").strip())
    elif op in ("edit", "delete"):
        record["target"] = body.get("target")
        if op == "edit":
            record["note"] = str(body.get("note") or "").strip()
    elif op == "reviewed":
        record["value"] = body.get("value")
    with write_lock(root):
        existing = read_annotations(root, view["run_id"])
        check_annotations(view, existing + [record])
        _append(root, view["run_id"], [record])
        return annotation_state(existing + [record])


def git_author() -> str:
    """The person's git user.name, as the store records it; a note without an author is refused."""
    try:
        name = subprocess.run(["git", "config", "user.name"], cwd=REPO, capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        name = ""
    if not name:
        raise ReviewInputError("set git user.name: every note records who wrote it")
    return name


# ---------------------------------------------------------------- decisions


def _items(view: dict) -> dict[str, str]:
    """Things a note can be attached to: questions (retriever, generation), documents (chunking) or images (ocr),
    with a readable label."""
    if view["stage"] in ("retriever", "generation"):
        return {q["key"]: q["question"] for q in view["questions"]}
    if view["stage"] == "ocr":
        return {str(i["n"]): f"{i['title'] or i['doc_id']} · {i['where']}" for i in view["images"]}
    return {str(d["n"]): d["title"] or d["doc_id"] for d in view["documents"]}


def write_decision(root: Path, view: dict, chosen: str | None, reasons: dict, notes: dict) -> dict:
    """Validate the person's decision on one run and write <stage>-<id>.md and .json under review/decisions."""
    cands = {c["id"]: c for c in view["candidates"]}
    if chosen is not None and (chosen not in cands or cands[chosen]["role"] != "candidate"
                               or cands[chosen]["status"] != "complete"):
        raise ReviewInputError("pick a candidate that completed, or none")
    entries = []
    for c in view["candidates"]:
        reason = str(reasons.get(c["id"]) or "").strip()
        if c["role"] == "baseline":
            outcome = "baseline"
        elif c["id"] == chosen:
            outcome = "chosen"
        elif c["status"] != "complete":
            outcome, reason = "failed", reason or c.get("reason") or "no result"
        else:
            outcome = "rejected"
            if not reason:
                raise ReviewInputError(f"give a reason for rejecting {c['label']}")
        entries.append({"id": c["id"], "label": c["label"], "ref": c["ref"], "commit": c["commit"],
                        "status": c["status"], "outcome": outcome, "reason": reason or None,
                        "variant": c.get("variant") or None})
    items = _items(view)
    unknown = set(notes) - set(items)
    if unknown:
        raise ReviewInputError(f"notes for unknown items: {sorted(unknown)[:3]}")
    kept = [{"item": k, "label": items[k], "note": str(v).strip()} for k, v in notes.items() if str(v).strip()]
    record = {"schema": DECISION_SCHEMA, "stage": view["stage"], "run_id": view["run_id"],
              "decided_at": datetime.now(timezone.utc).isoformat(), "chosen": chosen,
              "candidates": entries, "notes": kept, "summary": view.get("summary")}
    folder = decisions_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    md, js = folder / f"{view['run_id']}.md", folder / f"{view['run_id']}.json"
    # The .json marks the run decided in the list, so it is written last: a failed .md never shows as decided. Both
    # under the write lock, so two servers saving at once never leave one's .md beside the other's .json.
    with write_lock(root):
        md.write_text(decision_markdown(record, runs_dir(root) / view["run_id"]), encoding="utf-8")
        js.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    spans = annotations_file(root, view["run_id"])
    return {"markdown": str(md), "json": str(js), "prompt": agent_prompt(record, md, js, spans if spans.exists() else None)}


def _measure(stage: str, s: dict | None) -> str:
    if not s:
        return "no result"
    if stage == "retriever":
        return (f"nDCG@5 {s['ndcg5'] if s['ndcg5'] is not None else '-'} over {s['ndcg5_n']}/{s['dev_rows']} dev "
                f"questions; needle top-5 hits {s['needle_hits']}/{s['needle_rows']}")
    if stage == "generation":
        failures = ", ".join(f"{k} {v}" for k, v in sorted(s["validation_failures"].items())) or "none"
        metadata = (f"metadata questions correct {s['metadata_correct']}/{s['metadata_rows']}; "
                    if s.get("metadata_rows") else "")
        return (f"passed {s['passed']}/{s.get('passage_rows', s['rows'])}; {metadata}"
                f"required claims correct {s['required_correct']}/{s['required']}; "
                f"of {s['claims']} claims {s['claims_supported']} quoted verbatim, {s['claims_rejected']} rejected, "
                f"{s['claims'] - s['claims_supported'] - s['claims_rejected']} unjudged; validation failures {failures}; "
                f"processing failures {s.get('technical_failures', 0)}; "
                f"{s['cost_micro_usd'] / 1e6:.4f} USD over {s['paid_answers']} paid answers; latency p50 "
                f"{s['latency_ms']['p50']} ms, p95 {s['latency_ms']['p95']} ms (measured {s['latency_ms']['n']} of "
                f"{s['answered']} answered)")
    if stage == "ocr":
        return (f"flagged {s['flagged']}/{s['read']} read images, re-read {s['reread']}/{s['flagged']}, unresolved "
                f"{s['unresolved']}; not OCR'd (unreadable) {s['unreadable']}/{s['images']}; "
                f"{s['spent_micro_usd'] / 1e6:.4f} USD")
    z = s["sizes"]
    return (f"{z['count']} chunks (median {z.get('p50')}, p90 {z.get('p90')}, max {z.get('max')} tokens); table "
            f"titles kept {s['tables_kept']}/{s['tables_titled']}")


def decision_markdown(record: dict, folder: Path) -> str:
    by_id = {c["id"]: c for c in record["candidates"]}
    chosen = by_id.get(record["chosen"])
    lines = [f"# {record['stage'].capitalize()} review decision", "",
             f"- Run: `{record['run_id']}` (folder `{folder}`)", f"- Decided: {record['decided_at']}",
             f"- Outcome: " + (f"chose `{chosen['ref']}` ({chosen['commit']})" if chosen else
                               "no candidate accepted; the baseline stays"), "",
             "## Candidates", "", "| Candidate | Ref | Commit | Status | Decision | Measured | Reason |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for c in record["candidates"]:
        measured = _measure(record["stage"], (record.get("summary") or {}).get(c["id"]))
        reason = (c["reason"] or "-").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {c['label']} | `{c['ref']}` | `{c['commit'][:12]}` | {c['status']} | {c['outcome']} | "
                     f"{measured} | {reason} |")
    if record["notes"]:
        lines += ["", "## Notes", ""]
        for n in record["notes"]:
            lines += [f"### `{n['item']}` {n['label']}", "", n["note"], ""]
    lines += ["", "## Next step", ""]
    if chosen and chosen.get("variant") and record["stage"] == "generation":
        lines.append("The chosen candidate changes configuration through `review-variant.json`. Do not merge that "
                     "file: propose the same values (`generation_reasoning_effort`, `generation_max_output_tokens`) "
                     "for the server configuration (RFP_CONFIG_FILE) in a pull request; merge only its code changes, "
                     "if any.")
    elif chosen and chosen.get("variant"):
        lines.append("The chosen candidate changes configuration through `review-variant.json`. Do not merge that "
                     "file: reproduce the configuration with `compare` and switch serving with `activate-run` after "
                     "the person approves; merge only its code changes, if any.")
    elif chosen:
        lines.append(f"Carry `{chosen['ref']}` forward as a pull request against the baseline; keep the reasons and "
                     "notes above in mind and address them before asking for review.")
    else:
        lines.append("Merge no candidate. Use the rejection reasons and notes above to decide the next attempt.")
    return "\n".join(lines) + "\n"


def agent_prompt(record: dict, md: Path, js: Path, spans: Path | None = None) -> str:
    chosen = next((c for c in record["candidates"] if c["id"] == record["chosen"]), None)
    what = f"The person chose `{chosen['ref']}` ({chosen['commit'][:12]})." if chosen else \
        "The person accepted none of the candidates."
    span = (f" Their notes on spans of individual answers are in {spans} (append-only JSONL, {ANNOTATION_SCHEMA}; "
            "an edit or delete record replaces the note it targets).") if spans else ""
    return (f"Read the {record['stage']} review decision in {md} (JSON: {js}) before changing anything. {what} "
            f"Follow its 'Next step' section and address every rejection reason and note it records.{span}")


# ---------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    root: Path = data_dir()
    port = 0

    def log_message(self, fmt, *args):  # quiet: the launcher window shows only the URL and failures
        pass

    def _send(self, status: int, body: bytes, ctype: str = "application/json", extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def _local(self) -> bool:
        """Only this machine's own pages: a loopback Host (no DNS rebinding) and, for writes, a same-origin page."""
        host = self.headers.get("Host", "")
        if host not in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            return False
        origin = self.headers.get("Origin")
        return self.command == "GET" or origin in (None, f"http://{host}")

    def _guard(self, handler) -> None:
        if not self._local():
            self._json(403, {"error": "only this machine's review page may call this server"})
            return
        try:
            handler()
        except ReviewInputError as exc:
            self._json(400, {"error": str(exc)})
        except FileNotFoundError:
            self._json(404, {"error": "not found"})

    def do_GET(self):  # noqa: N802
        self._guard(self._get)

    def do_POST(self):  # noqa: N802
        self._guard(self._post)

    def _body(self, limit: int) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= limit:
            raise ReviewInputError("empty or oversized request")
        return self.rfile.read(length)

    def _get(self) -> None:
        path = self.path.split("?", 1)[0]
        parts = [p for p in path.split("/") if p]
        if parts[:2] == ["api", "runs"]:
            if len(parts) == 2:
                return self._json(200, list_runs(self.root))
            folder = run_folder(self.root, parts[2])
            if len(parts) == 3:
                return self._send(200, (folder / "view.json").read_bytes())
            if len(parts) == 5 and parts[3] == "docs" and parts[4].isdigit():
                return self._send(200, (folder / "docs" / f"{parts[4]}.json").read_bytes())
            if len(parts) == 5 and parts[3] == "images" and re.fullmatch(r"\d+\.png", parts[4]):
                return self._send(200, (folder / "images" / parts[4]).read_bytes(), TYPES[".png"])
            if len(parts) == 4 and parts[3] == "export":
                return self._send(200, export_run(self.root, parts[2]), "application/zip",
                                  {"Content-Disposition": f'attachment; filename="{parts[2]}.zip"'})
            if len(parts) == 4 and parts[3] == "annotations":
                return self._json(200, annotation_state(read_annotations(self.root, folder.name)))
            if len(parts) == 4 and parts[3] == "failure-types":
                return self._json(200, ea.run_counts(self.root, folder.name))
            if len(parts) == 4 and parts[3] == "decision":
                saved = decisions_dir(self.root) / f"{parts[2]}.json"
                return self._json(200, json.loads(saved.read_text(encoding="utf-8")) if saved.exists() else None)
            raise FileNotFoundError(path)
        if parts == ["api", "taxonomy"]:
            return self._json(200, ea.taxonomy_state(self.root))
        if parts == ["api", "taxonomy", "progress"]:
            last = re.search(r"[?&]last=(\d{1,4})\b", self.path)
            return self._json(200, ea.progress(self.root, int(last.group(1)) if last else 20))
        self._static(path)

    def _post(self) -> None:
        parts = [p for p in self.path.split("?", 1)[0].split("/") if p]
        if parts == ["api", "import"]:
            return self._json(200, {"run_id": import_run(self.root, self._body(MAX_IMPORT))})
        if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "decision":
            view = read_view(self.root, parts[2])
            try:
                body = json.loads(self._body(4 * 1024 ** 2))
            except ValueError:
                raise ReviewInputError("the decision is not JSON") from None
            return self._json(200, write_decision(self.root, view, body.get("chosen"), body.get("reasons") or {},
                                                  body.get("notes") or {}))
        if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "annotations":
            view = read_view(self.root, parts[2])
            try:
                body = json.loads(self._body(1024 ** 2))
            except ValueError:
                raise ReviewInputError("the note is not JSON") from None
            if not isinstance(body, dict):
                raise ReviewInputError("the note is not a JSON object")
            return self._json(200, add_annotation(self.root, view, body, git_author()))
        if parts == ["api", "taxonomy", "export"]:
            return self._json(200, ea.export(self.root))
        if parts == ["api", "taxonomy", "decide"]:
            try:
                body = json.loads(self._body(1024 ** 2))
            except ValueError:
                raise ReviewInputError("the decision is not JSON") from None
            if not isinstance(body, dict):
                raise ReviewInputError("the decision is not a JSON object")
            return self._json(200, ea.decide(self.root, body, git_author()))
        raise FileNotFoundError(self.path)

    def _static(self, path: str) -> None:
        target = (OUT / path.lstrip("/")).resolve()
        if not target.is_relative_to(OUT.resolve()):
            raise FileNotFoundError(path)
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            target = OUT / "404.html"
            if not target.is_file():
                raise FileNotFoundError(path)
            return self._send(404, target.read_bytes(), TYPES[".html"])
        self._send(200, target.read_bytes(), TYPES.get(target.suffix, "application/octet-stream"))


class Server(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # HTTPServer.server_bind reverse-resolves 127.0.0.1 (socket.getfqdn), which can stall a macOS start for ~30 s.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--port", type=int, default=0, help="0 picks a free port")
    p.add_argument("--no-build", action="store_true")
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    if not args.no_build:
        ensure_build()
    server = Server(("127.0.0.1", args.port), Handler)
    Handler.port = server.server_address[1]
    url = f"http://127.0.0.1:{Handler.port}/"
    print(f"Review app: {url}  (runs: {runs_dir(Handler.root)}; Ctrl+C stops it)", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
