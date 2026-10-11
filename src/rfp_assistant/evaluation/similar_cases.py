"""Further cases of an accepted failure type: the coding agent searches every stored answer, the person decides each.

Standard library only, like error_analysis, whose store this writes into. `suggest_export` writes one type's search
bundle under <data>/review/taxonomy/suggest-exports/<id>/ (type.json, answers.jsonl, prompt.md). `suggest` admits
the agent's file only if every span is verbatim in its stored answer field; the suggestions wait in
taxonomy/suggestions/<id>.json. `decide_suggestion` approves one into a note (origin ai_suggested, approved_by the
person) filed under the type, or rejects it with a reason the next bundle carries. Suggestions add cases, never
types, and do not find every case. Their status is folded from history.jsonl by error_analysis.suggestions.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from .error_analysis import (
    ACTION_SCHEMA, AI, ANNOTATION_SCHEMA, SOURCES, ReviewInputError, _fold, _labels, _now, _stamp, _view, _write_json,
    annotations_file, append_jsonl, field_text, live_notes, read_annotations, reviewed_answers, suggestions,
    taxonomy_dir, write_lock)

SUGGESTION_SCHEMA = "failure-type-suggestions-1"
RUN_FOLDER = re.compile(rf"^({'|'.join(SOURCES)})-\d{{8}}T\d{{6}}Z-[0-9a-f]{{6}}$")


def answer_fields(answer: dict) -> dict[str, str]:
    """Every field of a stored answer that takes span notes, by field id, as field_text reads it."""
    ids = ["summary", "next_action", *(f"claim.{i}" for i in range(len(answer.get("claims") or []))),
           *(f"conflict.{i}.{j}" for i, c in enumerate(answer.get("conflicts") or [])
             for j in range(len(c.get("alternatives") or []))),
           *(f"missing.{i}" for i in range(len(answer.get("missing") or [])))]
    return {f: t for f in ids if isinstance(t := field_text(answer, f), str) and t}


def _stored_answer(root: Path, run_id: str, question: str, candidate: str) -> tuple[dict, dict | None]:
    """The generation or traffic run's view and one of its stored answers (None when there is no such answer)."""
    view = _view(root, run_id) if isinstance(run_id, str) and RUN_FOLDER.match(run_id) else {}
    q = next((q for q in view.get("questions") or [] if q.get("key") == question), None) \
        if view.get("stage") in SOURCES else None
    answer = (q or {}).get("candidates", {}).get(candidate)
    return view, answer if isinstance(answer, dict) else None


def _stored_answers(root: Path) -> list[dict]:
    """Every answer of every generation and traffic run here, reviewed or not, with each field that takes span
    notes."""
    reviewed = {(a["run_id"], a["question"], a["candidate"]) for a in reviewed_answers(root)}
    out = []
    folder = root / "review" / "runs"
    for run in sorted(p.name for p in folder.iterdir() if RUN_FOLDER.match(p.name)) if folder.exists() else []:
        view = _view(root, run)
        labels = {c["id"]: c.get("label") for c in view.get("candidates") or []}
        for q in view.get("questions") or []:
            for cand, answer in (q.get("candidates") or {}).items():
                fields = answer_fields(answer) if isinstance(answer, dict) else {}
                if fields:
                    out.append({"run_id": run, "question": q["key"], "question_text": q.get("question"),
                                "candidate": cand, "candidate_label": labels.get(cand, cand),
                                "reviewed": (run, q["key"], cand) in reviewed, "fields": fields})
    return out


def suggest_export(root: Path, type_id: str) -> dict:
    """A bundle for the coding agent to find other answers showing one accepted type: the type's definition, its
    approved examples (the person's notes and suggestions they approved), the suggestions they rejected with their
    reasons and those still waiting, and every stored answer, reviewed or not."""
    fold, live = _fold(root), live_notes(root)
    t = fold["accepted"].get(type_id)
    if t is None:
        raise ReviewInputError("no accepted failure type has that id")
    answers = _stored_answers(root)
    if not answers:
        raise ReviewInputError("no stored generation answers to search")
    export_id = _stamp("suggest")
    folder = taxonomy_dir(root) / "suggest-exports" / export_id
    folder.mkdir(parents=True)
    keep = ("run_id", "question", "question_text", "candidate", "candidate_label", "field", "start", "end", "text")
    labels = {a["run_id"]: _labels(root, a["run_id"]) for a in answers}
    mine = [{**s, "question_text": labels.get(s["run_id"], ({}, {}))[0].get(s["question"]),
             "candidate_label": labels.get(s["run_id"], ({}, {}))[1].get(s["candidate"], s["candidate"])}
            for s in suggestions(root, fold) if s["type"] == type_id]
    _write_json(folder / "type.json", {
        "id": type_id, "name": t["name"], "definition": t["definition"],
        "approved": [{**{k: live[n].get(k) for k in keep}, "note": live[n]["note"],
                      "origin": live[n].get("origin") or "human"} for n in t["notes"] if n in live],
        "rejected": [{**{k: s.get(k) for k in keep}, "reason": s["reason"], "why_rejected": s["reason_rejected"]}
                     for s in mine if s["status"] == "rejected"],
        "waiting": [{k: s.get(k) for k in keep} for s in mine if s["status"] == "pending"]})
    (folder / "answers.jsonl").write_text("".join(json.dumps(a, ensure_ascii=False) + "\n" for a in answers),
                                          encoding="utf-8")
    out = folder / "suggestions.json"
    (folder / "prompt.md").write_text("\n".join([
        f"# Find other answers showing the failure type “{t['name']}”", "",
        f"`type.json` holds the type the person accepted: its definition, the {len(t['notes'])} spans already filed "
        "under it (`approved`), the spans the person rejected as not this type with their reasons (`rejected`) and "
        "suggestions still waiting for them (`waiting`). `answers.jsonl` holds every stored generated answer, "
        f"{len(answers)} of them, reviewed by a person or not: one JSON object per line with the run, question, "
        "candidate and each `fields` entry (field id -> the exact stored text).", "",
        "Rules:", "",
        "- Find spans that fit this definition as the approved examples show it. Read the rejected ones and their "
        "reasons, and do not suggest the same kind of span again.",
        "- Only this type. Do not propose a new type, rename or redefine it, or file a span under another type.",
        "- Quote each span exactly as it stands in its field; `suggest` refuses any text that is not verbatim in "
        "the stored answer. Give `start` (a character offset in the field, as Python counts) only when the text "
        "occurs more than once in that field.",
        "- Skip spans already approved, rejected or waiting. Give each suggestion a one-line reason in Korean that "
        "says why it fits the definition.",
        "- Read the answers yourself. Do not call a model API or anything that spends the shared budget. You will "
        "not find every case, and nothing becomes a case until the person approves it.", "",
        f"Write the suggestions to `{out}`:", "",
        "```json",
        json.dumps({"schema": SUGGESTION_SCHEMA, "bundle": export_id, "type": type_id, "suggestions": [
            {"run_id": "<run>", "question": "<question key>", "candidate": "<candidate id>", "field": "<field id>",
             "text": "<exact span>", "reason": "<one line>"}]}, ensure_ascii=False),
        "```", "",
        f"Then run `python -m rfp_assistant.cli error-analysis suggest --file {out}` from the repository root and "
        "fix whatever it refuses. The person approves or rejects each suggestion on the review app's 실패 유형 "
        "screen.", ""]), encoding="utf-8")
    prompt = (f"Read {folder / 'prompt.md'} and follow it: find other answers in {folder / 'answers.jsonl'} showing "
              f"the failure type “{t['name']}” defined in {folder / 'type.json'}, then submit them with "
              f"`python -m rfp_assistant.cli error-analysis suggest --file {out}`.")
    return {"export_id": export_id, "bundle": str(folder), "type": type_id, "answers": len(answers), "prompt": prompt}


def _span(text: str, quote, start) -> tuple[int, int]:
    """Where a quoted span lies in its field, refusing text that is not verbatim there or is ambiguous."""
    if not isinstance(quote, str) or not quote.strip():
        raise ReviewInputError("quote the span's text")
    if start is not None:
        if not isinstance(start, int) or isinstance(start, bool) or start < 0 or text[start:start + len(quote)] != quote:
            raise ReviewInputError(f"“{quote}” is not at offset {start} of its stored field")
        return start, start + len(quote)
    hits = [i for i in range(len(text)) if text.startswith(quote, i)]  # str.count skips overlapping occurrences
    if not hits:
        raise ReviewInputError(f"“{quote}” is not verbatim in its stored answer field")
    if len(hits) > 1:
        raise ReviewInputError(f"“{quote}” occurs {len(hits)} times in its field; give start")
    return hits[0], hits[0] + len(quote)


def suggest(root: Path, body) -> dict:
    """Validate the agent's suggestions of further cases of one accepted type and store them for the person. Each
    names a stored generation answer, a field of it, a span verbatim in that field and a one-line reason; nothing
    becomes a note until the person approves it."""
    allowed = {"schema", "bundle", "type", "suggestions"}
    if not isinstance(body, dict) or body.get("schema") != SUGGESTION_SCHEMA:
        raise ReviewInputError(f"not a {SUGGESTION_SCHEMA} file")
    if set(body) - allowed:
        raise ReviewInputError(f"unknown keys: {sorted(set(body) - allowed)}")
    items = body.get("suggestions")
    if not isinstance(items, list) or not items:
        raise ReviewInputError("suggest at least one span; if you found none, tell the person so")
    with write_lock(root):
        fold, live = _fold(root), live_notes(root)
        t = fold["accepted"].get(body.get("type"))
        if t is None:
            raise ReviewInputError(f"unknown failure type {body.get('type')!r}: suggest only for an accepted type")
        mine = [s for s in suggestions(root, fold) if s["type"] == t["id"]]
        filed = [live[n] for n in t["notes"] if n in live]
        kept, seen = [], set()
        for i, s in enumerate(items, 1):
            if not isinstance(s, dict) or set(s) - {"run_id", "question", "candidate", "field", "text", "start", "reason"}:
                raise ReviewInputError(f"suggestion {i}: only run_id, question, candidate, field, text, start and "
                                       "reason are allowed")
            _, answer = _stored_answer(root, s.get("run_id"), s.get("question"), s.get("candidate"))
            if answer is None:
                raise ReviewInputError(f"suggestion {i}: no stored generation answer {s.get('run_id')} / "
                                       f"{s.get('question')} / {s.get('candidate')}")
            text = field_text(answer, s.get("field"))
            if not text:
                raise ReviewInputError(f"suggestion {i}: that answer has no field {s.get('field')!r}")
            try:
                start, end = _span(text, s.get("text"), s.get("start"))
            except ReviewInputError as exc:
                raise ReviewInputError(f"suggestion {i}: {exc}") from None
            reason = s.get("reason")
            if not isinstance(reason, str) or not reason.strip() or "\n" in reason.strip() or len(reason.strip()) > 200:
                raise ReviewInputError(f"suggestion {i}: give a one-line reason (at most 200 characters)")
            where = (s["run_id"], s["question"], s["candidate"], s["field"])
            if any((n["run_id"], n["question"], n["candidate"], n["field"]) == where and n["start"] < end
                   and start < n["end"] for n in filed):
                raise ReviewInputError(f"suggestion {i}: that span is already a case of {t['name']}")
            same = next((m for m in mine if (m["run_id"], m["question"], m["candidate"], m["field"], m["start"],
                                             m["end"]) == (*where, start, end)), None)
            if same and same["status"] == "rejected":
                raise ReviewInputError(f"suggestion {i}: the person already rejected that span"
                                       + (f" ({same['reason_rejected']})" if same["reason_rejected"] else ""))
            if same or (*where, start, end) in seen:
                raise ReviewInputError(f"suggestion {i}: that span is already suggested")
            seen.add((*where, start, end))
            kept.append({"run_id": s["run_id"], "question": s["question"], "candidate": s["candidate"],
                         "field": s["field"], "start": start, "end": end, "text": s["text"], "reason": reason.strip()})
        batch_id = _stamp("suggestions")
        path = taxonomy_dir(root) / "suggestions" / f"{batch_id}.json"
        _write_json(path, {"schema": SUGGESTION_SCHEMA, "batch_id": batch_id, "bundle": body.get("bundle"),
                           "type": t["id"], "type_name": t["name"], "suggested_at": _now(), "suggestions": kept})
    return {"batch_id": batch_id, "file": str(path), "type": t["name"], "suggestions": len(kept),
            "next": "the person approves or rejects each one on the review app's 실패 유형 screen"}


def suggestion_queue(root: Path, type_id: str) -> dict:
    """One type's suggestions for the person: each waiting one inside its answer field, so the span shows in
    context, and the rejected ones with their reasons."""
    fold = _fold(root)
    t = fold["accepted"].get(type_id)
    if t is None:
        raise ReviewInputError("no accepted failure type has that id")
    mine = [s for s in suggestions(root, fold) if s["type"] == type_id]
    waiting = []
    for s in mine:
        if s["status"] != "pending":
            continue
        view, answer = _stored_answer(root, s["run_id"], s["question"], s["candidate"])
        q = next((q for q in view.get("questions") or [] if q.get("key") == s["question"]), {})
        label = next((c.get("label") for c in view.get("candidates") or [] if c["id"] == s["candidate"]), s["candidate"])
        waiting.append({**s, "question_text": q.get("question"), "candidate_label": label,
                        "field_text": field_text(answer, s["field"]) if answer else None})
    return {"seq": len(fold["history"]), "type": {"id": type_id, "name": t["name"], "definition": t["definition"]},
            "pending": waiting,
            "approved": sum(s["status"] == "approved" for s in mine),
            "rejected": [s for s in mine if s["status"] == "rejected"]}


def decide_suggestion(root: Path, body: dict, author: str) -> dict:
    """approve {suggestion}: the span becomes a note (origin ai_suggested, approved_by the person) filed under the
    type; reject {suggestion, reason?}: kept with the reason, shown to the agent the next time it is asked. Neither
    changes the taxonomy version."""
    op, sid = body.get("op"), body.get("suggestion")
    if op not in ("approve", "reject"):
        raise ReviewInputError("approve or reject")
    with write_lock(root):
        fold = _fold(root)
        s = next((s for s in suggestions(root, fold) if s["id"] == sid), None)
        if s is None or s["status"] != "pending":
            raise ReviewInputError("that suggestion does not exist or was already decided")
        t = fold["accepted"].get(s["type"])
        if t is None:
            raise ReviewInputError(f"{s['type_name']} was merged, split or rejected since; ask the agent again for "
                                   "the type it became")
        now = _now()
        record = {"schema": ACTION_SCHEMA, "id": uuid.uuid4().hex, "op": f"suggestion-{op}", "author": author,
                  "at": now, "refs": [], "types": [], "version": fold["version"], "suggestion": sid,
                  "reason": str(body.get("reason") or "").strip() or None}
        if op == "approve":
            view, answer = _stored_answer(root, s["run_id"], s["question"], s["candidate"])
            if answer is None or (field_text(answer, s["field"]) or "")[s["start"]:s["end"]] != s["text"]:
                raise ReviewInputError("the stored answer no longer holds that span")
            # The note goes in first and the history record commits it: until then annotation_state hides it. A
            # note left by a failed commit is found among the raw records by its suggestion id and reused when the
            # approval is sent again; a rejection leaves it hidden for good.
            nid = next((r["id"] for r in read_annotations(root, s["run_id"])
                        if r["op"] == "note" and r.get("suggestion") == sid), None)
            if nid is None:
                nid = uuid.uuid4().hex
                commit = next(c["commit"] for c in view["candidates"] if c["id"] == s["candidate"])
                append_jsonl(annotations_file(root, s["run_id"]), [{
                    "schema": ANNOTATION_SCHEMA, "id": nid, "run_id": s["run_id"], "op": "note", "author": author,
                    "at": now, "question": s["question"], "candidate": s["candidate"], "commit": commit,
                    "field": s["field"], "start": s["start"], "end": s["end"], "text": s["text"], "note": s["reason"],
                    "origin": AI, "suggestion": sid, "approved_by": author}])
            record.update(refs=[t["id"]], types=[{**t, "notes": [*t["notes"], nid]}])
        append_jsonl(taxonomy_dir(root) / "history.jsonl", [record])
    return suggestion_queue(root, s["type"])
