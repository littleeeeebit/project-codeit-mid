"""Drafted-question review: approved rows go to the dataset file, rejected rows to the rejection wiki.

The `gold_candidates` table is the only authority. The dataset file, every rejection page and the wiki index
are rendered from it inside the deciding transaction, so a file never disagrees with the database; `check`
proves that byte for byte and `sync` repairs a crash between the file write and the commit.

Loop for the drafting agent (.wiki/gold-drafting.md): read the rejection wiki, record an inferred reason for
every rejection with `infer`, then `submit` a new batch. `submit` refuses while any rejection lacks an
inferred reason, and refuses a question that was already drafted for the same document.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from .evaluation import METADATA_TYPES, OPERATIONAL_TYPES, RowChecker, dataset_path
from .ingestion import QUARANTINE_TEXT, nfc
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


def rejections_dir(settings: Settings) -> Path:
    return settings.data_dir / "datasets" / "rejections"


def batch_path(settings: Settings, batch_id: str) -> Path:
    return settings.data_dir / "datasets" / "candidates" / f"{batch_id}.jsonl"


def _sha(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def question_key(row: dict) -> str:
    """Same document and same question text, ignoring whitespace."""
    return f"{row.get('doc_id')}|" + re.sub(r"\s+", "", nfc(str(row.get("question", ""))))


# ---------------------------------------------------------------- source context


def source_context(conn: sqlite3.Connection, row: dict) -> dict:
    """What the reviewer compares the row against: document, source state, cited elements, CSV fields."""
    doc = conn.execute(
        "SELECT d.doc_id, d.filename, d.normalized_metadata_json, d.quality_json, s.format, s.parse_status, "
        "s.review_status, s.reason_code, s.active_extraction_id FROM documents d "
        "JOIN sources s ON s.source_hash = d.active_source_hash WHERE d.doc_id = ?", (row.get("doc_id"),)).fetchone()
    if doc is None:
        return {"document": None}
    meta = json.loads(doc["normalized_metadata_json"])
    quality = json.loads(doc["quality_json"])
    out = {
        "document": {"doc_id": doc["doc_id"], "filename": doc["filename"], "title": meta.get("title"),
                     "institution": meta.get("institution"), "format": doc["format"],
                     "parse_status": doc["parse_status"], "review_status": doc["review_status"],
                     "unavailable_reason": QUARANTINE_TEXT.get(doc["reason_code"] or "")},
        "evidence": [],
    }
    for ev in row.get("evidence") or []:
        el = conn.execute("SELECT element_id, kind, raw_text, location_json FROM elements WHERE extraction_id = ? "
                          "AND element_id = ?", (row.get("extraction_id"), ev.get("element_id"))).fetchone()
        out["evidence"].append({
            "element_id": ev.get("element_id"), "quote": ev.get("quote"),
            "kind": el["kind"] if el else None, "location": json.loads(el["location_json"]) if el else None,
            "text": el["raw_text"][:CONTEXT_CHARS] if el else None})
    if row.get("type") in METADATA_TYPES:
        fields = row.get("metadata_fields") or []
        out["metadata"] = {f: meta.get(f) for f in fields}
        out["metadata_conflicts"] = [c for c in quality.get("provenance_conflicts", []) if c.get("field") in fields]
    return out


# ---------------------------------------------------------------- rendering (pure functions of the DB rows)


def _fm(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_rejection(c: sqlite3.Row) -> str:
    row = json.loads(c["row_json"])
    reject = json.loads(c["reject_json"])
    ctx = json.loads(c["context_json"])
    inf = json.loads(c["inference_json"]) if c["inference_json"] else None
    front = {
        "candidate_id": c["candidate_id"], "batch_id": c["batch_id"],
        "batch_file": f"datasets/candidates/{c['batch_id']}.jsonl", "batch_sha256": c["batch_sha256"],
        "dataset": c["dataset"], "row_sha256": c["row_sha256"], "type": row.get("type"),
        "doc_id": row.get("doc_id"), "drafted_by": c["drafted_by"], "submitted_at": c["submitted_at"],
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
    doc = ctx.get("document")
    if doc is None:
        lines.append("Document not found in the manifest.")
    else:
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


def render_index(rejected: list[sqlite3.Row]) -> str:
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
        lines.append(f"| [{c['candidate_id']}]({c['candidate_id']}.md) | {c['batch_id']} | {row.get('type')} | "
                     f"{', '.join(json.loads(c['reject_json'])['categories'])} | {c['decided_at']} | "
                     f"{'inferred' if c['inference_json'] else 'pending'} |")
    return "\n".join(lines) + "\n"


def dataset_rows(conn: sqlite3.Connection, dataset: str) -> list[dict]:
    out = []
    for c in conn.execute("SELECT * FROM gold_candidates WHERE dataset = ? AND status = 'approved' "
                          "ORDER BY decided_at, candidate_id", (dataset,)):
        row = json.loads(c["row_json"])
        row.update(reviewed_by=c["decided_by"], reviewed_at=c["decided_at"], batch_id=c["batch_id"])
        out.append(row)
    return out


def _rejected(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM gold_candidates WHERE status = 'rejected' "
                        "ORDER BY decided_at, candidate_id").fetchall()


def _write_rejection(settings: Settings, conn: sqlite3.Connection, candidate_id: str) -> None:
    c = conn.execute("SELECT * FROM gold_candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
    write_text_atomic(rejections_dir(settings) / f"{candidate_id}.md", render_rejection(c))
    write_text_atomic(rejections_dir(settings) / "index.md", render_index(_rejected(conn)))


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
                or batch_path(settings, batch_id).exists():
            raise GoldError(f"batch {batch_id} already exists")
        known_ids = {r[0] for r in conn.execute("SELECT candidate_id FROM gold_candidates")}
        known_questions = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT question_key, candidate_id, status FROM gold_candidates")}
        checker = RowChecker(settings, conn)
        errors: list[str] = []
        seen: set[str] = set()
        for i, row in enumerate(rows, start=1):
            tag = f"row {i} ({row.get('id')})"
            cid = str(row.get("id", ""))
            if not ID_RE.fullmatch(cid):
                errors.append(f"{tag}: id must use lowercase letters, digits and hyphens")
            elif cid in known_ids or cid in seen:
                errors.append(f"{tag}: id already used by another candidate")
            seen.add(cid)
            if row.get("drafted_by") != drafted_by:
                errors.append(f"{tag}: drafted_by must equal --drafted-by")
            if row.get("reviewed_by") or row.get("reviewed_at"):
                errors.append(f"{tag}: drafts cannot carry a review")
            prior = known_questions.get(question_key(row))
            if prior:
                errors.append(f"{tag}: same question already drafted as {prior[0]} ({prior[1]})")
            known_questions[question_key(row)] = (cid, "this batch")
            errors += checker.check(row, tag)
        if errors:
            raise GoldError("\n".join(errors))
        path = batch_path(settings, batch_id)
        write_jsonl_atomic(path, rows)
        batch_sha = _sha(path.read_bytes())
        now = utcnow()
        conn.executemany(
            "INSERT INTO gold_candidates(candidate_id, batch_id, batch_sha256, dataset, row_json, row_sha256, "
            "question_key, drafted_by, submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(r["id"], batch_id, batch_sha, dataset, dumps(r), _sha(dumps(r)), question_key(r), drafted_by, now)
             for r in rows])
        # from now on the dataset file is a projection of approved candidates only
        write_jsonl_atomic(dataset_path(settings, dataset), dataset_rows(conn, dataset))
    return {"batch_id": batch_id, "rows": len(rows), "batch_file": str(path), "batch_sha256": batch_sha}


def repin(settings: Settings, batch_id: str) -> dict:
    """Moves pending rows whose evidence cites a superseded extraction onto the active one, by element path.

    A walker revision re-derives every element ID of a document while most elements keep their path. Each cited
    element is found again by path and its quote must still be in it; otherwise nothing moves and the row has to
    be redrafted. The moved rows go into a new immutable batch file, `batch_id`, and name where they came from."""
    if not ID_RE.fullmatch(batch_id):
        raise GoldError("batch ids use lowercase letters, digits and hyphens")
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        if conn.execute("SELECT 1 FROM gold_candidates WHERE batch_id = ?", (batch_id,)).fetchone() \
                or batch_path(settings, batch_id).exists():
            raise GoldError(f"batch {batch_id} already exists")
        checker = RowChecker(settings, conn)
        moved, errors = [], []
        for c in conn.execute("SELECT * FROM gold_candidates WHERE status = 'pending' ORDER BY submitted_at, "
                              "candidate_id").fetchall():
            row = json.loads(c["row_json"])
            doc = checker.docs.get(row.get("doc_id"))
            old, new = row.get("extraction_id"), doc and doc["active_extraction_id"]
            if not row.get("evidence") or doc is None or old == new:
                continue
            by_path = {json.loads(r["location_json"]).get("path"): r["element_id"] for r in conn.execute(
                "SELECT element_id, location_json FROM elements WHERE extraction_id = ?", (new,))}
            evidence = []
            for ev in row["evidence"]:
                was = conn.execute("SELECT location_json FROM elements WHERE extraction_id = ? AND element_id = ?",
                                   (old, ev.get("element_id"))).fetchone()
                now = by_path.get(json.loads(was["location_json"]).get("path")) if was else None
                evidence.append({**ev, "element_id": now})
            new_row = {**row, "extraction_id": new, "evidence": evidence,
                       "repinned_from": {"batch_id": c["batch_id"], "extraction_id": old}}
            problems = checker.check(new_row, f"{c['candidate_id']}")
            if problems:
                errors += problems
            else:
                moved.append(new_row)
        if errors:
            raise GoldError("\n".join(errors))
        if not moved:
            return {"batch_id": None, "rows": 0}
        path = batch_path(settings, batch_id)
        write_jsonl_atomic(path, moved)
        batch_sha = _sha(path.read_bytes())
        conn.executemany("UPDATE gold_candidates SET batch_id = ?, batch_sha256 = ?, row_json = ?, row_sha256 = ? "
                         "WHERE candidate_id = ?",
                         [(batch_id, batch_sha, dumps(r), _sha(dumps(r)), r["id"]) for r in moved])
    return {"batch_id": batch_id, "rows": len(moved), "batch_file": str(path), "batch_sha256": batch_sha}


def decide(settings: Settings, candidate_id: str, decision: str, reviewer: str, expected_sha: str,
           categories: list[str] | None = None, note: str = "") -> dict:
    """First decision wins. expected_sha is the row version the reviewer saw."""
    if decision not in ("approve", "reject"):
        raise GoldError("decision must be approve or reject")
    categories = list(dict.fromkeys(categories or []))
    note = (note or "").strip()
    if decision == "reject":
        if not categories or any(c not in REJECT_CATEGORIES for c in categories):
            raise GoldError("거절 사유를 하나 이상 선택하세요.")
        if "other" in categories and not note:
            raise GoldError("'기타'를 고르면 메모가 필요합니다.")
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        c = conn.execute("SELECT * FROM gold_candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
        if c is None:
            raise GoldError("알 수 없는 질문입니다.")
        if c["status"] != "pending":
            raise GoldError(f"이미 처리된 질문입니다 ({c['status']}, {c['decided_by']}).")
        if c["row_sha256"] != expected_sha:
            raise GoldError("질문 내용이 바뀌었습니다. 다시 불러오세요.")
        if c["drafted_by"] == reviewer:
            raise GoldError("초안 작성자는 자기 질문을 검토할 수 없습니다.")
        row = json.loads(c["row_json"])
        now = utcnow()
        conn.execute(
            "UPDATE gold_candidates SET status = ?, decided_by = ?, decided_at = ?, reject_json = ?, context_json = ? "
            "WHERE candidate_id = ?",
            ("approved" if decision == "approve" else "rejected", reviewer, now,
             dumps({"categories": categories, "note": note}) if decision == "reject" else None,
             dumps(source_context(conn, row)), candidate_id))
        if decision == "approve":
            write_jsonl_atomic(dataset_path(settings, c["dataset"]), dataset_rows(conn, c["dataset"]))
        else:
            _write_rejection(settings, conn, candidate_id)
    return {"candidate_id": candidate_id, "status": "approved" if decision == "approve" else "rejected",
            "decided_at": now}


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


def _expected_files(settings: Settings, conn: sqlite3.Connection) -> dict[Path, str]:
    rejected = _rejected(conn)
    files = {rejections_dir(settings) / f"{c['candidate_id']}.md": render_rejection(c) for c in rejected}
    if rejected:
        files[rejections_dir(settings) / "index.md"] = render_index(rejected)
    for (dataset,) in conn.execute("SELECT DISTINCT dataset FROM gold_candidates"):
        rows = dataset_rows(conn, dataset)
        files[dataset_path(settings, dataset)] = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    return files


def check(settings: Settings) -> list[str]:
    """Every projection equals its rendering from the database; every batch file is intact."""
    errors: list[str] = []
    with open_db(settings.db_path) as conn:
        expected = _expected_files(settings, conn)
        batches: dict[str, set[str]] = {}
        for c in conn.execute("SELECT * FROM gold_candidates"):
            if _sha(c["row_json"]) != c["row_sha256"]:
                errors.append(f"{c['candidate_id']}: stored row does not match its hash")
            batches.setdefault(c["batch_id"], set()).add(c["batch_sha256"])
    for batch_id, shas in batches.items():
        path = batch_path(settings, batch_id)
        if not path.exists():
            errors.append(f"batch file missing: {path.name}")
        elif {_sha(path.read_bytes())} != shas:
            errors.append(f"batch file changed after submission: {path.name}")
    for path, text in expected.items():
        if not path.exists():
            errors.append(f"missing: {path.relative_to(settings.data_dir)}")
        elif path.read_text(encoding="utf-8") != text:
            errors.append(f"differs from the database: {path.relative_to(settings.data_dir)}")
    if rejections_dir(settings).exists():
        for page in rejections_dir(settings).glob("*.md"):
            if page not in expected:
                errors.append(f"page without a rejected candidate: {page.relative_to(settings.data_dir)}")
    return errors


def sync(settings: Settings) -> int:
    """Rewrites every projection from the database; removes rejection pages that have no candidate."""
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        expected = _expected_files(settings, conn)
        for path, text in expected.items():
            write_text_atomic(path, text)
        if rejections_dir(settings).exists():
            for page in rejections_dir(settings).glob("*.md"):
                if page not in expected:
                    page.unlink()
    return len(expected)


def status(settings: Settings) -> dict:
    with open_db(settings.db_path) as conn:
        counts = {f"{r[0]}:{r[1]}": r[2] for r in conn.execute(
            "SELECT dataset, status, COUNT(*) FROM gold_candidates GROUP BY 1, 2")}
        pending_inference = [r[0] for r in conn.execute(
            "SELECT candidate_id FROM gold_candidates WHERE status = 'rejected' AND inference_json IS NULL "
            "ORDER BY decided_at")]
    return {"counts": counts, "rejection_wiki": str(rejections_dir(settings) / "index.md"),
            "pending_inference": pending_inference}


# ---------------------------------------------------------------- review screen reads


def queue(settings: Settings) -> list[dict]:
    with open_db(settings.db_path) as conn:
        rows = conn.execute("SELECT candidate_id, dataset, batch_id, row_json FROM gold_candidates "
                            "WHERE status = 'pending' ORDER BY submitted_at, candidate_id").fetchall()
    return [{"candidate_id": r["candidate_id"], "dataset": r["dataset"], "batch_id": r["batch_id"],
             "type": json.loads(r["row_json"]).get("type"), "question": json.loads(r["row_json"]).get("question")}
            for r in rows]


def candidate(settings: Settings, candidate_id: str) -> dict:
    with open_db(settings.db_path) as conn:
        c = conn.execute("SELECT * FROM gold_candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
        if c is None:
            raise GoldError("알 수 없는 질문입니다.")
        row = json.loads(c["row_json"])
        return {"candidate_id": candidate_id, "status": c["status"], "row": row, "row_sha256": c["row_sha256"],
                "drafted_by": c["drafted_by"], "batch_id": c["batch_id"], "dataset": c["dataset"],
                "operational": row.get("type") in OPERATIONAL_TYPES, "context": source_context(conn, row)}


def recent(settings: Settings, limit: int = 20) -> list[dict]:
    with open_db(settings.db_path) as conn:
        rows = conn.execute("SELECT candidate_id, status, decided_by, decided_at, reject_json FROM gold_candidates "
                            "WHERE status != 'pending' ORDER BY decided_at DESC LIMIT ?", (limit,)).fetchall()
    return [{"candidate_id": r["candidate_id"], "status": r["status"], "decided_by": r["decided_by"],
             "decided_at": r["decided_at"],
             "categories": json.loads(r["reject_json"])["categories"] if r["reject_json"] else []} for r in rows]
