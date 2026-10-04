"""Drafted-question review: approved rows go to the dataset file, rejected rows to the rejection wiki.

The `gold_candidates` table is the only authority, with `gold_reviews` as its append-only review log. The dataset
file, every rejection page and the wiki index are rendered from them inside the deciding transaction, so a file
never disagrees with the database; `check` proves that byte for byte and `sync` repairs a crash between the file
write and the commit.

Two row schemas share the queue: the phase-2 pilot rows (`dev-pilot`) and the phase-4 gold rows (`gold-2`, datasets
`dev` and the sealed `test`). Sealed candidates, their batch files and rejection pages live under `sealed/`; the
review screen never lists them, so only the owner's CLI (the sealed evaluator) can review them.

Loop for the drafting agent (.wiki/gold-drafting.md): read the rejection wiki, record an inferred reason for
every rejection with `infer`, then `submit` a new batch. `submit` refuses while any rejection lacks an
inferred reason, and refuses a question that was already drafted for the same document.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path

from .evaluation import (GOLD_DATASETS, METADATA_TYPES, OPERATIONAL_TYPES, SEALED_SPLITS, GoldChecker, RowChecker,
                         dataset_path, split_of)
from .ingestion import CODE_RE, QUARANTINE_TEXT, nfc
from .postgres import Connection, Row
from .settings import Settings
from .store import dumps, open_db, tx, utcnow, write_jsonl_atomic, write_text_atomic

# code -> Korean label shown to the reviewer
REJECT_CATEGORIES = {
    "leading_question": "질문에 답이나 힌트가 드러남",
    "wrong_answer": "기대 답변이 원문과 다름",
    "evidence_mismatch": "근거 인용이 답을 뒷받침하지 않음",
    "wrong_type": "질문 유형 분류가 틀림",
    "too_easy": "너무 쉬움 (앞부분·목차 수준)",
    "unnatural": "실제 컨설턴트가 묻지 않을 질문",
    "ambiguous": "질문이 모호함",
    "duplicate": "다른 질문과 중복",
    "other": "기타 (메모 필수)",
}
INFERENCE_FIELDS = ("cause", "lesson", "drafting_rule")
ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
CONTEXT_CHARS = 3000


class GoldError(RuntimeError):
    pass


def is_gold(dataset: str | None) -> bool:
    return dataset in GOLD_DATASETS


def is_sealed(dataset: str | None) -> bool:
    return dataset in SEALED_SPLITS


def _area(settings: Settings, sealed: bool) -> Path:
    return settings.data_dir / ("sealed" if sealed else "datasets")


def rejections_dir(settings: Settings, dataset: str | None = None) -> Path:
    return _area(settings, is_sealed(dataset)) / "rejections"


def batch_path(settings: Settings, batch_id: str, dataset: str | None = None) -> Path:
    return _area(settings, is_sealed(dataset)) / "candidates" / f"{batch_id}.jsonl"


def _sha(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def candidate_id_of(row: dict, dataset: str) -> str:
    """Pilot rows carry their own id; a gold row is `<question_id>-r<revision>`, so a correction is a new
    candidate under the same stable question ID."""
    if is_gold(dataset):
        return f"{row.get('question_id')}-r{row.get('revision')}"
    return str(row.get("id", ""))


def drafter_of(row: dict) -> str:
    if "review" in row and isinstance(row["review"], dict):
        return str(row["review"].get("drafted_by") or "")
    return str(row.get("drafted_by") or "")


def doc_ids_of(row: dict) -> list[str]:
    if isinstance(row.get("scope"), list):
        return [s.get("doc_id") for s in row["scope"] if isinstance(s, dict)]
    return [row.get("doc_id")]


def question_key(row: dict) -> str:
    """Same document(s) and same question text, ignoring whitespace."""
    return ",".join(sorted(str(d) for d in doc_ids_of(row))) + "|" + re.sub(r"\s+", "", nfc(str(row.get("question", ""))))


def row_checker(settings: Settings, conn, dataset: str):
    return GoldChecker(settings, conn) if is_gold(dataset) else RowChecker(settings, conn)


def check_row(checker, row: dict, tag: str, dataset: str) -> list[str]:
    if isinstance(checker, GoldChecker):
        return checker.check(row, tag, split=split_of(dataset), require_review=False)
    return checker.check(row, tag)


# ---------------------------------------------------------------- source context


def _document_context(conn: Connection, doc_id: str | None) -> dict | None:
    doc = conn.execute(
        "SELECT d.doc_id, d.filename, d.normalized_metadata_json, s.format, s.parse_status, "
        "s.review_status, s.reason_code FROM documents d "
        "JOIN sources s ON s.source_hash = d.active_source_hash WHERE d.doc_id = ?", (doc_id,)).fetchone()
    if doc is None:
        return None
    meta = json.loads(doc["normalized_metadata_json"])
    return {"doc_id": doc["doc_id"], "filename": doc["filename"], "title": meta.get("title"),
            "institution": meta.get("institution"), "format": doc["format"], "parse_status": doc["parse_status"],
            "review_status": doc["review_status"], "unavailable_reason": QUARANTINE_TEXT.get(doc["reason_code"] or "")}


def _evidence_context(conn: Connection, extraction_id: str | None, ev: dict) -> dict:
    el = conn.execute("SELECT element_id, kind, raw_text, location_json FROM elements WHERE extraction_id = ? "
                      "AND element_id = ?", (extraction_id, ev.get("element_id"))).fetchone()
    return {"element_id": ev.get("element_id"), "quote": ev.get("quote"),
            "kind": el["kind"] if el else None, "location": json.loads(el["location_json"]) if el else None,
            "text": el["raw_text"][:CONTEXT_CHARS] if el else None}


def source_context(conn: Connection, row: dict) -> dict:
    """What the reviewer compares the row against: document(s), source state, cited elements, CSV fields."""
    if isinstance(row.get("scope"), list):  # gold-2: every scoped document and every evidence alternative
        docs = [_document_context(conn, s.get("doc_id")) for s in row["scope"] if isinstance(s, dict)]
        out = {"document": docs[0] if docs else None, "documents": docs, "evidence": []}
        for g in row.get("evidence_groups") or []:
            for alt in g.get("alternatives") or []:
                out["evidence"].append({**_evidence_context(conn, alt.get("extraction_id"), alt),
                                        "group_id": g.get("group_id"), "doc_id": g.get("doc_id")})
        if row.get("mode") == "metadata":
            fields = row.get("metadata_fields") or []
            out["metadata"], out["metadata_conflicts"] = {}, []
            for s in row["scope"]:
                d = conn.execute("SELECT normalized_metadata_json, quality_json FROM documents WHERE doc_id = ?",
                                 (s.get("doc_id"),)).fetchone()
                if d:
                    meta, quality = json.loads(d[0]), json.loads(d[1])
                    out["metadata"][s["doc_id"]] = {f: meta.get(f) for f in fields}
                    out["metadata_conflicts"] += [c for c in quality.get("provenance_conflicts", [])
                                                  if c.get("field") in fields]
        return out
    document = _document_context(conn, row.get("doc_id"))
    if document is None:
        return {"document": None}
    out = {"document": document,
           "evidence": [_evidence_context(conn, row.get("extraction_id"), ev) for ev in row.get("evidence") or []]}
    if row.get("type") in METADATA_TYPES:
        d = conn.execute("SELECT normalized_metadata_json, quality_json FROM documents WHERE doc_id = ?",
                         (row.get("doc_id"),)).fetchone()
        meta, quality = json.loads(d[0]), json.loads(d[1])
        fields = row.get("metadata_fields") or []
        out["metadata"] = {f: meta.get(f) for f in fields}
        out["metadata_conflicts"] = [c for c in quality.get("provenance_conflicts", []) if c.get("field") in fields]
    return out


# ---------------------------------------------------------------- rendering (pure functions of the DB rows)


def _fm(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _row_type(row: dict):
    return row.get("question_type", row.get("type"))


def render_rejection(c: Row) -> str:
    row = json.loads(c["row_json"])
    reject = json.loads(c["reject_json"])
    ctx = json.loads(c["context_json"])
    inf = json.loads(c["inference_json"]) if c["inference_json"] else None
    front = {
        "candidate_id": c["candidate_id"], "batch_id": c["batch_id"],
        "batch_file": f"{'sealed' if is_sealed(c['dataset']) else 'datasets'}/candidates/{c['batch_id']}.jsonl",
        "batch_sha256": c["batch_sha256"],
        "dataset": c["dataset"], "row_sha256": c["row_sha256"], "type": _row_type(row),
        "doc_id": ",".join(str(d) for d in doc_ids_of(row)), "drafted_by": c["drafted_by"],
        "submitted_at": c["submitted_at"],
        "rejected_by": c["decided_by"], "rejected_at": c["decided_at"], "categories": reject["categories"],
        "inference_status": "inferred" if inf else "pending",
        "inferred_by": inf["inferred_by"] if inf else None, "inferred_at": inf["inferred_at"] if inf else None,
    }
    lines = ["---", *(f"{k}: {_fm(v)}" for k, v in front.items()), "---", "",
             f"# Rejected candidate {c['candidate_id']}", "", "## Question", "",
             *(f"> {line}" for line in str(row.get("question", "")).splitlines() or [""]), "",
             "## Reviewer decision", ""]
    lines += [f"- `{code}`: {REJECT_CATEGORIES.get(code, code)}" for code in reject["categories"]]
    lines += ["", "Reviewer note:", ""]
    lines += [f"> {line}" for line in (reject["note"] or "(none)").splitlines()] + ["", "## Inferred reason", ""]
    if inf:
        lines += [f"- Cause: {inf['cause']}", f"- Lesson: {inf['lesson']}",
                  f"- Drafting rule: {inf['drafting_rule']}", f"- Inferred by {inf['inferred_by']} at {inf['inferred_at']}"]
    else:
        lines += ["Pending. Record it with:", "",
                  f"`python -m rfp_assistant.cli gold infer --candidate-id {c['candidate_id']} --by <agent> "
                  "--file <inference.json>`"]
    lines += ["", "## Candidate row (verbatim)", "", "```json",
              json.dumps(row, ensure_ascii=False, indent=1, sort_keys=True), "```", "",
              "## Source context at rejection", ""]
    docs = ctx.get("documents") or ([ctx.get("document")] if "document" in ctx else [])
    if not docs or docs == [None]:
        lines.append("Document not found in the manifest.")
    for doc in docs:
        if doc is None:
            continue
        lines += [f"- Document: {doc['title']} (`{doc['filename']}`)",
                  f"- Source: {doc['format']}, parse `{doc['parse_status']}`, review `{doc['review_status']}`"]
        if doc.get("unavailable_reason"):
            lines.append(f"- Unavailable: {doc['unavailable_reason']}")
    for ev in ctx.get("evidence", []):
        lines += ["", f"### Evidence element `{ev['element_id']}` ({ev['kind']})", "",
                  f"- Location: `{json.dumps(ev['location'], ensure_ascii=False)}`", f"- Quote: {ev['quote']}", "",
                  "```text", ev["text"] if ev["text"] is not None else "(element missing)", "```"]
    if "metadata" in ctx:
        lines += ["", "### CSV metadata fields", "", "```json",
                  json.dumps({"fields": ctx["metadata"], "conflicts": ctx["metadata_conflicts"]},
                             ensure_ascii=False, indent=1, sort_keys=True), "```"]
    return "\n".join(lines) + "\n"


def render_index(rejected: list[Row]) -> str:
    pending = [c for c in rejected if not c["inference_json"]]
    lines = ["# Rejection wiki", "",
             "Every rejected dataset candidate, rendered from the database. Do not edit these files by hand: "
             "`gold infer` records a reason and `gold check` fails on any difference. The drafting procedure is "
             "`.wiki/gold-drafting.md` in the repository.", "",
             f"## Pending inference ({len(pending)})", ""]
    lines += [f"- [{c['candidate_id']}]({c['candidate_id']}.md): "
              f"{', '.join(json.loads(c['reject_json'])['categories'])}" for c in pending] or ["None."]
    lines += ["", "## Drafting rules learned", ""]
    rules = [(c, json.loads(c["inference_json"])) for c in rejected if c["inference_json"]]
    lines += [f"- {inf['drafting_rule']} (from [{c['candidate_id']}]({c['candidate_id']}.md), "
              f"{', '.join(json.loads(c['reject_json'])['categories'])})" for c, inf in rules] or ["None yet."]
    lines += ["", "## All rejections", "", "| Candidate | Batch | Type | Categories | Rejected at | Inference |",
              "| --- | --- | --- | --- | --- | --- |"]
    for c in rejected:
        row = json.loads(c["row_json"])
        lines.append(f"| [{c['candidate_id']}]({c['candidate_id']}.md) | {c['batch_id']} | {_row_type(row)} | "
                     f"{', '.join(json.loads(c['reject_json'])['categories'])} | {c['decided_at']} | "
                     f"{'inferred' if c['inference_json'] else 'pending'} |")
    return "\n".join(lines) + "\n"


def _reviews(conn: Connection, candidate_id: str) -> list[Row]:
    return conn.execute("SELECT * FROM gold_reviews WHERE candidate_id = ? ORDER BY created_at, review_id",
                        (candidate_id,)).fetchall()


def dataset_rows(conn: Connection, dataset: str) -> list[dict]:
    out = []
    rows = conn.execute("SELECT * FROM gold_candidates WHERE dataset = ? AND status = 'approved' "
                        "ORDER BY decided_at, candidate_id", (dataset,)).fetchall()
    if not is_gold(dataset):
        for c in rows:
            row = json.loads(c["row_json"])
            row.update(reviewed_by=c["decided_by"], reviewed_at=c["decided_at"], batch_id=c["batch_id"])
            out.append(row)
        return out
    latest: dict[str, tuple[int, Row]] = {}  # a correction replaces its earlier approved revision
    for c in rows:
        row = json.loads(c["row_json"])
        if row["question_id"] not in latest or row["revision"] > latest[row["question_id"]][0]:
            latest[row["question_id"]] = (row["revision"], c)
    for _, c in sorted(latest.values(), key=lambda x: (x[1]["decided_at"], x[1]["candidate_id"])):
        row = json.loads(c["row_json"])
        reviews = _reviews(conn, c["candidate_id"])
        first = next((r for r in reviews if r["kind"] == "decision"), None)
        second = [r for r in reviews if r["kind"] == "second"]
        # Only a corrected revision can clear a disagreement, including in older review histories.
        second = next((r for r in second if r["decision"] == "disagree"), second[-1] if second else None)
        review = dict(row.get("review") or {})
        review.update(reviewed_by=c["decided_by"], approved_at=c["decided_at"], status="approved",
                      original_inspected=bool(first and first["original_inspected"]),
                      disputed=bool(review.get("disputed")) or bool(first and first["decision"] == "approve:disputed"),
                      second_review={"reviewer": second["reviewer"], "agreed": second["decision"] == "agree",
                                     "note": second["note"], "at": second["created_at"]} if second else None)
        row.update(review=review, candidate_id=c["candidate_id"], batch_id=c["batch_id"])
        out.append(row)
    return out


def _rejected(conn: Connection, sealed: bool) -> list[Row]:
    rows = conn.execute("SELECT * FROM gold_candidates WHERE status = 'rejected' "
                        "ORDER BY decided_at, candidate_id").fetchall()
    return [c for c in rows if is_sealed(c["dataset"]) == sealed]


def _write_rejection(settings: Settings, conn: Connection, candidate_id: str) -> None:
    c = conn.execute("SELECT * FROM gold_candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
    folder = rejections_dir(settings, c["dataset"])
    write_text_atomic(folder / f"{candidate_id}.md", render_rejection(c))
    write_text_atomic(folder / "index.md", render_index(_rejected(conn, is_sealed(c["dataset"]))))


def _record_review(conn: Connection, candidate_id: str, reviewer: str, kind: str, decision: str,
                   original_inspected: bool, note: str, at: str) -> None:
    conn.execute("INSERT INTO gold_reviews(review_id, candidate_id, reviewer, kind, decision, original_inspected, "
                 "note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (str(uuid.uuid4()), candidate_id, reviewer, kind, decision, int(original_inspected), note, at))


# ---------------------------------------------------------------- operations


def submit(settings: Settings, source: Path, batch_id: str, dataset: str, drafted_by: str) -> dict:
    if not ID_RE.fullmatch(batch_id):
        raise GoldError("batch ids use lowercase letters, digits and hyphens")
    dataset_path(settings, dataset)  # validates the name
    if not drafted_by.strip():
        raise GoldError("--drafted-by is required")
    raw = source.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise GoldError("candidate file must be UTF-8 without BOM")
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    if not rows:
        raise GoldError("candidate file is empty")
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        uninferred = [r[0] for r in conn.execute(
            "SELECT candidate_id FROM gold_candidates WHERE status = 'rejected' AND inference_json IS NULL")]
        if uninferred:
            raise GoldError("infer why these candidates were rejected before drafting more: " + ", ".join(uninferred))
        if conn.execute("SELECT 1 FROM gold_candidates WHERE batch_id = ?", (batch_id,)).fetchone() \
                or batch_path(settings, batch_id).exists() or batch_path(settings, batch_id, "test").exists():
            raise GoldError(f"batch {batch_id} already exists")
        known = {r["candidate_id"]: dict(r) for r in conn.execute(
            "SELECT candidate_id, status, dataset, row_json, question_key FROM gold_candidates")}
        known_questions = {r["question_key"]: (cid, r["status"], json.loads(r["row_json"]).get("question_id"))
                           for cid, r in known.items()}
        revisions: dict[str, list[tuple[int, str]]] = {}  # question_id -> [(revision, status)] of gold rows
        for r in known.values():
            row = json.loads(r["row_json"])
            if row.get("question_id"):
                revisions.setdefault(row["question_id"], []).append((row.get("revision") or 0, r["status"]))
        checker = row_checker(settings, conn, dataset)
        errors: list[str] = []
        seen: set[str] = set()
        for i, row in enumerate(rows, start=1):
            cid = candidate_id_of(row, dataset)
            tag = f"row {i} ({cid})"
            if not ID_RE.fullmatch(cid):
                errors.append(f"{tag}: id must use lowercase letters, digits and hyphens")
            elif cid in known or cid in seen:
                errors.append(f"{tag}: id already used by another candidate")
            seen.add(cid)
            if drafter_of(row) != drafted_by:
                errors.append(f"{tag}: drafted_by must equal --drafted-by")
            if not is_gold(dataset) and (row.get("reviewed_by") or row.get("reviewed_at")):
                errors.append(f"{tag}: drafts cannot carry a review")
            prior = known_questions.get(question_key(row))
            if is_gold(dataset):
                qid, rev = row.get("question_id"), row.get("revision")
                earlier = revisions.get(qid, [])
                if earlier and (not isinstance(rev, int) or rev <= max(r for r, _ in earlier)):
                    errors.append(f"{tag}: a correction appends a higher revision of {qid}")
                elif earlier and any(s == "pending" for _, s in earlier):
                    errors.append(f"{tag}: an earlier revision of {qid} is still pending review")
                elif not earlier and isinstance(rev, int) and rev != 1:
                    errors.append(f"{tag}: a new question starts at revision 1")
                if prior and prior[2] != qid:
                    errors.append(f"{tag}: same question already drafted as {prior[0]} ({prior[1]})")
                revisions.setdefault(qid, []).append((rev if isinstance(rev, int) else 0, "pending"))
            elif prior:
                errors.append(f"{tag}: same question already drafted as {prior[0]} ({prior[1]})")
            known_questions[question_key(row)] = (cid, "this batch", row.get("question_id"))
            errors += check_row(checker, row, tag, dataset)
        if errors:
            raise GoldError("\n".join(errors))
        path = batch_path(settings, batch_id, dataset)
        write_jsonl_atomic(path, rows)
        batch_sha = _sha(path.read_bytes())
        now = utcnow()
        conn.executemany(
            "INSERT INTO gold_candidates(candidate_id, batch_id, batch_sha256, dataset, row_json, row_sha256, "
            "question_key, drafted_by, submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(candidate_id_of(r, dataset), batch_id, batch_sha, dataset, dumps(r), _sha(dumps(r)), question_key(r),
              drafted_by, now) for r in rows])
        # from now on the dataset file is a projection of approved candidates only
        write_jsonl_atomic(dataset_path(settings, dataset), dataset_rows(conn, dataset))
    return {"batch_id": batch_id, "rows": len(rows), "batch_file": str(path), "batch_sha256": batch_sha}


def _repin_evidence(conn, old: str, new: str, evidence: list[dict]) -> list[dict]:
    by_path = {json.loads(r["location_json"]).get("path"): r["element_id"] for r in conn.execute(
        "SELECT element_id, location_json FROM elements WHERE extraction_id = ?", (new,))}
    out = []
    for ev in evidence:
        was = conn.execute("SELECT location_json FROM elements WHERE extraction_id = ? AND element_id = ?",
                           (old, ev.get("element_id"))).fetchone()
        now = by_path.get(json.loads(was["location_json"]).get("path")) if was else None
        out.append({**ev, "element_id": now})
    return out


def repin(settings: Settings, batch_id: str) -> dict:
    """Moves pending rows whose evidence cites a superseded extraction onto the active one, by element path.

    A walker revision re-derives every element ID of a document while most elements keep their path. Each cited
    element is found again by path and its quote must still be in it; otherwise nothing moves and the row has to
    be redrafted. The moved rows go into a new immutable batch file, `batch_id`, and name where they came from.
    Pilot and gold rows are never mixed in one batch: each dataset's moved rows need their own batch."""
    if not ID_RE.fullmatch(batch_id):
        raise GoldError("batch ids use lowercase letters, digits and hyphens")
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        if conn.execute("SELECT 1 FROM gold_candidates WHERE batch_id = ?", (batch_id,)).fetchone() \
                or batch_path(settings, batch_id).exists():
            raise GoldError(f"batch {batch_id} already exists")
        moved, errors, datasets = [], [], set()
        for c in conn.execute("SELECT * FROM gold_candidates WHERE status = 'pending' ORDER BY submitted_at, "
                              "candidate_id").fetchall():
            row = json.loads(c["row_json"])
            checker = row_checker(settings, conn, c["dataset"])
            if is_gold(c["dataset"]):
                stale = {s["doc_id"]: (s.get("extraction_id"), checker.docs[s["doc_id"]]["active_extraction_id"])
                         for s in row.get("scope") or [] if s.get("doc_id") in checker.docs and s.get("extraction_id")
                         and s.get("extraction_id") != checker.docs[s["doc_id"]]["active_extraction_id"]}
                if not stale:
                    continue
                scope = [{**s, "extraction_id": stale[s["doc_id"]][1]} if s.get("doc_id") in stale else s
                         for s in row["scope"]]
                groups = []
                for g in row.get("evidence_groups") or []:
                    if g.get("doc_id") in stale:
                        old, new = stale[g["doc_id"]]
                        alts = [{**a, "extraction_id": new}
                                for a in _repin_evidence(conn, old, new, g.get("alternatives") or [])]
                        g = {**g, "alternatives": alts}
                    groups.append(g)
                new_row = {**row, "scope": scope, "evidence_groups": groups,
                           "repinned_from": {"batch_id": c["batch_id"],
                                             "extractions": {d: o for d, (o, _) in stale.items()}}}
            else:
                doc = checker.docs.get(row.get("doc_id"))
                old, new = row.get("extraction_id"), doc and doc["active_extraction_id"]
                if not row.get("evidence") or doc is None or old == new:
                    continue
                new_row = {**row, "extraction_id": new, "evidence": _repin_evidence(conn, old, new, row["evidence"]),
                           "repinned_from": {"batch_id": c["batch_id"], "extraction_id": old}}
            problems = check_row(checker, new_row, f"{c['candidate_id']}", c["dataset"])
            if problems:
                errors += problems
            else:
                moved.append((c["candidate_id"], new_row))
                datasets.add(c["dataset"])
        if errors:
            raise GoldError("\n".join(errors))
        if not moved:
            return {"batch_id": None, "rows": 0}
        if len(datasets) > 1:
            raise GoldError(f"pending rows of several datasets need repinning ({sorted(datasets)}); one batch each")
        (dataset,) = datasets
        path = batch_path(settings, batch_id, dataset)
        write_jsonl_atomic(path, [r for _, r in moved])
        batch_sha = _sha(path.read_bytes())
        conn.executemany("UPDATE gold_candidates SET batch_id = ?, batch_sha256 = ?, row_json = ?, row_sha256 = ? "
                         "WHERE candidate_id = ?",
                         [(batch_id, batch_sha, dumps(r), _sha(dumps(r)), cid) for cid, r in moved])
    return {"batch_id": batch_id, "rows": len(moved), "batch_file": str(path), "batch_sha256": batch_sha}


def _sealed_refusal(dataset: str, include_sealed: bool) -> None:
    if is_sealed(dataset) and not include_sealed:
        raise GoldError("봉인된 평가 질문은 검증 화면에서 다룰 수 없습니다. 소유자 CLI로 검토하세요.")


def decide(settings: Settings, candidate_id: str, decision: str, reviewer: str, expected_sha: str,
           categories: list[str] | None = None, note: str = "", *, original_inspected: bool = False,
           disputed: bool = False, include_sealed: bool = False) -> dict:
    """First decision wins. expected_sha is the row version the reviewer saw. A gold-2 approval also states that
    the reviewer inspected the original; `disputed` asks for an independent second review before the row counts."""
    if decision not in ("approve", "reject"):
        raise GoldError("decision must be approve or reject")
    categories = list(dict.fromkeys(categories or []))
    note = (note or "").strip()
    if decision == "reject":
        if not categories or any(c not in REJECT_CATEGORIES for c in categories):
            raise GoldError("거절 사유를 하나 이상 선택하세요.")
        if "other" in categories and not note:
            raise GoldError("'기타'를 고르면 메모가 필요합니다.")
    if not (reviewer or "").strip():
        raise GoldError("검토자 이름이 필요합니다.")
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        c = conn.execute("SELECT * FROM gold_candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
        if c is None:
            raise GoldError("알 수 없는 질문입니다.")
        _sealed_refusal(c["dataset"], include_sealed)
        if c["status"] != "pending":
            raise GoldError(f"이미 처리된 질문입니다 ({c['status']}, {c['decided_by']}).")
        if c["row_sha256"] != expected_sha:
            raise GoldError("질문 내용이 바뀌었습니다. 다시 불러오세요.")
        if c["drafted_by"] == reviewer and (decision == "approve" or not note):
            raise GoldError("초안 작성자는 자기 질문을 승인할 수 없습니다. 철회하려면 거절 사유를 적으세요.")
        row = json.loads(c["row_json"])
        prov = row.get("generation_provenance") or {}
        if (is_gold(c["dataset"]) and prov.get("method") == "llm" and reviewer == prov.get("model")
                and (decision == "approve" or not note)):
            raise GoldError("초안을 만든 모델은 자기 질문을 승인할 수 없습니다.")
        if is_gold(c["dataset"]) and decision == "approve" and not original_inspected:
            raise GoldError("원문을 직접 확인했다고 표시해야 승인할 수 있습니다.")
        now = utcnow()
        conn.execute(
            "UPDATE gold_candidates SET status = ?, decided_by = ?, decided_at = ?, reject_json = ?, context_json = ? "
            "WHERE candidate_id = ?",
            ("approved" if decision == "approve" else "rejected", reviewer, now,
             dumps({"categories": categories, "note": note}) if decision == "reject" else None,
             dumps(source_context(conn, row)), candidate_id))
        _record_review(conn, candidate_id, reviewer, "decision",
                       "approve:disputed" if decision == "approve" and disputed else decision,
                       original_inspected, note, now)
        if decision == "approve":
            write_jsonl_atomic(dataset_path(settings, c["dataset"]), dataset_rows(conn, c["dataset"]))
        else:
            _write_rejection(settings, conn, candidate_id)
    return {"candidate_id": candidate_id, "status": "approved" if decision == "approve" else "rejected",
            "decided_at": now}


def second_review(settings: Settings, candidate_id: str, reviewer: str, agreed: bool, note: str, *,
                  include_sealed: bool = False) -> dict:
    """An independent second check of an approved gold row (disputed deadlines, amounts, institutions, mandatory
    conditions, conflicts). Appended to the review log; a disagreement keeps the row out of valid gold until a
    corrected revision is approved."""
    note = (note or "").strip()
    if not note:
        raise GoldError("2차 검토에는 확인 내용을 적어야 합니다.")
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        c = conn.execute("SELECT * FROM gold_candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
        if c is None or not is_gold(c["dataset"]) or c["status"] != "approved":
            raise GoldError("승인된 평가 질문만 2차 검토할 수 있습니다.")
        _sealed_refusal(c["dataset"], include_sealed)
        prov = json.loads(c["row_json"]).get("generation_provenance") or {}
        if (reviewer in (c["drafted_by"], c["decided_by"]) or not reviewer.strip()
                or (prov.get("method") == "llm" and reviewer == prov.get("model"))):
            raise GoldError("2차 검토자는 초안 작성자·1차 검토자와 달라야 합니다.")
        _record_review(conn, candidate_id, reviewer, "second", "agree" if agreed else "disagree", True, note, utcnow())
        write_jsonl_atomic(dataset_path(settings, c["dataset"]), dataset_rows(conn, c["dataset"]))
    return {"candidate_id": candidate_id, "second_review": "agree" if agreed else "disagree"}


def infer(settings: Settings, candidate_id: str, by: str, inference: dict) -> dict:
    if not by.strip():
        raise GoldError("--by is required")
    missing = [k for k in INFERENCE_FIELDS if not str(inference.get(k, "")).strip()]
    if missing:
        raise GoldError(f"inference needs non-empty {missing}")
    record = {k: str(inference[k]).strip() for k in INFERENCE_FIELDS}
    record.update(inferred_by=by, inferred_at=utcnow())
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        c = conn.execute("SELECT status FROM gold_candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
        if c is None or c["status"] != "rejected":
            raise GoldError(f"{candidate_id} is not a rejected candidate")
        conn.execute("UPDATE gold_candidates SET inference_json = ? WHERE candidate_id = ?",
                     (dumps(record), candidate_id))
        _write_rejection(settings, conn, candidate_id)
    return {"candidate_id": candidate_id, **record}


def _expected_files(settings: Settings, conn: Connection) -> dict[Path, str]:
    files: dict[Path, str] = {}
    for sealed in (False, True):
        rejected = _rejected(conn, sealed)
        folder = _area(settings, sealed) / "rejections"
        files.update({folder / f"{c['candidate_id']}.md": render_rejection(c) for c in rejected})
        if rejected:
            files[folder / "index.md"] = render_index(rejected)
    for (dataset,) in conn.execute("SELECT DISTINCT dataset FROM gold_candidates"):
        rows = dataset_rows(conn, dataset)
        files[dataset_path(settings, dataset)] = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    return files


def _pages(settings: Settings) -> list[Path]:
    return [p for sealed in (False, True) if (_area(settings, sealed) / "rejections").exists()
            for p in (_area(settings, sealed) / "rejections").glob("*.md")]


def check(settings: Settings) -> list[str]:
    """Every projection equals its rendering from the database; every batch file is intact."""
    errors: list[str] = []
    with open_db(settings.db_path) as conn:
        expected = _expected_files(settings, conn)
        batches: dict[tuple[str, str], set[str]] = {}
        for c in conn.execute("SELECT * FROM gold_candidates"):
            if _sha(c["row_json"]) != c["row_sha256"]:
                errors.append(f"{c['candidate_id']}: stored row does not match its hash")
            batches.setdefault((c["batch_id"], c["dataset"]), set()).add(c["batch_sha256"])
    for (batch_id, dataset), shas in batches.items():
        path = batch_path(settings, batch_id, dataset)
        if not path.exists():
            errors.append(f"batch file missing: {path.name}")
        elif {_sha(path.read_bytes())} != shas:
            errors.append(f"batch file changed after submission: {path.name}")
    for path, text in expected.items():
        if not path.exists():
            errors.append(f"missing: {path.relative_to(settings.data_dir)}")
        elif path.read_text(encoding="utf-8") != text:
            errors.append(f"differs from the database: {path.relative_to(settings.data_dir)}")
    for page in _pages(settings):
        if page not in expected:
            errors.append(f"page without a rejected candidate: {page.relative_to(settings.data_dir)}")
    return errors


def sync(settings: Settings) -> int:
    """Rewrites every projection from the database; removes rejection pages that have no candidate."""
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        expected = _expected_files(settings, conn)
        for path, text in expected.items():
            write_text_atomic(path, text)
        for page in _pages(settings):
            if page not in expected:
                page.unlink()
    return len(expected)


def current_errors(checker, row: dict, dataset: str = "dev-pilot") -> list[str]:
    """What the shared row checks say about a candidate today: a source can be re-parsed or recovered after the
    candidate was drafted (e.g. a converter case whose document is now parsed)."""
    return check_row(checker, row, str(row.get("question_id") or row.get("id")), dataset)


def status(settings: Settings) -> dict:
    with open_db(settings.db_path) as conn:
        counts = {f"{r[0]}:{r[1]}": r[2] for r in conn.execute(
            "SELECT dataset, status, COUNT(*) FROM gold_candidates GROUP BY 1, 2")}
        pending_inference = [r[0] for r in conn.execute(
            "SELECT candidate_id FROM gold_candidates WHERE status = 'rejected' AND inference_json IS NULL "
            "ORDER BY decided_at")]
        checkers: dict = {}
        pending_invalid = []
        for r in conn.execute("SELECT candidate_id, dataset, row_json FROM gold_candidates WHERE status = 'pending' "
                              "ORDER BY submitted_at, candidate_id"):
            checker = checkers.setdefault(is_gold(r["dataset"]), row_checker(settings, conn, r["dataset"]))
            errors = current_errors(checker, json.loads(r["row_json"]), r["dataset"])
            if errors:
                pending_invalid.append({"candidate_id": r["candidate_id"], "errors": errors})
    return {"counts": counts, "rejection_wiki": str(rejections_dir(settings) / "index.md"),
            "pending_inference": pending_inference, "pending_invalid": pending_invalid}


# ---------------------------------------------------------------- review screen reads


def queue(settings: Settings, include_sealed: bool = False) -> list[dict]:
    with open_db(settings.db_path) as conn:
        rows = conn.execute("SELECT candidate_id, dataset, batch_id, row_json FROM gold_candidates "
                            "WHERE status = 'pending' ORDER BY submitted_at, candidate_id").fetchall()
    return [{"candidate_id": r["candidate_id"], "dataset": r["dataset"], "batch_id": r["batch_id"],
             "type": _row_type(json.loads(r["row_json"])), "question": json.loads(r["row_json"]).get("question")}
            for r in rows if include_sealed or not is_sealed(r["dataset"])]


def candidate(settings: Settings, candidate_id: str, include_sealed: bool = False) -> dict:
    with open_db(settings.db_path) as conn:
        c = conn.execute("SELECT * FROM gold_candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
        if c is None:
            raise GoldError("알 수 없는 질문입니다.")
        _sealed_refusal(c["dataset"], include_sealed)
        row = json.loads(c["row_json"])
        return {"candidate_id": candidate_id, "status": c["status"], "row": row, "row_sha256": c["row_sha256"],
                "drafted_by": c["drafted_by"], "batch_id": c["batch_id"], "dataset": c["dataset"],
                "gold": is_gold(c["dataset"]), "operational": _row_type(row) in OPERATIONAL_TYPES,
                "context": source_context(conn, row),
                "reviews": [dict(r) for r in _reviews(conn, candidate_id)],
                "current_errors": current_errors(row_checker(settings, conn, c["dataset"]), row, c["dataset"])}


def recent(settings: Settings, limit: int = 20, include_sealed: bool = False) -> list[dict]:
    with open_db(settings.db_path) as conn:
        rows = conn.execute("SELECT candidate_id, dataset, status, decided_by, decided_at, reject_json "
                            "FROM gold_candidates WHERE status != 'pending' ORDER BY decided_at DESC").fetchall()
    rows = [r for r in rows if include_sealed or not is_sealed(r["dataset"])][:limit]
    return [{"candidate_id": r["candidate_id"], "status": r["status"], "decided_by": r["decided_by"],
             "decided_at": r["decided_at"],
             "categories": json.loads(r["reject_json"])["categories"] if r["reject_json"] else []} for r in rows]


# ---------------------------------------------------------------- drafting inputs

EXCERPT_CATEGORIES = ("numeric_qualifier", "late_table", "repeated_code", "deadline")
QUALIFIER_RE = re.compile(r"(\d[\d,]{3,}\s*원|부가(가치)?세|VAT|\d+\s*(%|개월|일\s*이내|년\s*이내)|이상|이하|이내|초과|미만)")
DEADLINE_RE = re.compile(r"(20\d\d\s*[.\-년]\s*\d{1,2}\s*[.\-월]\s*\d{1,2}|마감|제출\s*기한|까지)")
LATE_FROM = 0.7


_BOUNDARY = re.compile(r"(?<=[.。!?])\s+|\n")


def _trigger(category: str, raw: str, code_docs: dict[str, set[str]]) -> tuple[int, int] | None:
    """Character span in raw_text of the fact that puts an element in a category."""
    if category == "numeric_qualifier":
        m = QUALIFIER_RE.search(raw)
    elif category == "deadline":
        m = DEADLINE_RE.search(raw)
    elif category == "repeated_code":
        m = next((m for m in CODE_RE.finditer(raw) if len(code_docs.get(m.group(1), ())) > 1), None)
    else:  # late_table: the header row is what makes the table readable
        first = raw.split("\n", 1)[0]
        return (0, len(first)) if first.strip() else None
    return (m.start(), m.end()) if m else None


def _window(raw: str, kind: str, trigger: tuple[int, int], max_chars: int) -> dict | None:
    """A bounded excerpt that always contains the trigger: for a table, its header row plus the row(s) holding the
    trigger; for prose, the sentence(s) holding it, widened by whole neighbouring sentences. `offsets` are raw_text
    spans, so every excerpt traces back to the active element. None when even the trigger does not fit."""
    start, end = trigger
    if end - start > max_chars:
        return None
    if kind == "table":
        lines, pos = [], 0
        for line in raw.split("\n"):
            lines.append((pos, pos + len(line)))
            pos += len(line) + 1
        hit = [i for i, (a, b) in enumerate(lines) if a < end and start < b] or [0]
        chosen = sorted({0, *hit})
        size = lambda ids: sum(lines[i][1] - lines[i][0] + 1 for i in ids)  # noqa: E731
        if size(chosen) <= max_chars:
            nxt = hit[-1] + 1
            while nxt < len(lines) and size(chosen + [nxt]) <= max_chars:  # the next rows often hold conditions
                chosen.append(nxt)
                nxt += 1
            spans = [lines[i] for i in chosen]
            return {"text": "\n".join(raw[a:b] for a, b in spans), "offsets": [list(x) for x in spans],
                    "row_lines": chosen, "context_clipped": False}
        # one row longer than the bound: fall through to a character window inside it
    bounds = sorted({0, len(raw), *(m.end() for m in _BOUNDARY.finditer(raw))})
    sentences = list(zip(bounds, bounds[1:]))
    first = next(i for i, (a, b) in enumerate(sentences) if b > start)
    last = next(i for i, (a, b) in enumerate(sentences) if b >= end)
    lo, hi = first, last
    if sentences[hi][1] - sentences[lo][0] <= max_chars:
        while True:
            grew = False
            for cand in (hi + 1, lo - 1):
                if 0 <= cand < len(sentences):
                    a, b = min(sentences[lo][0], sentences[cand][0]), max(sentences[hi][1], sentences[cand][1])
                    if b - a <= max_chars:
                        lo, hi, grew = min(lo, cand), max(hi, cand), True
            if not grew:
                break
        a, b = sentences[lo][0], sentences[hi][1]
        return {"text": raw[a:b].strip(), "offsets": [[a, b]], "context_clipped": False}
    pad = (max_chars - (end - start)) // 2
    a = max(0, start - pad)
    b = min(len(raw), a + max_chars)
    a = max(0, b - max_chars)
    return {"text": raw[a:b], "offsets": [[a, b]], "context_clipped": True}


def excerpts(settings: Settings, per_category: int = 2, max_chars: int = 800) -> dict:
    """Narrowly scoped drafting inputs: a few candidate elements per dev-family document and category, plus the
    queue context a drafter must respect (rejections with their reasons, existing question keys). Contains source
    text, so it belongs in the local inputs; the owner decides whether to share it."""
    fam_path = settings.data_dir / "datasets" / "families.json"
    if not fam_path.exists():
        raise GoldError("families.json is missing; run validate-gold or assign families first")
    families = json.loads(fam_path.read_text(encoding="utf-8"))["families"]
    dev = {d: k for k, f in families.items() if f["split"] == "dev" for d in f["doc_ids"]}
    with open_db(settings.db_path) as conn:
        docs = [dict(r) for r in conn.execute(
            "SELECT d.doc_id, d.active_source_hash, s.active_extraction_id FROM documents d "
            "JOIN sources s ON s.source_hash = d.active_source_hash WHERE s.parse_status = 'parsed' "
            "ORDER BY d.csv_row_id")]
        docs = [d for d in docs if d["doc_id"] in dev]
        seen_sources: set[str] = set()
        elements: dict[str, list[dict]] = {}
        for d in docs:
            if d["active_source_hash"] in seen_sources:
                continue  # byte-identical copies share one extraction: draft against one association
            seen_sources.add(d["active_source_hash"])
            elements[d["doc_id"]] = [dict(r) for r in conn.execute(
                "SELECT element_id, source_order, kind, raw_text, location_json FROM elements "
                "WHERE extraction_id = ? AND kind != 'toc' ORDER BY source_order", (d["active_extraction_id"],))]
        code_docs: dict[str, set[str]] = {}
        for doc_id, els in elements.items():
            for e in els:
                for code in CODE_RE.findall(e["raw_text"]):
                    code_docs.setdefault(code, set()).add(doc_id)
        rejected = [{"candidate_id": r["candidate_id"], "doc_id": json.loads(r["row_json"]).get("doc_id"),
                     "type": json.loads(r["row_json"]).get("type"),
                     "categories": json.loads(r["reject_json"])["categories"],
                     "note": json.loads(r["reject_json"]).get("note"),
                     "inference": json.loads(r["inference_json"]) if r["inference_json"] else None}
                    for r in conn.execute("SELECT * FROM gold_candidates WHERE status = 'rejected' "
                                          "AND dataset NOT IN ('test')")]  # sealed rows never reach drafting packs
        keys = sorted(r[0] for r in conn.execute("SELECT question_key FROM gold_candidates "
                                                 "WHERE dataset NOT IN ('test')"))
    out, omitted = [], []
    for d in docs:
        els = elements.get(d["doc_id"])
        if not els:
            continue
        n = len(els)
        picked: dict[str, int] = {}
        for e in els:
            raw = e["raw_text"]
            if len(raw.strip()) < 20:
                continue
            position = e["source_order"] / max(1, els[-1]["source_order"])
            for cat in EXCERPT_CATEGORIES:
                if picked.get(cat, 0) >= per_category:
                    continue
                if cat == "late_table" and not (e["kind"] == "table" and position >= LATE_FROM):
                    continue
                trigger = _trigger(cat, raw, code_docs)
                if trigger is None:
                    continue
                window = _window(raw, e["kind"], trigger, max_chars)
                if window is None:
                    omitted.append({"category": cat, "element_id": e["element_id"],
                                    "reason": "the triggering fact does not fit the excerpt bound"})
                    continue
                picked[cat] = picked.get(cat, 0) + 1
                loc = json.loads(e["location_json"])
                out.append({"category": cat, "doc_id": d["doc_id"], "source_hash": d["active_source_hash"],
                            "extraction_id": d["active_extraction_id"], "family": dev[d["doc_id"]],
                            "element_id": e["element_id"], "kind": e["kind"], "position": round(position, 3),
                            "elements_in_extraction": n,
                            "location": {k: loc.get(k) for k in ("page", "page_label", "section_path", "path")},
                            "trigger": raw[trigger[0]:trigger[1]], "trigger_offset": list(trigger), **window})
                break  # one category per element keeps the pack varied
    context = {"created_at": utcnow(), "dev_documents": len(docs), "excerpts": len(out),
               "by_category": {c: sum(x["category"] == c for x in out) for c in EXCERPT_CATEGORIES},
               "omitted": omitted,
               "queue": status(settings)["counts"], "rejections": rejected, "existing_question_keys": keys,
               "rules": "Read .wiki/gold-drafting.md. Quote exactly from `text`; keep reviewed_by null; never pick "
                        "a family or split yourself; a different person approves against the original."}
    return {"excerpts": out, "context": context}


def write_excerpts(settings: Settings, out_dir: Path, **kw) -> dict:
    if not out_dir.is_absolute():
        raise GoldError("--out must be an absolute directory")
    if out_dir.exists() and any(out_dir.iterdir()):
        raise GoldError("--out already has files; choose a new directory so earlier packs survive")
    pack = excerpts(settings, **kw)
    write_jsonl_atomic(out_dir / "excerpts.jsonl", pack["excerpts"])
    write_text_atomic(out_dir / "drafting-context.json", json.dumps(pack["context"], ensure_ascii=False, indent=1))
    return {"out": str(out_dir), **{k: pack["context"][k] for k in ("dev_documents", "excerpts", "by_category")}}
