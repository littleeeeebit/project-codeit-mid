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
from .ingestion import CODE_RE, QUARANTINE_TEXT, nfc
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
                    for r in conn.execute("SELECT * FROM gold_candidates WHERE status = 'rejected'")]
        keys = sorted(r[0] for r in conn.execute("SELECT question_key FROM gold_candidates"))
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
