"""Development-pilot dataset validation and source-family assignment."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .ingestion import nfc
from .settings import Settings
from .store import dumps, open_db, read_jsonl, write_text_atomic

QUESTION_TYPES = {
    "late_content", "table_fact", "numeric_qualifier", "repeated_code", "requirement_detail", "condition",
    "missing_metadata", "provenance_conflict", "converter_unavailable", "not_in_source",
}
REQUIRED_PILOT_TYPES = {"late_content", "table_fact", "repeated_code", "missing_metadata", "provenance_conflict",
                        "converter_unavailable"}
OPERATIONAL_TYPES = {"converter_unavailable"}  # recovery cases, kept apart from source-absence gold
METADATA_TYPES = {"missing_metadata", "provenance_conflict"}  # answered from CSV metadata provenance
PILOT_SIZE = 24
LATE_FRACTION = 0.3


def _ws(text: str) -> str:
    return re.sub(r"\s+", " ", nfc(text)).strip()


def dataset_path(settings: Settings, name: str) -> Path:
    if not re.fullmatch(r"[a-z0-9-]+", name):
        raise ValueError("dataset names use lowercase letters, digits and hyphens")
    return settings.data_dir / "datasets" / f"{name}.jsonl"


def assign_families(settings: Settings) -> dict:
    """One family per original hash (byte-identical copies share it), split deterministically by hash.
    Existing assignments are never moved."""
    path = settings.data_dir / "datasets" / "families.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"families": {}}
    with open_db(settings.db_path) as conn:
        docs = [dict(r) for r in conn.execute("SELECT doc_id, filename, active_source_hash FROM documents")]
    fams = existing["families"]
    for d in docs:
        h = d["active_source_hash"]
        fam = fams.setdefault(f"src-{h[:16]}", {
            "split": "dev" if int(hashlib.sha256(h.encode()).hexdigest(), 16) % 2 == 0 else "test",
            "source_hash": h, "doc_ids": []})
        if d["doc_id"] not in fam["doc_ids"]:
            fam["doc_ids"].append(d["doc_id"])
    write_text_atomic(path, json.dumps({"families": fams}, ensure_ascii=False, indent=1))
    return {"families": len(fams), "dev": sum(f["split"] == "dev" for f in fams.values())}


class RowChecker:
    """Structural and provenance checks for one dataset row; shared by submission and final validation."""

    def __init__(self, settings: Settings, conn) -> None:
        fam_path = settings.data_dir / "datasets" / "families.json"
        families = json.loads(fam_path.read_text(encoding="utf-8"))["families"] if fam_path.exists() else {}
        self.fam_of_doc = {d: (k, f) for k, f in families.items() for d in f["doc_ids"]}
        self.conn = conn
        self.docs = {r["doc_id"]: dict(r) for r in conn.execute(
            "SELECT d.doc_id, d.active_source_hash, s.active_extraction_id, s.parse_status "
            "FROM documents d JOIN sources s ON s.source_hash = d.active_source_hash")}

    def check(self, row: dict, tag: str) -> list[str]:
        errors: list[str] = []
        if row.get("type") not in QUESTION_TYPES:
            errors.append(f"{tag}: unknown type {row.get('type')!r}")
        if row.get("split") != "dev":
            errors.append(f"{tag}: pilot rows must be split=dev")
        if not str(row.get("question", "")).strip():
            errors.append(f"{tag}: empty question")
        doc = self.docs.get(row.get("doc_id"))
        if doc is None:
            return errors + [f"{tag}: unknown doc_id"]
        if row.get("source_hash") != doc["active_source_hash"]:
            errors.append(f"{tag}: source revision does not match the managed original")
        fam = self.fam_of_doc.get(row["doc_id"])
        if fam is None or fam[0] != row.get("family"):
            errors.append(f"{tag}: family missing or not the document's assigned family")
        elif fam[1]["split"] != "dev":
            errors.append(f"{tag}: family is assigned to test")
        if row.get("type") in OPERATIONAL_TYPES:
            if row.get("answerable") is not False or not row.get("operational_case"):
                errors.append(f"{tag}: converter cases are operational and unanswerable")
            if doc["parse_status"] != "quarantined":
                errors.append(f"{tag}: converter case refers to a document that is not quarantined")
            return errors
        if row.get("type") in METADATA_TYPES:
            if not row.get("metadata_fields"):
                errors.append(f"{tag}: metadata cases name the CSV fields they test")
            return errors
        if row.get("answerable") and not row.get("evidence"):
            errors.append(f"{tag}: answerable rows need original evidence")
        extraction = row.get("extraction_id")
        if row.get("evidence") and extraction != doc["active_extraction_id"]:
            errors.append(f"{tag}: evidence extraction revision is not the active one")
        total = self.conn.execute("SELECT COUNT(*) FROM elements WHERE extraction_id = ?",
                                  (extraction,)).fetchone()[0]
        for ev in row.get("evidence", []):
            el = self.conn.execute("SELECT raw_text, table_json, source_order FROM elements WHERE extraction_id = ? "
                                   "AND element_id = ?", (extraction, ev.get("element_id"))).fetchone()
            if el is None:
                errors.append(f"{tag}: evidence element not in extraction")
                continue
            haystacks = [el["raw_text"]] + [c["text"] for c in json.loads(el["table_json"] or '{"cells": []}')
                                            ["cells"]]
            if not any(_ws(ev.get("quote", "")) in _ws(h) for h in haystacks) or not ev.get("quote", "").strip():
                errors.append(f"{tag}: quote not found in the original extraction element")
            if row.get("type") == "late_content" and total and el["source_order"] < LATE_FRACTION * total:
                errors.append(f"{tag}: late_content evidence sits in the opening {int(LATE_FRACTION * 100)}%")
        return errors


def validate_gold(settings: Settings, name: str) -> dict:
    path = dataset_path(settings, name)
    if not path.exists():
        return {"ok": False, "errors": [f"dataset file missing: {path.name}"], "rows": 0}
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return {"ok": False, "errors": ["dataset must be UTF-8 without BOM"], "rows": 0}
    rows = read_jsonl(path)
    errors: list[str] = []
    unreviewed = 0
    with open_db(settings.db_path) as conn:
        checker = RowChecker(settings, conn)
        ids = set()
        for i, row in enumerate(rows, start=1):
            tag = f"row {i} ({row.get('id')})"
            if row.get("id") in ids:
                errors.append(f"{tag}: duplicate id")
            ids.add(row.get("id"))
            if not row.get("reviewed_by") or row.get("reviewed_by") == row.get("drafted_by"):
                unreviewed += 1
                errors.append(f"{tag}: needs an independent reviewer different from the drafter")
            errors += checker.check(row, tag)
    present = {r.get("type") for r in rows}
    missing_types = REQUIRED_PILOT_TYPES - present
    if missing_types:
        errors.append(f"pilot lacks required types: {sorted(missing_types)}")
    if len(rows) < PILOT_SIZE:
        errors.append(f"pilot has {len(rows)} rows; {PILOT_SIZE} required")
    report = {"ok": not errors, "rows": len(rows), "unreviewed_rows": unreviewed,
              "dataset_sha256": hashlib.sha256(raw).hexdigest(), "errors": errors,
              "types": {t: sum(r.get("type") == t for r in rows) for t in sorted(present - {None})}}
    write_text_atomic(path.with_suffix(".validation.json"), dumps(report))
    return report
