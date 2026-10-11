"""Human error analysis: the person's span notes on generated answers, grouped into named failure types they accept.

Standard library only: review/server.py, which must run without the project's environment, imports this module, and
so do the CLI (`error-analysis export | propose`) and the release report. Nothing here calls a model or a paid
service. The coding agent groups an exported bundle of notes into a proposal file; `propose` admits a type only if
it stands on the person's own notes, and the person accepts, renames, merges, splits or rejects every type in the
review app. Counts are answers with a note of a type over answers a person reviewed; they guide what to fix next
and are never a release gate.

For an accepted type the agent may also suggest other spans of it across every stored answer (similar_cases.py): each one
must quote its stored answer verbatim, and only the person's approval turns it into a note (origin ai_suggested),
counted apart from the cases a person found. Suggestions add cases, never types, and do not find every case. An
answer marked reviewed with no issues before a type was accepted is flagged for a second look against it.

Under <data>/review:
  annotations/<run-id>.jsonl        the span notes and reviewed marks (append-only; review/server.py writes them)
  taxonomy/exports/<export-id>/     notes.jsonl (notes in no type yet) and prompt.md, for the coding agent
  taxonomy/proposals/<id>.json      validated proposals; their types wait for the person's decision
  taxonomy/suggest-exports/<id>/    type.json, answers.jsonl and prompt.md: one type's search over stored answers
  taxonomy/suggestions/<id>.json    validated suggestions; each waits for the person's approval or rejection
  taxonomy/history.jsonl            every decision on a type or a suggestion, append-only
  taxonomy/versions/v<N>.json       the accepted taxonomy after each change to its types (an approved suggestion
                                    adds a case to a type without a new version)
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

PROPOSAL_SCHEMA = "failure-taxonomy-proposal-1"
ACTION_SCHEMA = "failure-taxonomy-action-1"
TAXONOMY_SCHEMA = "failure-taxonomy-1"
ANNOTATION_SCHEMA = "review-annotation-1"
AI = "ai_suggested"  # the origin of a note the agent suggested and a person approved; a person's own note has none
THIN = 3  # a type seen in fewer answers than this is marked: too few cases to know what it is
HANGUL = re.compile(r"[가-힣]")
FIELD = re.compile(r"^(summary|next_action|claim\.(\d+)|conflict\.(\d+)\.(\d+)|missing\.(\d+))$")


class ReviewInputError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp(prefix: str) -> str:
    return f"{prefix}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


# ---------------------------------------------------------------- shared store primitives


@contextlib.contextmanager
def write_lock(root: Path):
    """Every write under <data>/review: one at a time across threads and across processes, since the launcher may
    start a second server on another port over the same data folder and the CLI writes proposals. An OS lock on a
    file, released by the OS if the process dies, held around a whole read-check-write."""
    path = root / "review" / ".write.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with WRITE_LOCK, path.open("a+b") as f:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)  # retries for 10 s, then raises: wait again
                    break
                except OSError:
                    continue
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


WRITE_LOCK = threading.Lock()


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_jsonl(path: Path, records: list[dict]) -> None:
    """Add records after the existing ones. The file is replaced whole, so a failed write leaves it as it was
    rather than ending in half a line; nothing already recorded changes."""
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    before = path.read_bytes() if path.exists() else b""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(before + "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records).encode("utf-8"))
    os.replace(tmp, path)


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- the person's notes


def annotations_file(root: Path, run_id: str) -> Path:
    return root / "review" / "annotations" / f"{run_id}.jsonl"


def read_annotations(root: Path, run_id: str) -> list[dict]:
    return read_jsonl(annotations_file(root, run_id))


def unconfirmed_suggestions(root: Path) -> set[str]:
    """Suggestions stored here whose approval is not committed in history.jsonl. A note naming one was written by an
    approval whose history record then failed. A note naming another store's suggestion came with an imported run,
    committed where it was approved (export_run leaves the uncommitted ones out), and stays live."""
    return {s["id"] for s in suggestions(root) if s["status"] != "approved"}


def annotation_state(records: list[dict], unconfirmed: set[str] | frozenset = frozenset()) -> dict:
    """The records folded in order: live notes with their latest text, and answers marked reviewed and fine. A note
    from a suggestion in `unconfirmed` (unconfirmed_suggestions) is not shown, counted or kept by a rejection."""
    notes, reviewed = {}, {}
    for r in records:
        if r["op"] == "note" and r.get("origin") and r.get("suggestion") in unconfirmed:
            continue
        if r["op"] == "note":
            notes[r["id"]] = {k: r[k] for k in ("id", "question", "candidate", "commit", "field", "start", "end",
                                                "text", "note", "author", "at")}
            notes[r["id"]].update({k: r[k] for k in ("origin", "suggestion", "approved_by") if r.get(k)})
        elif r["op"] == "edit" and r["target"] in notes:
            notes[r["target"]].update(note=r["note"], edited_by=r["author"], edited_at=r["at"])
        elif r["op"] == "delete":
            notes.pop(r["target"], None)
        elif r["op"] == "reviewed":
            key = (r["question"], r["candidate"])
            if r["value"]:
                # `at` is the latest mark (a second look renews it); `first_at` keeps when the answer was first read.
                first = reviewed.get(key, {}).get("first_at", r["at"])
                reviewed[key] = {"question": r["question"], "candidate": r["candidate"], "author": r["author"],
                                 "at": r["at"], "first_at": first}
            else:
                reviewed.pop(key, None)
    # The store only grows, so its record count orders snapshots: the app keeps the newest it has seen.
    return {"version": len(records), "notes": list(notes.values()), "reviewed": list(reviewed.values())}


def human(note: dict) -> bool:
    """A note a person found, not a suggestion they approved: only these make an answer reviewed or found."""
    return not note.get("origin")


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


def _runs_with_notes(root: Path) -> dict[str, dict]:
    folder = root / "review" / "annotations"
    if not folder.exists():
        return {}
    # Records first, statuses second, so no lock is needed (callers may hold it): an orphan note read here had its
    # suggestion stored before it was written, so the later read finds it unapproved, or its approval committed.
    records = {p.stem: read_jsonl(p) for p in sorted(folder.glob("*.jsonl"))}
    unconfirmed = unconfirmed_suggestions(root)
    return {run: annotation_state(r, unconfirmed) for run, r in records.items()}


def _view(root: Path, run_id: str) -> dict:
    try:
        return json.loads((root / "review" / "runs" / run_id / "view.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _labels(root: Path, run_id: str) -> tuple[dict, dict]:
    """Question text by key and candidate label by id from the run's view, when the run folder is here."""
    view = _view(root, run_id)
    return ({q["key"]: q.get("question") for q in view.get("questions") or []},
            {c["id"]: c.get("label") for c in view.get("candidates") or []})


def live_notes(root: Path) -> dict[str, dict]:
    """Every live note of every run, by id, with its run, question text and candidate label."""
    out = {}
    for run_id, state in _runs_with_notes(root).items():
        questions, labels = _labels(root, run_id)
        for n in state["notes"]:
            out[n["id"]] = {**n, "run_id": run_id, "question_text": questions.get(n["question"]),
                            "candidate_label": labels.get(n["candidate"], n["candidate"])}
    return out


def reviewed_answers(root: Path) -> list[dict]:
    """Every answer a person reviewed (marked fine or noted), once, ordered by when it first counted as reviewed. An
    approved suggestion does not make its answer reviewed: the person read the span, not the answer."""
    out = []
    for run_id, state in _runs_with_notes(root).items():
        first: dict[tuple, str] = {}
        for r in state["reviewed"]:
            first[(r["question"], r["candidate"])] = r["first_at"]
        for n in filter(human, state["notes"]):
            key = (n["question"], n["candidate"])
            first[key] = min(first.get(key, n["at"]), n["at"])
        out += [{"run_id": run_id, "question": q, "candidate": c, "at": at} for (q, c), at in first.items()]
    return sorted(out, key=lambda a: (a["at"], a["run_id"], a["question"], a["candidate"]))


# ---------------------------------------------------------------- the taxonomy


def taxonomy_dir(root: Path) -> Path:
    return root / "review" / "taxonomy"


def check_type(name, definition, where: str = "") -> tuple[str, str]:
    """A Korean name on one line and a one-sentence definition; returns them trimmed."""
    raw_name, raw_def = str(name or ""), str(definition or "")
    name, definition = raw_name.strip(), " ".join(raw_def.split())
    label = where or name or "a type"
    if not HANGUL.search(name) or "\n" in raw_name or len(name) > 40:
        raise ReviewInputError(f"{label}: the name must be Korean, on one line, at most 40 characters")
    if not definition:
        raise ReviewInputError(f"{label}: give a one-sentence definition")
    if "\n" in raw_def.strip() or re.search(r"[.!?。](?=\s+\S)", definition):
        raise ReviewInputError(f"{label}: the definition must be one sentence")
    return name, definition


def _fold(root: Path) -> dict:
    """Proposals and decisions folded in order: accepted types by id, undecided proposed types by ref
    (<proposal-id>/<n>), the taxonomy version and the decisions."""
    folder = taxonomy_dir(root)
    history = read_jsonl(folder / "history.jsonl")
    accepted: dict[str, dict] = {}
    decided: set[str] = set()
    version = 0
    for a in history:
        kept = {t["id"] for t in a["types"]}
        for ref in a["refs"]:
            if ref in kept and ref in accepted:  # renamed or given a case: replaced below, in its place
                continue
            if accepted.pop(ref, None) is None:
                decided.add(ref)
        for t in a["types"]:
            accepted[t["id"]] = t
        version = a["version"]
    pending = {}
    for path in sorted((folder / "proposals").glob("*.json")) if (folder / "proposals").exists() else []:
        p = json.loads(path.read_text(encoding="utf-8"))
        for i, t in enumerate(p["types"], 1):
            ref = f"{p['proposal_id']}/{i}"
            if ref not in decided:
                pending[ref] = {**t, "ref": ref, "proposal_id": p["proposal_id"], "proposed_at": p["proposed_at"]}
    return {"version": version, "accepted": accepted, "pending": pending, "history": history}


def _assigned(fold: dict) -> dict[str, str]:
    """Note id -> name of the accepted or proposed type holding it. A note belongs to at most one type."""
    out = {}
    for t in [*fold["accepted"].values(), *fold["pending"].values()]:
        for nid in t["notes"]:
            out[nid] = t["name"]
    return out


def taxonomy_state(root: Path) -> dict:
    """What the 실패 유형 screen shows: accepted and proposed types with the quoted spans behind them, the notes in
    no type yet, and the decisions newest first."""
    fold, live = _fold(root), live_notes(root)

    def resolve(t: dict) -> dict:
        notes = [live[n] for n in t["notes"] if n in live]
        found = {(n["run_id"], n["question"], n["candidate"]) for n in notes if human(n)}
        return {**{k: v for k, v in t.items() if k != "notes"}, "notes": notes,
                "deleted": len(t["notes"]) - len(notes), "answers": len(found),
                "ai_answers": len({(n["run_id"], n["question"], n["candidate"]) for n in notes} - found)}

    assigned = _assigned(fold)
    waiting: dict[str, int] = {}
    for s in suggestions(root, fold):
        if s["status"] == "pending":
            waiting[s["type"]] = waiting.get(s["type"], 0) + 1
    # seq counts every committed decision, suggestions' included, so the app keeps the newest of overlapping replies
    return {"version": fold["version"], "seq": len(fold["history"]),
            "accepted": [resolve(t) | {"suggested": waiting.get(t["id"], 0)} for t in fold["accepted"].values()],
            "pending": [resolve(t) for t in fold["pending"].values()],
            "unassigned": sum(1 for nid, n in live.items() if nid not in assigned and human(n)),
            "history": [{k: a.get(k) for k in ("op", "author", "at", "version", "reason")}
                        | {"names": [t["name"] for t in a["types"]], "refs": a["refs"]}
                        for a in reversed(fold["history"]) if not a.get("suggestion")]}


def export(root: Path) -> dict:
    """A bundle for the coding agent: every live note in no accepted or proposed type, and the instructions."""
    fold, live = _fold(root), live_notes(root)
    assigned = _assigned(fold)
    notes = [n for nid, n in live.items() if nid not in assigned and human(n)]
    if not notes:
        raise ReviewInputError("no notes left to group: every note is already in a type, or none was written")
    export_id = _stamp("export")
    folder = taxonomy_dir(root) / "exports" / export_id
    folder.mkdir(parents=True)
    keep = ("id", "run_id", "question_text", "candidate_label", "field", "text", "note", "author", "at")
    (folder / "notes.jsonl").write_text("".join(json.dumps({k: n.get(k) for k in keep}, ensure_ascii=False) + "\n"
                                                for n in notes), encoding="utf-8")
    existing = [f"- {t['name']}: {t['definition']}" for t in [*fold["accepted"].values(), *fold["pending"].values()]]
    proposal = folder / "proposal.json"
    (folder / "prompt.md").write_text("\n".join([
        "# Group a person's answer notes into failure types", "",
        f"`notes.jsonl` holds {len(notes)} notes a person wrote on spans of generated answers: one JSON object per "
        "line with the note `id`, the run, the question, the candidate, the answer field, the quoted span `text` "
        "and the person's own `note` on what is wrong with it.", "",
        "Rules:", "",
        "- Only group these notes. Every type must stand on at least one note from this file. Do not add a type "
        "that no note shows, and do not add notes, examples or cases of your own.",
        "- Put each note in at most one type. Leave out a note that fits no group; a type may hold a single note.",
        "- Name each type in Korean (one line, at most 40 characters) and define it in one sentence that says what "
        "is wrong in the answer, so another reader can tell whether a new note belongs.",
        "- Read and group the notes yourself. Do not call a model API or anything that spends the shared budget.",
        "- Do not reuse a name the person already has:" if existing else "- The person has no types yet.",
        *[f"  {line}" for line in existing], "",
        f"Write the proposal to `{proposal}`:", "",
        "```json",
        json.dumps({"schema": PROPOSAL_SCHEMA, "bundle": export_id, "types": [
            {"name": "<Korean name>", "definition": "<one sentence>", "notes": ["<note id>", "..."]}]},
            ensure_ascii=False),
        "```", "",
        f"Then run `python -m rfp_assistant.cli error-analysis propose --file {proposal}` from the repository root "
        "and fix whatever it refuses. The person decides every type on the review app's 실패 유형 screen.", ""]),
        encoding="utf-8")
    prompt = (f"Read {folder / 'prompt.md'} and follow it: group the {len(notes)} answer notes in "
              f"{folder / 'notes.jsonl'} into failure types, then submit the proposal with "
              f"`python -m rfp_assistant.cli error-analysis propose --file {proposal}`.")
    return {"export_id": export_id, "bundle": str(folder), "notes": len(notes), "prompt": prompt}


def propose(root: Path, proposal) -> dict:
    """Validate the agent's grouping and store it for the person's decision. Each type needs a Korean name, a
    one-sentence definition and at least one existing human note that is in no other type."""
    if not isinstance(proposal, dict) or proposal.get("schema") != PROPOSAL_SCHEMA:
        raise ReviewInputError(f"not a {PROPOSAL_SCHEMA} file")
    if set(proposal) - {"schema", "bundle", "types"}:
        raise ReviewInputError(f"unknown keys: {sorted(set(proposal) - {'schema', 'bundle', 'types'})}")
    types = proposal.get("types")
    if not isinstance(types, list) or not types:
        raise ReviewInputError("propose at least one type")
    with write_lock(root):
        fold, live = _fold(root), live_notes(root)
        assigned = _assigned(fold)
        names = {t["name"] for t in [*fold["accepted"].values(), *fold["pending"].values()]}
        seen: dict[str, str] = {}
        kept = []
        for i, t in enumerate(types, 1):
            if not isinstance(t, dict) or set(t) - {"name", "definition", "notes"}:
                raise ReviewInputError(f"type {i}: only name, definition and notes are allowed")
            name, definition = check_type(t.get("name"), t.get("definition"), f"type {i}")
            ids = t.get("notes")
            if not isinstance(ids, list) or not ids:
                raise ReviewInputError(f"{name}: a type needs at least one of the person's notes; only notes may be "
                                       "grouped, no type may be added without one")
            for nid in ids:
                if not isinstance(nid, str) or nid not in live:
                    raise ReviewInputError(f"{name}: note {nid} does not exist or was deleted")
                if not human(live[nid]):
                    raise ReviewInputError(f"{name}: note {nid} is an approved suggestion; a type stands on the "
                                           "person's own notes")
                if nid in seen:
                    raise ReviewInputError(f"note {nid} is in both {seen[nid]} and {name}")
                if nid in assigned:
                    raise ReviewInputError(f"{name}: note {nid} already belongs to {assigned[nid]}")
                seen[nid] = name
            if name in names:
                raise ReviewInputError(f"{name}: a type of that name already exists")
            names.add(name)
            kept.append({"name": name, "definition": definition, "notes": list(ids)})
        proposal_id = _stamp("proposal")
        path = taxonomy_dir(root) / "proposals" / f"{proposal_id}.json"
        _write_json(path, {"schema": PROPOSAL_SCHEMA, "proposal_id": proposal_id, "bundle": proposal.get("bundle"),
                           "proposed_at": _now(), "types": kept})
    return {"proposal_id": proposal_id, "file": str(path), "types": len(kept), "notes": len(seen),
            "next": "decide each type on the review app's 실패 유형 screen"}


def run_cli(root: Path, action: str, file: str | None) -> dict:
    """`error-analysis export`, `error-analysis propose --file <proposal.json>` or
    `error-analysis suggest --file <suggestions.json>`."""
    if action == "export":
        return export(root)
    if not file:
        raise ReviewInputError(f"error-analysis {action} needs --file <{'proposal' if action == 'propose' else 'suggestions'}.json>")
    path = Path(file)
    if not path.is_file():
        raise ReviewInputError(f"no such file: {path}")
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise ReviewInputError(f"{path} is not JSON") from None
    if action == "suggest":
        from .similar_cases import suggest  # it builds on this module
        return suggest(root, body)
    return propose(root, body)


def decide(root: Path, body: dict, author: str) -> dict:
    """One decision of the person on proposed or accepted types:
    accept {ref, name?, definition?}, rename {ref, name, definition}, merge {refs, name, definition},
    split {ref, parts: [{name, definition, notes}]}, reject {ref, reason?}. Every new or changed type is accepted;
    a change to the accepted set is a new taxonomy version."""
    op = body.get("op")
    if op not in ("accept", "rename", "merge", "split", "reject"):
        raise ReviewInputError("unknown decision")
    with write_lock(root):
        fold, live = _fold(root), live_notes(root)
        items = {**fold["pending"], **fold["accepted"]}
        refs = body.get("refs") if op == "merge" else [body.get("ref")]
        if not isinstance(refs, list) or not refs or any(r not in items for r in refs) or len(set(refs)) != len(refs):
            raise ReviewInputError("that type does not exist or was already decided")
        if op == "merge" and len(refs) < 2:
            raise ReviewInputError("merge two or more types")
        if op == "rename" and refs[0] not in fold["accepted"]:
            raise ReviewInputError("only an accepted type is renamed; accept a proposed one with its new name")
        notes = [n for r in refs for n in items[r]["notes"] if n in live]  # a deleted note is not carried on
        now = _now()
        # Since when the criterion has existed: a reviewed mark older than this was given without it. A rename, merge
        # or split of accepted types keeps the earliest; a type accepted from a proposal starts now.
        since = min([_since(fold["accepted"][r]) for r in refs if r in fold["accepted"]], default=now)

        def new(name, definition, ids, keep_id=None):
            name, definition = check_type(name, definition)
            if not ids:
                raise ReviewInputError(f"{name}: no live note is left in it; reject it instead")
            return {"id": keep_id or uuid.uuid4().hex[:12], "name": name, "definition": definition, "notes": ids,
                    "decided_by": author, "decided_at": now, "since": since}

        if op == "accept":
            t = items[refs[0]]
            if refs[0] not in fold["pending"]:
                raise ReviewInputError("that type is already accepted")
            types = [new(body.get("name") or t["name"], body.get("definition") or t["definition"], notes)]
        elif op == "rename":
            types = [new(body.get("name"), body.get("definition"), notes, keep_id=refs[0])]
        elif op == "merge":
            types = [new(body.get("name"), body.get("definition"), notes)]
        elif op == "split":
            parts = body.get("parts")
            if not isinstance(parts, list) or len(parts) < 2 or not all(isinstance(p, dict) for p in parts):
                raise ReviewInputError("split into two or more parts")
            given = [n for p in parts for n in (p.get("notes") or [])]
            if not all(isinstance(n, str) for n in given) or sorted(given) != sorted(notes):
                raise ReviewInputError("put every note of the type in exactly one part")
            types = [new(p.get("name"), p.get("definition"), list(p.get("notes") or [])) for p in parts]
        else:
            types = []
        remaining = {t["name"] for i, t in fold["accepted"].items() if i not in refs}
        names = [t["name"] for t in types]
        if len(set(names)) != len(names) or remaining & set(names):
            raise ReviewInputError("an accepted type already has that name")
        changes = bool(types) or any(r in fold["accepted"] for r in refs)
        record = {"schema": ACTION_SCHEMA, "id": uuid.uuid4().hex, "op": op, "author": author, "at": now,
                  "refs": refs, "types": types, "version": fold["version"] + (1 if changes else 0),
                  "reason": str(body.get("reason") or "").strip() or None}
        # The history record is the commit, so the snapshot is written first: a failed snapshot commits nothing and
        # the same decision can be sent again. A snapshot left by a failed append is replaced by the next decision
        # that reaches its version, since only history.jsonl counts.
        if changes:
            accepted = [t for i, t in fold["accepted"].items() if i not in refs] + types
            _write_json(taxonomy_dir(root) / "versions" / f"v{record['version']}.json",
                        {"schema": TAXONOMY_SCHEMA, "version": record["version"], "at": now, "by": author,
                         "change": op, "types": accepted})
        append_jsonl(taxonomy_dir(root) / "history.jsonl", [record])
    return taxonomy_state(root)


# ---------------------------------------------------------------- counts


def _type_answers(root: Path) -> tuple[dict, dict[str, set], dict[str, set]]:
    """The accepted types (version folded in) and, for each, the answers (run, question, candidate) a person found
    it in (a note of their own) and the further answers it holds only through an approved suggestion. Only the first
    are a rate over reviewed answers: suggestions were searched across every stored answer and miss cases."""
    fold, live = _fold(root), live_notes(root)
    found, suggested = {}, {}
    for tid, t in fold["accepted"].items():
        notes = [live[n] for n in t["notes"] if n in live]
        found[tid] = {(n["run_id"], n["question"], n["candidate"]) for n in notes if human(n)}
        suggested[tid] = {(n["run_id"], n["question"], n["candidate"]) for n in notes} - found[tid]
    return fold, found, suggested


def progress(root: Path, last: int = 20) -> dict:
    """Breadth (how many types), depth (answers per type, thin ones marked), saturation (how many types first
    appeared among the last `last` reviewed answers) and the answers marked fine before a type that wait for a
    second look."""
    fold, answers, ai = _type_answers(root)
    reviewed = reviewed_answers(root)
    order = {(a["run_id"], a["question"], a["candidate"]): i for i, a in enumerate(reviewed)}
    window = min(max(1, last), len(reviewed)) if reviewed else 0
    again = rereview(root)
    types = []
    for tid, t in fold["accepted"].items():
        seen = [order[a] for a in answers[tid] if a in order]
        types.append({"id": tid, "name": t["name"], "definition": t["definition"], "answers": len(answers[tid]),
                      "ai_answers": len(ai[tid]), "thin": len(answers[tid]) < THIN,
                      "first": min(seen) + 1 if seen else None, "runs": sorted({a[0] for a in answers[tid]}),
                      "rereview": sum(1 for a in again if t["name"] in a["types"])})
    types.sort(key=lambda t: (-t["answers"], t["name"]))
    new = [t["name"] for t in types if t["first"] is not None and t["first"] > len(reviewed) - window]
    state = taxonomy_state(root)
    return {"version": fold["version"], "reviewed": len(reviewed), "breadth": len(types), "types": types,
            "last": window, "new_in_last": len(new), "new_names": new, "thin": THIN,
            "pending": len(state["pending"]), "unassigned": state["unassigned"], "rereview": len(again),
            "rereview_runs": sorted({a["run_id"] for a in again})}


def run_counts(root: Path, run_id: str) -> dict:
    """For one run's candidates: answers a person found each accepted type in, over that candidate's reviewed
    answers, and apart from them the answers holding it through an approved suggestion."""
    fold, answers, ai = _type_answers(root)
    reviewed: dict[str, int] = {}
    for a in reviewed_answers(root):
        if a["run_id"] == run_id:
            reviewed[a["candidate"]] = reviewed.get(a["candidate"], 0) + 1

    def by(keys):
        out: dict[str, int] = {}
        for run, _, cand in keys:
            if run == run_id:
                out[cand] = out.get(cand, 0) + 1
        return out
    types = [{"id": tid, "name": t["name"], "definition": t["definition"], "by_candidate": by(answers[tid]),
              "ai_by_candidate": by(ai[tid])} for tid, t in fold["accepted"].items()]
    return {"version": fold["version"], "reviewed": reviewed, "types": types}


def release_summary(root: Path) -> dict:
    """The release report's human error analysis: types, counts, denominators and source runs. Not a sealed
    metric and not a gate."""
    fold, answers, ai = _type_answers(root)
    reviewed: dict[str, int] = {}
    for a in reviewed_answers(root):
        reviewed[a["run_id"]] = reviewed.get(a["run_id"], 0) + 1
    types = []
    for tid, t in fold["accepted"].items():
        per_run: dict[str, int] = {}
        for run, _, _ in answers[tid]:
            per_run[run] = per_run.get(run, 0) + 1
        types.append({"name": t["name"], "definition": t["definition"], "answers": len(answers[tid]),
                      "ai_answers": len(ai[tid]), "per_run": per_run})
    types.sort(key=lambda t: (-t["answers"], t["name"]))
    return {"sealed_metric": False, "gate": False, "version": fold["version"], "reviewed": sum(reviewed.values()),
            "reviewed_by_run": reviewed, "types": types, "pending": len(fold["pending"]), "thin": THIN}


def release_lines(summary: dict) -> list[str]:
    L = ["", "## Human error analysis (not a sealed metric)", "",
         "- Failure types the person named and accepted from their own span notes on stage-review generation runs "
         "(development questions). They say what to fix next; they are not a release gate, not a sealed-set "
         "measurement and do not move any pass line above."]
    if not summary["types"]:
        return L + ["- no accepted failure type yet: write notes in the review app, then "
                    "`python -m rfp_assistant.cli error-analysis export` and decide the proposal on 실패 유형"]
    runs = ", ".join(f"`{r}` {n}" for r, n in sorted(summary["reviewed_by_run"].items()))
    L += [f"- taxonomy version v{summary['version']}; reviewed answers {summary['reviewed']} ({runs}); "
          f"proposed types not yet decided {summary['pending']}",
          "- The rate counts only answers a person found the type in. Approved AI suggestions are further answers "
          "the coding agent proposed across every stored answer and the person approved one by one; they are not a "
          "rate and the search does not find every case.", "",
          "| Failure type | Definition | Answers with this type / reviewed | Source runs (answers / reviewed) "
          "| Further answers from approved AI suggestions |",
          "| --- | --- | --- | --- | --- |"]
    for t in summary["types"]:
        thin = f" (fewer than {summary['thin']} cases)" if t["answers"] < summary["thin"] else ""
        sources = ", ".join(f"`{r}` {n}/{summary['reviewed_by_run'].get(r, 0)}" for r, n in sorted(t["per_run"].items()))
        definition = t["definition"].replace("|", "\\|")
        L.append(f"| {t['name']} | {definition} | {t['answers']}/{summary['reviewed']}{thin} | {sources or '-'} "
                 f"| {t.get('ai_answers', 0)} |")
    return L


# ---------------------------------------------------------------- similar cases the agent suggests


def _since(t: dict) -> str:
    """When a type's criterion began; a type accepted before this was recorded began when it was decided."""
    return t.get("since") or t["decided_at"]


def suggestions(root: Path, fold: dict | None = None) -> list[dict]:
    """Every stored suggestion, by id <batch>/<n>, with its status from history: pending, approved or rejected."""
    fold = fold or _fold(root)
    decided = {a["suggestion"]: a for a in fold["history"] if a.get("suggestion")}
    folder = taxonomy_dir(root) / "suggestions"
    out = []
    for path in sorted(folder.glob("*.json")) if folder.exists() else []:
        batch = json.loads(path.read_text(encoding="utf-8"))
        for i, s in enumerate(batch["suggestions"], 1):
            sid = f"{batch['batch_id']}/{i}"
            a = decided.get(sid)
            out.append({**s, "id": sid, "type": batch["type"], "type_name": batch["type_name"],
                        "suggested_at": batch["suggested_at"],
                        "status": "pending" if a is None else "approved" if a["op"] == "suggestion-approve" else "rejected",
                        "decided_by": a and a["author"], "decided_at": a and a["at"], "reason_rejected": a and a["reason"]})
    return out


def rereview(root: Path, run_id: str | None = None) -> list[dict]:
    """Answers a person marked reviewed with no issues (and no note of their own) before an accepted type's
    criterion existed, with the names of those types. Marking the answer again, or clearing the mark, settles it."""
    since = [(_since(t), t["name"]) for t in _fold(root)["accepted"].values()]
    out = []
    for run, state in _runs_with_notes(root).items():
        if run_id not in (None, run):
            continue
        noted = {(n["question"], n["candidate"]) for n in state["notes"] if human(n)}
        for r in state["reviewed"]:
            names = sorted(name for at, name in since if r["at"] < at)
            if names and (r["question"], r["candidate"]) not in noted:
                out.append({"run_id": run, "question": r["question"], "candidate": r["candidate"], "types": names})
    return out
