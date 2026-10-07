"""Dataset validation (development pilot and the phase-4 gold schema), source-family assignment, frozen
retrieval runs and source-span scoring."""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import time
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..corpus.ingestion import CODE_RE, nfc
from ..retrieval.chunking import table_rows
from ..settings import Settings
from ..storage.store import dumps, open_db, read_jsonl, utcnow, write_text_atomic

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


SEALED_SPLITS = ("test",)  # questions and labels live under sealed/, never under datasets/


def dataset_path(settings: Settings, name: str) -> Path:
    if not re.fullmatch(r"[a-z0-9-]+", name):
        raise ValueError("dataset names use lowercase letters, digits and hyphens")
    if name in SEALED_SPLITS:
        return settings.data_dir / "sealed" / f"{name}.jsonl"
    return settings.data_dir / "datasets" / f"{name}.jsonl"


def assign_families(settings: Settings) -> dict:
    """Byte-identical copies share a family, and so do related revisions (same notice number): a question
    about one must not test on another. New documents join an existing related family; a family is otherwise
    keyed and split deterministically by original hash. Existing assignments are never moved; related
    documents that already sit in different splits are reported instead."""
    path = settings.data_dir / "datasets" / "families.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"families": {}}
    with open_db(settings.db_path) as conn:
        docs = [dict(r) for r in conn.execute(
            "SELECT doc_id, filename, active_source_hash, normalized_metadata_json FROM documents ORDER BY csv_row_id")]
    fams = existing["families"]
    fam_of_doc = {d: k for k, f in fams.items() for d in f["doc_ids"]}
    notice_of = {d["doc_id"]: json.loads(d["normalized_metadata_json"]).get("notice") for d in docs}
    for d in docs:
        if d["doc_id"] in fam_of_doc:
            continue
        h = d["active_source_hash"]
        key = f"src-{h[:16]}"
        if key not in fams:
            related = [fam_of_doc[o] for o, n in notice_of.items()
                       if n and n == notice_of[d["doc_id"]] and o in fam_of_doc]
            if related:
                key = related[0]
        fam = fams.setdefault(key, {
            "split": "dev" if int(hashlib.sha256(h.encode()).hexdigest(), 16) % 2 == 0 else "test",
            "source_hash": h, "doc_ids": []})
        fam["doc_ids"].append(d["doc_id"])
        fam_of_doc[d["doc_id"]] = key
        if h != fam["source_hash"]:
            fam.setdefault("related_sources", [])
            if h not in fam["related_sources"]:
                fam["related_sources"].append(h)
    split_related = []
    by_notice: dict[str, set[str]] = {}
    for doc_id, n in notice_of.items():
        if n and doc_id in fam_of_doc:
            by_notice.setdefault(n, set()).add(fam_of_doc[doc_id])
    for n, keys in sorted(by_notice.items()):
        if len({fams[k]["split"] for k in keys}) > 1:
            split_related.append({"notice": n, "families": sorted(keys)})
    write_text_atomic(path, json.dumps({"families": fams, "related_across_splits": split_related},
                                       ensure_ascii=False, indent=1))
    return {"families": len(fams), "dev": sum(f["split"] == "dev" for f in fams.values()),
            "related_across_splits": split_related}


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
    if name in GOLD_DATASETS:  # the phase-4 schema (dev, sealed test); dev-pilot keeps the pilot rules
        return validate_gold_v2(settings, name)
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
        # An operational (converter-failure) case is mandatory only while some source is actually quarantined:
        # a corpus whose failed conversions were all recovered cannot supply one honestly. Submitted converter rows
        # stay under RowChecker's strict quarantine check either way.
        quarantined = sorted(d for d, doc in checker.docs.items() if doc["parse_status"] == "quarantined")
        required_types = REQUIRED_PILOT_TYPES - OPERATIONAL_TYPES
        if quarantined:
            required_types |= OPERATIONAL_TYPES & REQUIRED_PILOT_TYPES
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
    missing_types = required_types - present
    if missing_types:
        errors.append(f"pilot lacks required types: {sorted(missing_types)}")
    if len(rows) < PILOT_SIZE:
        errors.append(f"pilot has {len(rows)} rows; {PILOT_SIZE} required")
    report = {"ok": not errors, "rows": len(rows), "unreviewed_rows": unreviewed,
              "dataset_sha256": hashlib.sha256(raw).hexdigest(), "errors": errors,
              "required_types": sorted(required_types),
              "operational_cases": {"applicable": bool(quarantined), "quarantined_documents": len(quarantined),
                                    "reason": "a source is currently quarantined" if quarantined else
                                    "no source is currently quarantined (failed conversions were recovered)"},
              "types": {t: sum(r.get("type") == t for r in rows) for t in sorted(present - {None})}}
    write_text_atomic(path.with_suffix(".validation.json"), dumps(report))
    return report


# ================================================================ phase 2: frozen retrieval comparison

# Scoring and promotion policy. Bump whenever grading, critical checks or the HR gate change: runs and gates
# recorded under another version are history, not evidence, and are neither compared against nor activated.
# 2: row-fragment grading, numeric/qualifier critical failures, trial frozen to H, recorded reranker concurrency.
# 3: runs bind the evaluated population (scored rows and their pinned evidence), not only the dataset bytes.
# 4: runs require an index matching the current analyzer/query policy and its frozen metadata snapshot.
EVAL_VERSION = "retrieval-eval-4"
# Gold-2 runs record their ranking policy, so a run scored under another deduplication rule is never reused.
# 1: repeats keyed by alternative and grade; 2: repeats keyed by the source coverage a chunk carries;
# 3: nDCG@5 counted in judged group units, the unit of its ideal; 4: grading targets the reviewer's offsets/cells.
# 5: row fragments target the approved column or offset occurrence within the row.
GOLD_RANKING_POLICY = "gold2-pinned-coordinates-5"
RANK_DEPTH = 20
CRITICAL_CODE_TYPES = {"repeated_code", "requirement_detail", "exact_identifier"}
CRITICAL_EVIDENCE_TYPES = {"numeric_qualifier"}  # an amount/date/VAT condition must reach the packed context
NDCG_AT = 5
GATE_NDCG_GAIN = 0.03
GATE_ADDED_P95_MS = 1000.0
LOAD_USERS = 6


class EvaluationError(RuntimeError):
    pass


def wilson(successes: int, n: int, z: float = 1.96) -> list[float] | None:
    """95% Wilson score interval for a binary rate."""
    if n == 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


BOOTSTRAP_SEED = 20261001
BOOTSTRAP_RESAMPLES = 1000


def family_bootstrap(results: list[dict], key: str, seed: int = BOOTSTRAP_SEED,
                     resamples: int = BOOTSTRAP_RESAMPLES) -> dict | None:
    """95% percentile interval of a mean ranked metric, resampling whole source families (a row's family set is
    its unit), so related questions move together. None when fewer than two families are scored."""
    by_family: dict[str, list[float]] = {}
    for r in results:
        value = (r.get("metrics") or {}).get(key)
        if value is not None:
            by_family.setdefault("+".join(r.get("families") or [str(r.get("id"))]), []).append(value)
    if len(by_family) < 2:
        return None
    units = sorted(by_family)
    rng = random.Random(seed)
    means = []
    for _ in range(resamples):
        picked = [v for u in (rng.choice(units) for _ in units) for v in by_family[u]]
        means.append(sum(picked) / len(picked))
    means.sort()
    return {"low": round(means[int(0.025 * resamples)], 4), "high": round(means[int(0.975 * resamples) - 1], 4),
            "families": len(units), "seed": seed, "resamples": resamples, "unit": "source family"}


def load_eval_rows(settings: Settings, name: str, sealed: bool = False,
                   extractions: dict[str, str] | None = None) -> tuple[list[dict], list[dict], str]:
    """(scored rows, skipped rows with reasons, dataset sha256). Only independently reviewed dev rows whose
    evidence is pinned to the document's active extraction are scored; the rest are listed, not dropped. The
    sealed test split is read only by the sealed run (`sealed=True`). `extractions` (source hash -> extraction)
    names what the evaluated index holds where it differs from the active one: after a re-parse the served index
    still holds the older extraction, and the questions pinned to it are still its questions."""
    if name in SEALED_SPLITS and not sealed:
        raise EvaluationError("the test split is sealed: it is evaluated once, by sealed-run under a release "
                              "freeze, never by evaluate-retrieval or plan-run")
    path = dataset_path(settings, name)
    if not path.exists():
        raise EvaluationError(f"dataset file missing: {path.name}")
    raw = path.read_bytes()
    rows = read_jsonl(path)
    with open_db(settings.db_path) as conn:
        active = {r["doc_id"]: (r["active_source_hash"], (extractions or {}).get(r["active_source_hash"],
                                                                                 r["active_extraction_id"]))
                  for r in conn.execute("SELECT d.doc_id, d.active_source_hash, s.active_extraction_id FROM documents "
                                        "d JOIN sources s ON s.source_hash = d.active_source_hash")}
    if name in GOLD_DATASETS:
        return _gold_eval_rows(rows, name, active) + (hashlib.sha256(raw).hexdigest(),)
    scored, skipped = [], []
    for row in rows:
        reason = None
        if not row.get("reviewed_by") or row.get("reviewed_by") == row.get("drafted_by"):
            reason = "not_independently_reviewed"
        elif row.get("split") != "dev":
            reason = "not_dev"
        elif row.get("doc_id") not in active:
            reason = "unknown_doc"
        elif row.get("evidence") and active[row["doc_id"]][1] != row.get("extraction_id"):
            reason = "evidence_revision_not_active"
        (skipped if reason else scored).append(row if not reason else {"id": row.get("id"), "reason": reason})
    return scored, skipped, hashlib.sha256(raw).hexdigest()


def _gold_eval_rows(rows: list[dict], name: str, active: dict) -> tuple[list[dict], list[dict]]:
    scored, skipped = [], []
    for row in rows:
        review = row.get("review") or {}
        prov = row.get("generation_provenance") or {}
        second = review.get("second_review") or {}
        reason = None
        if (review.get("status") != "approved" or not review.get("reviewed_by")
                or review.get("reviewed_by") == review.get("drafted_by") or review.get("original_inspected") is not True
                or (prov.get("method") == "llm" and review.get("reviewed_by") == prov.get("model"))):
            reason = "not_independently_reviewed"
        elif (review.get("disputed") or second) and (second.get("agreed") is not True
                or second.get("reviewer") in (None, "", review.get("drafted_by"), review.get("reviewed_by"))
                or (prov.get("method") == "llm" and second.get("reviewer") == prov.get("model"))):
            reason = "dispute_unresolved"
        elif row.get("split") != split_of(name):
            reason = f"not_{split_of(name)}"
        elif any(s.get("doc_id") not in active for s in row.get("scope") or []):
            reason = "unknown_doc"
        elif row.get("mode") != "metadata" and any(active[s["doc_id"]][1] != s.get("extraction_id")
                                                   for s in row.get("scope") or []):
            reason = "evidence_revision_not_active"
        (skipped if reason else scored).append(row if not reason else {"id": row.get("question_id"),
                                                                       "reason": reason})
    return scored, skipped


def is_gold_row(row: dict) -> bool:
    return row.get("dataset_version") == GOLD_SCHEMA


def row_id(row: dict):
    return row.get("question_id") if is_gold_row(row) else row.get("id")


def row_type(row: dict):
    return row.get("question_type") if is_gold_row(row) else row.get("type")


def row_critical(row: dict) -> bool:
    if is_gold_row(row):
        return any(c.get("criticality") == "critical" for c in row.get("required_claims") or [])
    return bool(row.get("critical"))


def row_scope(row: dict) -> list[tuple]:
    """[(DocRef, extraction_id)] the row's retrieval is restricted to (one per compared document)."""
    from ..contracts import DocRef

    if is_gold_row(row):
        return [(DocRef(s["doc_id"], s["source_hash"]), s.get("extraction_id")) for s in row.get("scope") or []]
    return [(DocRef(row["doc_id"], row["source_hash"]), row["extraction_id"])]


def row_groups(row: dict) -> list[dict]:
    """Required evidence groups; each group is one fact, satisfied by any of its alternative source spans. A pilot
    row's evidence units are groups of one alternative each. Gold-2 alternatives carry the reviewer's `offsets`
    and `cells`, which pick the approved occurrence when a quote repeats."""
    if is_gold_row(row):
        return [{"group_id": g["group_id"], "doc_id": g.get("doc_id"),
                 "alternatives": [{"element_id": a["element_id"], "quote": a["quote"],
                                   "extraction_id": a["extraction_id"],
                                   **{k: a[k] for k in ("offsets", "cells") if a.get(k) is not None}}
                                  for a in g.get("alternatives") or []]}
                for g in row.get("evidence_groups") or []]
    # pilot units keep the phase-2 quote matching (retrieval-eval-4): reviewer coordinates are a gold-2 policy
    return [{"group_id": u.get("element_id"), "doc_id": row.get("doc_id"),
             "alternatives": [{**{k: v for k, v in u.items() if k not in ("offsets", "cells")},
                               "extraction_id": row.get("extraction_id")}]} for u in row.get("evidence") or []]


def row_families(row: dict) -> list[str]:
    return sorted(row.get("family_ids") or []) if is_gold_row(row) else [row.get("family") or str(row.get("id"))]


def population_identity(rows: list[dict], skipped: list[dict]) -> str:
    """Identity of what a run actually scores: each eligible row's question, type, scope and pinned evidence, plus
    which rows were skipped and why. The dataset file can stay byte-identical while a source revision or review
    change makes a different set of questions eligible; runs over different populations are not comparable."""
    if rows and is_gold_row(rows[0]):
        scored = sorted(({"id": r.get("question_id"), "revision": r.get("revision"), "type": r.get("question_type"),
                          "question": r.get("question"), "mode": r.get("mode"), "scope": r.get("scope"),
                          "answerability": r.get("answerability"), "critical": row_critical(r),
                          "groups": [[g["group_id"], [[a["element_id"], a["quote"]] for a in g["alternatives"]]]
                                     for g in row_groups(r)]} for r in rows), key=lambda x: str(x["id"]))
        return hashlib.sha256(dumps({"schema": GOLD_SCHEMA, "scored": scored,
                                     "skipped": sorted(skipped, key=lambda x: str(x.get("id")))}).encode()).hexdigest()
    scored = sorted(({"id": r.get("id"), "type": r.get("type"), "question": r.get("question"),
                      "doc_id": r.get("doc_id"), "source_hash": r.get("source_hash"),
                      "extraction_id": r.get("extraction_id"), "answerable": r.get("answerable"),
                      "critical": bool(r.get("critical")),
                      "evidence": [[e.get("element_id"), e.get("quote")] for e in r.get("evidence") or []]}
                     for r in rows), key=lambda x: str(x["id"]))
    return hashlib.sha256(dumps({"scored": scored, "skipped": sorted(skipped, key=lambda x: str(x.get("id")))}
                                ).encode()).hexdigest()


def current_population(settings: Settings, dataset: str) -> tuple[str, int]:
    rows, skipped, _ = load_eval_rows(settings, dataset)
    return population_identity(rows, skipped), len(rows)


def is_passage_row(row: dict) -> bool:
    """Scored on ranked passages: answerable (or conflicting) evidence-backed rows; metadata, operational and
    unanswerable rows are reported in their own strata."""
    if is_gold_row(row):
        return (row.get("mode") != "metadata" and bool(row.get("evidence_groups"))
                and row.get("answerability") in ("answerable", "conflicting"))
    return bool(row.get("answerable")) and bool(row.get("evidence")) and row.get("type") not in (
        METADATA_TYPES | OPERATIONAL_TYPES)


def scope_indexed(index, row: dict) -> bool:
    return any(index.rows_by_extraction.get(x) for _, x in row_scope(row))


def _quote_range(text: str, quote: str) -> tuple[int, int] | None:
    words = nfc(quote).split()
    if not words:
        return None
    m = re.search(r"\s*".join(re.escape(w) for w in words), nfc(text))
    return (m.start(), m.end()) if m else None


def _rendered_rows(cells: list[dict]) -> tuple[str, list[tuple[int, int, int]]]:
    """The table text the gold validator accepts (one line per nonempty row, cells joined by ` | `) and each row's
    (row, start, end) in it."""
    by_row: dict[int, list[dict]] = {}
    for c in cells:
        by_row.setdefault(c["row"], []).append(c)
    spans, text = [], ""
    for r in sorted(by_row):
        line = " | ".join(t for t in (c["text"].strip() for c in sorted(by_row[r], key=lambda c: c["col"])) if t)
        if line:
            text += ("\n" if text else "")
            spans.append((r, len(text), len(text) + len(line)))
            text += line
    return text, spans


def _quote_rows(element: dict, quote: str) -> set[int]:
    """Table rows holding the quote: a single cell, or across cells/rows of the rendered table text (the
    `raw_text` the gold validator accepts, one line per nonempty row)."""
    cells = (element.get("table") or {}).get("cells", [])
    rows: set[int] = set()
    for c in cells:
        if _quote_range(c["text"], quote):
            rows.update(range(c["row"], c["row"] + max(1, c.get("rowspan", 1))))
    if rows:
        return rows
    text, spans = _rendered_rows(cells)
    rng = _quote_range(text, quote)
    if rng:
        rows = {r for r, a, b in spans if a < rng[1] and rng[0] < b}
    return rows


def _offsets(unit: dict) -> tuple[int, int] | None:
    o = unit.get("offsets")
    return (o[0], o[1]) if isinstance(o, list) and len(o) == 2 and all(isinstance(x, int) for x in o) else None


def text_target(unit: dict, element: dict) -> tuple[int, int] | None:
    """The approved occurrence of the quote in the element text: searched only inside the reviewer's `offsets`
    when the alternative has them (the range may include context), otherwise the first occurrence (older,
    coordinate-free alternatives)."""
    o = _offsets(unit)
    if o is None:
        return _quote_range(element["raw_text"], unit["quote"])
    rng = _quote_range(element["raw_text"][o[0]:o[1]], unit["quote"])
    return (o[0] + rng[0], o[0] + rng[1]) if rng else None


def row_target(unit: dict, element: dict) -> set[int]:
    """The approved table rows: the rows of the reviewer's `cells` (with their row spans) when given, else the
    rows the reviewer's `offsets` cover in the rendered table text, else every row holding the quote."""
    cells = (element.get("table") or {}).get("cells", [])
    wanted = unit.get("cells")
    if wanted:
        by_pos = {(c["row"], c["col"]): c for c in cells}
        rows: set[int] = set()
        for rc in wanted:
            c = by_pos.get(tuple(rc)) if isinstance(rc, list) and len(rc) == 2 else None
            if c:
                rows.update(range(c["row"], c["row"] + max(1, c.get("rowspan", 1))))
        return rows
    o = _offsets(unit)
    if o is not None:
        text, spans = _rendered_rows(cells)
        if text == element.get("raw_text"):
            rng = _quote_range(text[o[0]:o[1]], unit["quote"])
            if rng:
                a, b = o[0] + rng[0], o[0] + rng[1]
                return {r for r, x, y in spans if x < b and a < y}
    return _quote_rows(element, unit["quote"])


def _line_segments(element: dict, row: int) -> dict[tuple[int, int], tuple[int, int]]:
    """Where each cell (by its own row and column) sits in `table_rows`' line for `row`, the line row fragments'
    offsets refer to: cells spanning into the row are carried, and an adjacent repeat shares the kept segment."""
    cells = [c for c in element["table"]["cells"] if c["row"] <= row < c["row"] + max(1, c.get("rowspan", 1))]
    out: dict[tuple[int, int], tuple[int, int]] = {}
    pos, last, last_seg = 0, None, None
    for c in sorted(cells, key=lambda c: c["col"]):
        t = c["text"].strip()
        if not t:
            continue
        if last is not None and t == last:
            out[(c["row"], c["col"])] = last_seg
            continue
        if last is not None:
            pos += 3  # " | "
        last, last_seg = t, (pos, pos + len(t))
        out[(c["row"], c["col"])] = last_seg
        pos += len(t)
    return out


def _raw_cells(element: dict) -> list[tuple[tuple[int, int], int, int]]:
    """Each nonempty cell's (row, col) and its character range in the element `raw_text` (`render_table`)."""
    text, spans = _rendered_rows(element["table"]["cells"])
    out = []
    for r, a, _ in spans:
        pos = a
        for c in sorted((c for c in element["table"]["cells"] if c["row"] == r), key=lambda c: c["col"]):
            t = c["text"].strip()
            if t:
                out.append(((c["row"], c["col"]), pos, pos + len(t)))
                pos += len(t) + 3
    return out


def fragment_target(unit: dict, element: dict, row: int) -> tuple[int, int] | None:
    """The approved quote inside one row's `table_rows` line (what a row fragment's offsets refer to). With
    `cells`, the quote inside the named cells' segment of this row; with `offsets`, the approved characters of the
    raw table text translated into this row's segments; without coordinates, the first occurrence in the line.
    None when this row does not hold the approved quote."""
    if row not in row_target(unit, element):
        return None
    line = dict(table_rows(element)).get(row, "")
    segments = _line_segments(element, row)
    named = [segments[tuple(rc)] for rc in unit.get("cells") or []
             if isinstance(rc, list) and len(rc) == 2 and tuple(rc) in segments]
    if unit.get("cells"):
        if not named:
            return None
        lo, hi = min(a for a, _ in named), max(b for _, b in named)
        rng = _quote_range(line[lo:hi], unit["quote"])
        return (lo + rng[0], lo + rng[1]) if rng else None
    o = _offsets(unit)
    text, _ = _rendered_rows(element["table"]["cells"])
    if o is not None and text == element.get("raw_text"):
        rng = _quote_range(text[o[0]:o[1]], unit["quote"])
        if not rng:
            return None
        qa, qb = o[0] + rng[0], o[0] + rng[1]
        raw = _raw_cells(element)
        first = next((x for x in raw if x[1] <= qa < x[2]), None)  # cells holding the first and last character
        last = next((x for x in raw if x[1] < qb <= x[2]), None)
        s1, s2 = (segments.get(first[0]) if first else None), (segments.get(last[0]) if last else None)
        if s1 is None or s2 is None:
            return None
        return (s1[0] + qa - first[1], s2[0] + qb - last[1])
    return _quote_range(line, unit["quote"])


def grade(chunk: dict, unit: dict, element: dict | None) -> int:
    """2: the chunk carries the whole approved evidence; 1: it carries part of the same element around it;
    0: unrelated. Judged on source spans against the reviewer's coordinates (`text_target`, `row_target`,
    `fragment_target`), so any chunking is scored against the same approved occurrence."""
    if element is None:
        return 0
    best = 0
    for span in chunk["spans"]:
        if span["element_id"] != unit["element_id"]:
            continue
        if "rows" in span and span.get("fragment"):
            # One piece of an oversized row: only the characters this piece carries count.
            frag = span["fragment"]
            rng = fragment_target(unit, element, frag["row"]) if element.get("table") else None
            if rng:
                if frag["start"] <= rng[0] and rng[1] <= frag["end"]:
                    return 2
                if frag["start"] < rng[1] and rng[0] < frag["end"]:
                    best = max(best, 1)
                continue
            rows = row_target(unit, element)  # e.g. the header row repeated with every piece
            if rows and rows <= set(span["rows"]) - {frag["row"]}:
                return 2
            best = max(best, 1)
        elif "rows" in span:
            rows = row_target(unit, element)
            if rows and rows <= set(span["rows"]):
                return 2
            best = max(best, 1)
        else:
            rng = text_target(unit, element)
            if rng and span["start"] <= rng[0] and rng[1] <= span["end"]:
                return 2
            if rng is None or (span["start"] < rng[1] and rng[0] < span["end"]):
                best = max(best, 1)
    return best


def ndcg_from_grades(grades: list[int], ideal: list[int], k: int = NDCG_AT) -> float | None:
    """`DCG = sum((2**grade - 1) / log2(rank + 1))` over the first k deduplicated units, divided by the DCG of the
    ideal (judged-pool) grades in descending order. None when the ideal pool has no relevant unit. The textbook
    form, for grades already in the ideal's units; the gold-2 scorer uses `ndcg_from_units` over group units."""
    gain = lambda gs: sum((2 ** g - 1) / math.log2(r + 1) for r, g in enumerate(gs[:k], start=1))  # noqa: E731
    best = gain(sorted(ideal, reverse=True))
    return round(gain(grades) / best, 4) if best else None


def graded_units(ranking: list[dict], groups: list[dict], elements: dict, k: int = NDCG_AT) -> list[dict]:
    """The first k judged units of a (deduplicated) gold-2 ranking. The judged unit is the required evidence group,
    the same unit the ideal counts, so a passage is mapped to the groups it newly supports: a passage completing two
    groups occupies two consecutive unit positions (as two passages would), a passage adding nothing occupies one
    zero position (an irrelevant or unlabelled passage keeps costing its rank), and a group raised from partial to
    complete gains only the difference `2**2 - 2**1`, so no group earns more than its ideal gain. Each unit is
    `{"group", "grade", "gain", "passage"}`; `passage` is the 1-based rank of the returned passage."""
    credited = [0] * len(groups)
    out: list[dict] = []
    for rank, chunk in enumerate(ranking, start=1):
        if len(out) >= k:
            break
        grades = [group_grade(chunk, g, elements) for g in groups]
        new = [{"group": groups[j]["group_id"], "grade": g, "gain": 2 ** g - 2 ** credited[j], "passage": rank}
               for j, g in enumerate(grades) if g > credited[j]]
        for j, g in enumerate(grades):
            credited[j] = max(credited[j], g)
        new.sort(key=lambda u: -u["gain"])  # deterministic, and the order a perfect ranking would take
        out.extend(new or [{"group": None, "grade": 0, "gain": 0, "passage": rank, "unlabelled": not any(grades)}])
    return out[:k]


def ndcg_from_units(units: list[dict], groups: int, k: int = NDCG_AT) -> float | None:
    """nDCG@k over judged group units: `sum(gain / log2(position + 1))` divided by the ideal of one complete unit
    (gain `2**2 - 1`) per required group at the first positions. None without a required group."""
    best = sum(3 / math.log2(r + 1) for r in range(1, min(groups, k) + 1))
    dcg = sum(u["gain"] / math.log2(r + 1) for r, u in enumerate(units[:k], start=1))
    return round(dcg / best, 4) if best else None


def group_grade(chunk: dict, group: dict, elements: dict) -> int:
    """A group's grade is its best alternative's: two alternatives of one fact never add up to two facts."""
    return max((grade(chunk, a, elements.get((a["extraction_id"], a["element_id"]))) for a in group["alternatives"]),
               default=0)


def coverage(chunk: dict, unit: dict, element: dict | None) -> frozenset | None:
    """The part of the approved evidence a chunk carries (the same targets `grade` uses), as comparable atoms:
    character offsets of the quote in the element text, quoted table rows, or character offsets of the quote inside one row fragment. None when it
    cannot be located (the chunk then never counts as a repeat)."""
    if element is None:
        return None
    atoms: set = set()
    for span in chunk["spans"]:
        if span["element_id"] != unit["element_id"]:
            continue
        if "rows" in span and span.get("fragment"):
            frag = span["fragment"]
            rng = fragment_target(unit, element, frag["row"]) if element.get("table") else None
            if rng:
                atoms.update(("f", frag["row"], i) for i in range(max(rng[0], frag["start"]), min(rng[1], frag["end"])))
                continue
            rows = row_target(unit, element)
            if not rows:
                return None
            atoms.update(("r", r) for r in rows & (set(span["rows"]) - {frag["row"]}))
        elif "rows" in span:
            rows = row_target(unit, element)
            if not rows:
                return None
            atoms.update(("r", r) for r in rows & set(span["rows"]))
        else:
            rng = text_target(unit, element)
            if rng is None:
                return None
            atoms.update(("c", i) for i in range(max(rng[0], span["start"]), min(rng[1], span["end"])))
    return frozenset(atoms)


def dedup_ranking(ranking: list[dict], groups: list[dict], elements: dict) -> tuple[list[dict], int]:
    """Canonical ranking for gold-2 ranked metrics: a chunk that only repeats source coverage already credited
    higher up is removed before rank positions and cutoffs apply. A supporting alternative is a repeat when that
    alternative's complete span was already credited, or when the characters/rows of it this chunk carries were
    all carried by earlier chunks; two different pieces of one quote are distinct support and both stay. A chunk
    supporting nothing stays as a rank-consuming zero; a chunk adding group support stays; another alternative of
    a credited fact is a different passage and stays (grading 0). Returns the ranking and the number removed."""
    full: set[tuple[int, int]] = set()
    seen: dict[tuple[int, int], set] = {}
    credited = [0] * len(groups)
    out, removed = [], 0
    for chunk in ranking:
        hits, cover = {}, {}
        for j, g in enumerate(groups):
            for i, a in enumerate(g["alternatives"]):
                el = elements.get((a["extraction_id"], a["element_id"]))
                v = grade(chunk, a, el)
                if v:
                    hits[(j, i)] = v
                    cover[(j, i)] = coverage(chunk, a, el) if v == 1 else None
        best = [max((v for (j, _), v in hits.items() if j == n), default=0) for n in range(len(groups))]
        repeat = bool(hits) and not any(b > c for b, c in zip(best, credited)) and all(
            k in full or (v == 1 and cover[k] is not None and cover[k] <= seen.get(k, set()))
            for k, v in hits.items())
        if repeat:
            removed += 1
            continue
        for k, v in hits.items():
            if v == 2:
                full.add(k)
            elif cover[k] is not None:
                seen.setdefault(k, set()).update(cover[k])
        credited = [max(b, c) for b, c in zip(best, credited)]
        out.append(chunk)
    return out, removed


def score_row(row: dict, ranking: list[dict], packed: list[dict], elements: dict,
              groups: list[dict] | None = None) -> dict:
    """Ranked metrics for one passage row. Each required evidence group is credited once, at the first rank where
    it reaches its grade: overlapping chunks repeating one fact earn nothing more. Ideal DCG places one complete
    group per rank (capped at 1 when a single chunk carries several groups). Gold-2 rows are ranked after
    `dedup_ranking`, so repeats do not consume positions; pilot rows keep the phase-2 raw ranking so frozen
    `retrieval-eval-4` runs stay comparable. Packed-context figures always use what was actually packed."""
    units = groups if groups is not None else row_groups(row)
    removed = None
    if is_gold_row(row):
        ranking, removed = dedup_ranking(ranking, units, elements)
    credited = [0] * len(units)
    dcg, first_full = 0.0, None
    for rank, chunk in enumerate(ranking[:RANK_DEPTH], start=1):
        gain = 0
        for j, u in enumerate(units):
            g = group_grade(chunk, u, elements)
            if g > credited[j]:
                if rank <= NDCG_AT:
                    gain += g - credited[j]
                credited[j] = g
            if g == 2 and first_full is None:
                first_full = rank
        dcg += gain / math.log2(rank + 1)
    at = lambda k: [max((group_grade(c, u, elements) for c in ranking[:k]), default=0)  # noqa: E731
                    for u in units]
    top5, top20 = at(5), at(RANK_DEPTH)
    packed_grades = [max((group_grade(c, u, elements) for c in packed), default=0) for u in units]
    n = len(units)
    return {
        "units": n, "hit@1": int(any(g == 2 for g in at(1))), "hit@5": int(any(g == 2 for g in top5)),
        "hit@10": int(any(g == 2 for g in at(10))),
        "hit@20": int(any(g == 2 for g in top20)),
        "recall@20": round(sum(g == 2 for g in top20) / n, 4), "complete@20": int(all(g == 2 for g in top20)),
        "ndcg@5": round(min(1.0, dcg / sum(2 / math.log2(j + 1) for j in range(1, min(n, NDCG_AT) + 1))), 4), "mrr": round(1 / first_full, 4) if first_full else 0.0,
        "packed_recall": round(sum(g == 2 for g in packed_grades) / n, 4),
        "packed_complete": int(all(g == 2 for g in packed_grades)),
        "qualifier_loss": sum(g == 1 for g in packed_grades), "packed_missing": sum(g == 0 for g in packed_grades),
        "unit_grades@20": top20,
        "missing_units": [u["group_id"] for u, g in zip(units, top20) if g < 2],
        "packed_grades": packed_grades,
    } | ({"duplicates_removed": removed} if removed is not None else {}) | (_gold_ndcg(ranking, units, elements) if is_gold_row(row) else {})


def _gold_ndcg(ranking: list[dict], groups: list[dict], elements: dict) -> dict:
    """Gold-2 rows use the phase-4 graded formula over judged group units (`graded_units`), whose ideal is one
    complete unit per required group: numerator, ideal and cutoff count the same units, however the evidence is
    chunked. The judged pool is the predeclared source-span labels; a returned passage outside them grades 0 and
    is counted in `unlabelled@5` (a blind review of those passages is not implemented). Pilot rows keep the
    phase-2 increment formula so frozen `retrieval-eval-4` runs stay comparable."""
    units = graded_units(ranking, groups, elements)
    return {"ndcg@5": ndcg_from_units(units, len(groups)),
            "ndcg_formula": "sum((2^g - 2^g_before)/log2(p+1)) over group units, ideal one complete unit per group",
            "graded@5": [u["grade"] for u in units],
            "unlabelled@5": len({u["passage"] for u in units if u.get("unlabelled")}),
            "unlabelled_chunks@5": sorted({str(ranking[u["passage"] - 1].get("chunk_id") or f"rank {u['passage']}")
                                           for u in units if u.get("unlabelled")})}


def combine_sides(parts: list[dict]) -> dict:
    """Metrics of a two-document row from each document's own ranking: groups count once overall; recall and
    complete coverage are defined, while single-ranking measures (hit, nDCG, MRR) are not applicable."""
    units = sum(p["units"] for p in parts)
    top20 = [g for p in parts for g in p["unit_grades@20"]]
    packed = [g for p in parts for g in p["packed_grades"]]
    return {"units": units, "hit@1": None, "hit@5": None, "hit@20": None,
            "recall@20": round(sum(g == 2 for g in top20) / units, 4), "complete@20": int(all(g == 2 for g in top20)),
            "ndcg@5": None, "mrr": None, "packed_recall": round(sum(g == 2 for g in packed) / units, 4),
            "packed_complete": int(all(g == 2 for g in packed)), "qualifier_loss": sum(g == 1 for g in packed),
            "packed_missing": sum(g == 0 for g in packed), "unit_grades@20": top20,
            "missing_units": [m for p in parts for m in p["missing_units"]], "packed_grades": packed,
            "per_document": True}


def code_check(row: dict, packed: list[dict]) -> dict | None:
    """Critical check: an explicit requirement code must bring its own block first."""
    codes = list(dict.fromkeys(CODE_RE.findall(nfc(row.get("question", "")))))
    if not codes or row_type(row) not in CRITICAL_CODE_TYPES:
        return None
    first = packed[0] if packed else None
    ok = bool(first and first.get("requirement_key") in codes)
    return {"codes": codes, "ok": ok, "first_key": first.get("requirement_key") if first else None}


def critical_failures(results: list[dict], rows: dict | None = None) -> list[str]:
    """Question IDs failing a critical check: an explicit code without its own block first, a candidate outside
    the scope, or a numeric/qualifier (or explicitly `critical`) row whose required evidence is not completely in
    the packed context, whether partly cut (grade 1) or wholly missing (grade 0)."""
    out = []
    for r in results:
        row = (rows or {}).get(r.get("id"), r)
        critical_row = (row_type(row) in CRITICAL_EVIDENCE_TYPES or bool(row.get("critical"))
                        or (is_gold_row(row) and row_critical(row)))
        if ((r.get("code_check") and not r["code_check"]["ok"]) or r.get("wrong_scope")
                or (critical_row and r.get("metrics") and not r["metrics"]["packed_complete"])):
            out.append(r["id"])
    return sorted(set(out), key=str)


def aggregate(results: list[dict], skipped: list[dict]) -> dict:
    passage = [r for r in results if r.get("metrics")]
    single = [r for r in passage if r["metrics"]["units"] == 1]
    multi = [r for r in passage if r["metrics"]["units"] > 1]

    def rate(rows: list[dict], key: str) -> dict:
        rows = [r for r in rows if r["metrics"].get(key) is not None]
        k = sum(r["metrics"][key] for r in rows)
        return {"numerator": k, "denominator": len(rows), "rate": round(k / len(rows), 4) if rows else None,
                "wilson95": wilson(k, len(rows))}

    def mean(rows: list[dict], key: str) -> float | None:
        rows = [r for r in rows if r["metrics"].get(key) is not None]  # not applicable is not zero
        return round(sum(r["metrics"][key] for r in rows) / len(rows), 4) if rows else None

    by_type: dict[str, dict] = {}
    for t in sorted({r["type"] for r in passage}):
        rows = [r for r in passage if r["type"] == t]
        by_type[t] = {"n": len(rows), "hit@20": rate(rows, "hit@20")["rate"], "ndcg@5": mean(rows, "ndcg@5"),
                      "packed_complete": rate(rows, "packed_complete")["rate"]}
    latencies = [r["timings_ms"]["total"] for r in results if r.get("timings_ms")]
    codes = [r["code_check"] for r in results if r.get("code_check")]
    from ..retrieval.dense import percentile

    gold = [r for r in passage if "unlabelled@5" in r["metrics"]]
    pool = {"ndcg_pool": {"judged_units": "required evidence groups (predeclared source spans)",
                          "unlabelled_top5_passages": sum(r["metrics"]["unlabelled@5"] for r in gold),
                          "blind_review_of_unlabelled": "not implemented"}} if gold else {}
    return {
        "passage_rows": len(passage), "single_evidence": {"hit@20": rate(single, "hit@20"),
                                                          "hit@5": rate(single, "hit@5")},
        "multi_evidence": {"complete@20": rate(multi, "complete@20"), "recall@20": mean(multi, "recall@20")},
        "ndcg@5": mean(passage, "ndcg@5"), "mrr": mean(passage, "mrr"),
        "packed_complete": rate(passage, "packed_complete"),
        "qualifier_losses": sum(r["metrics"]["qualifier_loss"] for r in passage),
        "wrong_scope_candidates": sum(r.get("wrong_scope", 0) for r in results),
        "code_checks": {"ok": sum(c["ok"] for c in codes), "n": len(codes)},
        "critical_failures": critical_failures(results),
        "missing_in_packed": sum(r["metrics"]["packed_missing"] for r in passage),
        "latency_ms": {"p50": percentile(latencies, 0.5), "p95": percentile(latencies, 0.95), "n": len(latencies)},
        "fallbacks": sorted({r["fallback"] for r in results if r.get("fallback")}),
        "by_type": by_type,
        "ndcg_eligible_rows": sum(1 for r in passage if r["metrics"].get("ndcg@5") is not None),
        "bootstrap95": {k: family_bootstrap(passage, k) for k in ("ndcg@5", "mrr", "recall@20")},
        "not_scored": {"non_passage_rows": sum(1 for r in results if not r.get("metrics")),
                       "types": sorted({r["type"] for r in results if not r.get("metrics")}),
                       "skipped": skipped},
    } | pool


def profile_stats(index) -> dict:
    """Chunk count, size, duplicated (overlapping) characters and memory of one loaded keyword index."""
    covered: dict[tuple[str, str], list[tuple[int, int]]] = {}
    span_chars = 0
    for c in index.chunks:
        for s in c["spans"]:
            if "start" in s:
                span_chars += s["end"] - s["start"]
                covered.setdefault((c["extraction_id"], s["element_id"]), []).append((s["start"], s["end"]))
    unique_chars = 0
    for spans in covered.values():
        end = -1
        for a, b in sorted(spans):
            if b > end:
                unique_chars += b - max(a, end)
                end = b
    tokens = [c["token_count"] for c in index.chunks]
    return {"profile": index.profile, "chunks": len(tokens), "mean_tokens": round(sum(tokens) / len(tokens), 1)
            if tokens else 0, "max_tokens": max(tokens, default=0), "duplicated_span_chars": span_chars - unique_chars,
            "payload_bytes": sum(len(c["payload"].encode("utf-8")) for c in index.chunks)}


def _run_dir(settings: Settings, run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9-]+", run_id):
        raise EvaluationError("invalid run id")
    return settings.data_dir / "runs" / run_id


def dense_policy(model: str) -> str:
    from ..retrieval.dense import embed_policy

    return embed_policy(model)


def embedding_identity(settings: Settings) -> dict:
    """What a run records of its embedding model. The OpenAI identity stays {model, dims} so runs measured before
    other models existed keep their IDs; a local or Gemini model adds its pinned revision and prefix policy."""
    from ..retrieval.models import EMBEDDINGS

    spec = EMBEDDINGS[settings.embedding_model]
    out = {"model": settings.embedding_model, "dims": settings.embedding_dimensions}
    if spec.backend != "openai":
        out.update(revision=spec.revision, policy=spec.policy, backend=spec.backend)
    return out


def _ready_dense_for(settings: Settings, index_version: str) -> str | None:
    with open_db(settings.db_path) as conn:
        for r in conn.execute("SELECT index_version, config_json FROM indexes WHERE state = 'ready' "
                              "ORDER BY created_at DESC"):
            cfg = json.loads(r["config_json"])
            if (cfg.get("kind") == "dense" and cfg.get("base_index_version") == index_version
                    and cfg.get("model") == settings.embedding_model
                    and cfg.get("dimensions") == settings.embedding_dimensions
                    and cfg.get("policy") == dense_policy(settings.embedding_model)):
                return r["index_version"]
    return None


def _frozen_config(settings: Settings, label: str, dataset: str, dataset_sha: str, index, dense, analyzer,
                   extra: dict | None = None, population: tuple[str, int] | None = None) -> dict:
    from ..retrieval.retrieval import RUN_MODES, WhitespaceAnalyzer, analyzer_fingerprint, corpus_route_record

    return {"eval_version": EVAL_VERSION, "label": label, "mode": RUN_MODES[label], "dataset": dataset,
            "dataset_sha256": dataset_sha, "population_sha256": population[0] if population else None,
            "population_size": population[1] if population else None, "index_version": index.version, "index_manifest_hash": index.manifest_hash,
            "profile": index.profile, "analyzer": WhitespaceAnalyzer.version if label == "K0"
            else analyzer_fingerprint(analyzer),
            "dense_version": dense.version if dense is not None and label in ("D", "H", "HR") else None,
            "embedding": embedding_identity(settings) if label in ("D", "H", "HR") else None,
            "limits": {"channel_top_k": settings.channel_top_k, "fused_top_k": settings.fused_top_k,
                       "rrf_k": settings.rrf_k, "fusion": settings.fusion, "dense_weight": settings.dense_weight,
                       "keyword_head": settings.keyword_head,
                       "corpus_route": corpus_route_record(),
                       "dense_search": settings.dense_search, "hnsw_ef_search": settings.hnsw_ef_search,
                       "evidence_target_tokens": settings.evidence_target_tokens,
                       "evidence_max_tokens": settings.evidence_max_tokens,
                       "evidence_max_units": settings.evidence_max_units}, **(extra or {})}


def _execute(settings: Settings, index, analyzer, rows: list[dict], mode: str, dense=None, vectors=None,
             reranker=None, rerank_depth=None, unscoped: bool = False, rerank_protect: int = 0) -> list[dict]:
    """`unscoped` asks every question once over the whole corpus (the all-documents scope) instead of its row's
    selected documents; every evidence group is then scored on that single ranking."""
    from ..retrieval.retrieval import corpus_scope, retrieve

    corpus = corpus_scope(settings, index) if unscoped else None
    results = []
    for row in rows:
        out = {"id": row_id(row), "type": row_type(row), "critical": row_critical(row),
               "question": row.get("question"), "families": row_families(row)}
        if not is_passage_row(row):
            results.append(out)  # metadata, operational and unanswerable rows: reported, not ranked
            continue
        scope, groups = corpus or row_scope(row), row_groups(row)
        allowed = {x for _, x in scope}
        sides = []  # one scoped retrieval per selected document, as the balanced comparison serves it
        for ref, x in ([(None, None)] if len(scope) == 1 or corpus else scope):
            r = retrieve(settings, index, analyzer, row["question"], scope if ref is None else [(ref, x)], mode=mode,
                         dense=dense, query_vector=(vectors or {}).get(row_id(row)), reranker=reranker,
                         rerank_depth=rerank_depth, rerank_protect=rerank_protect)
            ranking = [index.chunks[index.row_of[c]] for c in r.ranking]
            packed = [index.chunks[index.row_of[e.chunk_id]] for e in r.evidence]
            mine = groups if ref is None else [g for g in groups if g["doc_id"] == ref.doc_id]
            sides.append((r, ranking, packed, score_row(row, ranking, packed, index.elements, mine)))
        r, ranking, packed, metrics = sides[0]
        if len(sides) > 1:
            metrics = combine_sides([m for *_, m in sides])
        timings: dict = {}
        for side in sides:
            for k, v in side[0].timings_ms.items():
                timings[k] = round(timings.get(k, 0) + v, 1) if isinstance(v, (int, float)) else v
        out.update(
            metrics=metrics, code_check=code_check(row, packed) if len(sides) == 1 else None,
            wrong_scope=sum(c["extraction_id"] not in allowed for _, rk, _, _ in sides for c in rk),
            ranking=[c for s in sides for c in s[0].ranking[:RANK_DEPTH]],
            packed=[e.chunk_id for s in sides for e in s[0].evidence],
            fallback=next((s[0].fallback for s in sides if s[0].fallback), None),
            candidates=[c for s in sides for c in s[0].candidates],
            limitations=[x for s in sides for x in s[0].limitations], timings_ms=timings,
            evidence_tokens=sum(s[0].evidence_tokens for s in sides))
        results.append(out)
    return results


def _write_run(settings: Settings, run_id: str, config: dict, results: list[dict], scores: dict) -> None:
    d = _run_dir(settings, run_id)
    write_text_atomic(d / "config.json", json.dumps(config, ensure_ascii=False, indent=1))
    from ..storage.store import write_jsonl_atomic

    write_jsonl_atomic(d / "traces.jsonl", results)
    write_text_atomic(d / "scores.json", json.dumps(scores, ensure_ascii=False, indent=1))
    write_text_atomic(d / "report.md", run_report_md(run_id, config, scores))


def run_report_md(run_id: str, config: dict, scores: dict) -> str:
    agg = scores.get("aggregate") or {}
    lines = [f"# Retrieval run {run_id}", "", f"- label/mode: {config['label']} / {config['mode']}",
             f"- dataset: {config['dataset']} (`{config['dataset_sha256'][:16]}`)",
             f"- keyword index: `{config['index_version']}` ({config['profile']})",
             f"- dense matrix: `{config.get('dense_version')}`", f"- status: {scores['status']}", ""]
    if scores["status"] != "complete":
        lines += [f"Reason: {scores.get('reason')}", ""]
        return "\n".join(lines)
    s, m = agg["single_evidence"], agg["multi_evidence"]
    lines += ["| Metric | Value | n | Wilson 95% |", "| --- | --- | --- | --- |",
              f"| single hit@20 | {s['hit@20']['rate']} | {s['hit@20']['denominator']} | {s['hit@20']['wilson95']} |",
              f"| single hit@5 | {s['hit@5']['rate']} | {s['hit@5']['denominator']} | {s['hit@5']['wilson95']} |",
              f"| multi complete@20 | {m['complete@20']['rate']} | {m['complete@20']['denominator']} | "
              f"{m['complete@20']['wilson95']} |",
              f"| packed complete | {agg['packed_complete']['rate']} | {agg['packed_complete']['denominator']} | "
              f"{agg['packed_complete']['wilson95']} |",
              f"| nDCG@5 (mean) | {agg['ndcg@5']} | {agg['passage_rows']} | |",
              f"| MRR (mean) | {agg['mrr']} | {agg['passage_rows']} | |", "",
              f"- qualifier losses in packed context: {agg['qualifier_losses']} partly cut, "
              f"{agg.get('missing_in_packed', 0)} wholly missing",
              f"- code checks: {agg['code_checks']['ok']}/{agg['code_checks']['n']}; critical failures: "
              f"{agg['critical_failures'] or 'none'}",
              f"- wrong-scope candidates: {agg['wrong_scope_candidates']}",
              f"- retrieval latency ms p50/p95: {agg['latency_ms']['p50']}/{agg['latency_ms']['p95']} "
              f"(n={agg['latency_ms']['n']}, single process, warm)",
              f"- fallbacks: {agg['fallbacks'] or 'none'}",
              f"- query-embedding cost (micro-USD): {(scores.get('query_embedding') or {}).get('settled_micro_usd', 0)}",
              f"- not scored: {agg['not_scored']['non_passage_rows']} non-passage rows, "
              f"{len(agg['not_scored']['skipped'])} skipped", ""]
    if scores.get("profile_stats"):
        lines += [f"- chunk profile: {json.dumps(scores['profile_stats'], ensure_ascii=False)}", ""]
    if scores.get("gate"):
        lines += ["## Reranker gate", "", "```json", json.dumps(scores["gate"], ensure_ascii=False, indent=1),
                  "```", ""]
    return "\n".join(lines)


def evaluate_retrieval(settings: Settings, analyzer, transport, dataset: str, labels: list[str],
                       index_version: str | None = None, allow_paid_queries: bool = False,
                       force: bool = False, member_id: str = "owner-cli") -> list[dict]:
    """Retrieval-only runs on identical source/scope/query versions; no answer generation. Query vectors are
    computed once, cached, and shared by D and H. A run whose configuration is already frozen with scores is
    reused unless `force`."""
    from ..retrieval.retrieval import RUN_MODES, KeywordIndex, RetrievalError
    from ..storage.store import get_app_setting

    bad = [x for x in labels if x not in RUN_MODES or x == "HR"]
    if bad:
        raise EvaluationError(f"unknown or unsupported run labels {bad}; HR runs through trial-reranker")
    with open_db(settings.db_path) as conn:
        index_version = index_version or get_app_setting(conn, "active_index")
    try:
        index = KeywordIndex.load(settings, index_version)
    except RetrievalError as exc:
        raise EvaluationError(str(exc)) from None
    rows, skipped, dataset_sha = load_eval_rows(settings, dataset, extractions=index.source_extraction)
    if not rows:
        raise EvaluationError("no independently reviewed dev rows to evaluate")
    from ..retrieval.retrieval import index_compatibility

    if index_compatibility(index, analyzer):
        raise EvaluationError(index_compatibility(index, analyzer))
    dense, dense_error = _verified_dense(settings, index) if any(x in ("D", "H") for x in labels) else (None, None)
    vectors, query_info, vector_error = _eval_query_vectors(settings, transport, rows, index, dense, dataset,
                                                            dataset_sha, member_id, allow_paid_queries)
    dense_error = vector_error or dense_error
    stats = profile_stats(index)
    summaries = []
    for label in labels:
        config = _frozen_config(settings, label, dataset, dataset_sha, index, dense, analyzer,
                                population=(population_identity(rows, skipped), len(rows)),
                                extra={"ranking_policy": GOLD_RANKING_POLICY}
                                if any(is_gold_row(r) for r in rows) else None)
        run_id = f"{label}-{hashlib.sha256(dumps(config).encode()).hexdigest()[:10]}"
        d = _run_dir(settings, run_id)
        if (d / "scores.json").exists() and not force:
            scores = json.loads((d / "scores.json").read_text(encoding="utf-8"))
            if scores.get("status") == "complete":
                summaries.append({"run_id": run_id, "label": label, "reused": True, **_headline(scores)})
                continue
        if label in ("D", "H") and dense_error:
            scores = {"status": "blocked", "reason": dense_error, "query_embedding": query_info}
            _write_run(settings, run_id, config, [], scores)
            summaries.append({"run_id": run_id, "label": label, "status": "blocked", "reason": dense_error})
            continue
        results = _execute(settings, index, analyzer, rows, RUN_MODES[label], dense=dense, vectors=vectors)
        scores = {"status": "complete", "aggregate": aggregate(results, skipped), "profile_stats": stats,
                  "query_embedding": query_info if label in ("D", "H") else None, "created_at": utcnow(),
                  "provenance": {"code": code_fingerprint(), "hardware": hardware(),
                                 "metric_code_sha256": metric_code_sha256()}}
        _write_run(settings, run_id, config, results, scores)
        summaries.append({"run_id": run_id, "label": label, "reused": False, **_headline(scores)})
    return summaries


def _verified_dense(settings: Settings, index) -> tuple[object | None, str | None]:
    """The ready dense matrix over this keyword index, or why there is none."""
    from ..retrieval import dense as dense_mod

    version = _ready_dense_for(settings, index.version)
    if version is None:
        return None, "no ready dense matrix for this keyword index; run plan-embeddings and build-dense"
    try:
        return dense_mod.DenseIndex.load(settings, version, base=index), None
    except dense_mod.DenseError as exc:
        return None, f"dense matrix failed verification: {exc}"


def _eval_query_vectors(settings: Settings, transport, rows: list[dict], index, dense, dataset: str,
                        dataset_sha: str, member_id: str, allow_paid_queries: bool) -> tuple[dict, dict, str | None]:
    """Query vectors for the scored passage rows (cache first, paid only when allowed and no earlier billing is
    unknown), what they cost, and why D/H cannot run when some are missing."""
    from ..retrieval import dense as dense_mod

    dense_error = None
    vectors: dict[str, object] = {}
    query_info: dict = {"hits": 0, "paid": 0, "unavailable": 0, "settled_micro_usd": 0, "attempts": []}
    open_attempts = dense_mod.unresolved_attempts(settings, "gold_eval") if allow_paid_queries else []
    if open_attempts:
        allow_paid_queries = False  # never resend while an earlier query embedding's billing is unknown
        query_info["blocked_by_unresolved_attempts"] = open_attempts
    if dense is not None:
        request_id = None
        if allow_paid_queries:
            request_id = dense_mod.ensure_job_request(
                settings, member_id, f"evaluate:{dataset_sha[:16]}:{index.version}:{dense.version}",
                {"job": "evaluate-retrieval", "dataset": dataset, "index": index.version})
        for row in rows:
            if not is_passage_row(row) or not scope_indexed(index, row):
                continue  # an empty scope never pays for a query vector
            vec, info = dense_mod.query_vector(settings, transport, row["question"], request_id=request_id,
                                               member_id=member_id, purpose="gold_eval",
                                               allow_paid=allow_paid_queries)
            if vec is None:
                query_info["unavailable"] += 1
                query_info.setdefault("reasons", []).append(info.get("reason"))
                if info.get("status") == "unknown":
                    break  # no automatic resend; stop spending
                continue
            vectors[row_id(row)] = vec
            query_info["hits" if info["cache"] == "hit" else "paid"] += 1
            if info.get("attempt_id"):
                query_info["attempts"].append(info["attempt_id"])
                query_info["settled_micro_usd"] += info.get("settled_micro_usd") or 0
            if info.get("billing") == "unknown":
                query_info.setdefault("reasons", []).append("unknown_billing_reconcile_first")
                break  # the vector is kept; nothing more is paid until the attempt is reconciled
        wanted = [row_id(r) for r in rows if is_passage_row(r) and scope_indexed(index, r)]
        query_info["unavailable"] = sum(i not in vectors for i in wanted)
        if query_info["unavailable"]:
            dense_error = (f"{query_info['unavailable']} query vectors unavailable "
                           f"({sorted(set(r for r in query_info.get('reasons', []) if r))}); "
                           "pass --allow-paid-queries with paid mode enabled, or keep K1")
    return vectors, query_info, dense_error


def code_fingerprint() -> dict:
    """What code produced a result: the Git revision when Git can tell, whether tracked files differ from it, and a
    hash of the package source either way. A missing or unreadable repository is stated, never replaced by a clean
    revision."""
    import subprocess

    from ..settings import REPO_ROOT

    digest = hashlib.sha256()
    package = REPO_ROOT / "src" / "rfp_assistant"
    for path in sorted(package.rglob("*.py")):
        digest.update(path.relative_to(package).as_posix().encode() + b"\0" + path.read_bytes())
    out = {"source_sha256": digest.hexdigest(), "git_revision": None, "git_dirty": None}
    try:
        rev = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                             timeout=10)
        status = subprocess.run(["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--untracked-files=no"],
                                capture_output=True, text=True, timeout=10)
        if rev.returncode == 0 and status.returncode == 0:
            out.update(git_revision=rev.stdout.strip(), git_dirty=bool(status.stdout.strip()))
        else:
            out["git_note"] = "not a Git checkout or Git refused; no revision recorded"
    except (OSError, subprocess.SubprocessError):
        out["git_note"] = "Git is not available; no revision recorded"
    return out


def hardware() -> dict:
    import os
    import platform

    return {"platform": platform.platform(), "machine": platform.machine(), "python": platform.python_version(),
            "cpu_count": os.cpu_count()}


def metric_code_sha256() -> str:
    """Hash of the scoring code (this module): part of every phase-4 freeze."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _headline(scores: dict) -> dict:
    agg = scores.get("aggregate") or {}
    if scores.get("status") != "complete":
        return {"status": scores.get("status"), "reason": scores.get("reason")}
    return {"status": "complete", "hit@20": agg["single_evidence"]["hit@20"]["rate"],
            "complete@20": agg["multi_evidence"]["complete@20"]["rate"], "ndcg@5": agg["ndcg@5"],
            "mrr": agg["mrr"], "packed_complete": agg["packed_complete"]["rate"],
            "critical_failures": len(agg["critical_failures"]), "p95_ms": agg["latency_ms"]["p95"]}


def load_run(settings: Settings, run_id: str) -> tuple[dict, dict]:
    d = _run_dir(settings, run_id)
    if not (d / "config.json").exists() or not (d / "scores.json").exists():
        raise EvaluationError(f"run {run_id} not found")
    return (json.loads((d / "config.json").read_text(encoding="utf-8")),
            json.loads((d / "scores.json").read_text(encoding="utf-8")))


def _latest_run(settings: Settings, label: str, dataset_sha: str, index_version: str,
                population: str | None = None) -> str | None:
    """Newest complete current-policy run matching dataset bytes, index and, when given, evaluated population."""
    base = settings.data_dir / "runs"
    found = []
    for d in base.glob(f"{label}-*") if base.exists() else []:
        try:
            config, scores = load_run(settings, d.name)
        except (EvaluationError, json.JSONDecodeError):
            continue
        if (config.get("eval_version") == EVAL_VERSION and config["dataset_sha256"] == dataset_sha
                and config["index_version"] == index_version
                and (population is None or config.get("population_sha256") == population)
                and scores.get("status") == "complete"):
            found.append((scores.get("created_at", ""), d.name))
    return max(found)[1] if found else None


def trial_reranker(settings: Settings, analyzer, dataset: str, depths: list[int], reranker=None,
                   load_info: dict | None = None, index_version: str | None = None, users: int = LOAD_USERS) -> dict:
    """Local-only reranker trial on the frozen H candidate pool: quality at each depth, warm latency alone and
    under `users` concurrent queries, and the promotion gate. A load failure records the bypass."""
    from ..retrieval import dense as dense_mod
    from ..retrieval.dense import percentile

    index, rows, skipped, h_run, h_config, h_scores = _frozen_h(settings, analyzer, dataset, index_version)
    # Retrieval runs exactly as H was frozen; only the reranker is new. A changed retrieval setting needs a new H.
    limits = {k: v for k, v in h_config["limits"].items() if k != "corpus_route"}  # a code constant, recorded only
    trial_settings = settings.with_(**limits, embedding_model=h_config["embedding"]["model"],
                                    embedding_dimensions=h_config["embedding"]["dims"])
    h_traces = {t["id"]: t for t in read_jsonl(_run_dir(settings, h_run) / "traces.jsonl")}
    dense = dense_mod.DenseIndex.load(settings, h_config["dense_version"], base=index)
    vectors = _cached_query_vectors(trial_settings, rows)
    if reranker is None:
        reranker, load_info = dense_mod.load_reranker(settings)
    load_info = load_info or getattr(reranker, "info", {})
    report: dict = {"h_run": h_run, "load": load_info, "depths": {}, "created_at": utcnow()}
    if reranker is None:
        return _bypass_trial(settings, report, h_run, h_config, depths, load_info)
    passage = [r for r in rows if is_passage_row(r)]
    failed = lambda reason, depth=None: _failed_trial(  # noqa: E731
        settings, report, h_run, h_config, load_info, reason, depth)

    # Warm once, then measure the reranking stage alone and under concurrent load.
    if passage:
        warm = _execute(trial_settings, index, analyzer, passage[:1], "hybrid_rerank", dense, vectors, reranker,
                        max(depths))
        if _inference_failure(warm):
            return failed(_inference_failure(warm))
    base_ndcg = h_scores["aggregate"]["ndcg@5"] or 0.0
    by_id = {row_id(r): r for r in rows}
    base_critical = set(critical_failures(list(h_traces.values()), by_id))  # same rule for both sides
    best = None
    for depth in depths:
        results = _execute(trial_settings, index, analyzer, rows, "hybrid_rerank", dense, vectors, reranker, depth)
        if _inference_failure(results):
            return failed(_inference_failure(results), depth)
        _check_frozen_pool(results, h_traces, depth)
        alone = [r["timings_ms"]["rerank"] for r in results if r.get("timings_ms")]
        loaded, queue, infer, unranked, under_load = _rerank_under_load(
            trial_settings, index, analyzer, passage, dense, vectors, reranker, depth, users)
        if under_load:
            return failed(f"under {users}-user load: {under_load}", depth)
        agg = aggregate(results, skipped)
        gate = _reranker_gate(agg, base_critical, base_ndcg, percentile(loaded, 0.95), percentile(unranked, 0.95),
                              users)
        config = {**h_config, "label": "HR", "mode": "hybrid_rerank", "h_run": h_run,
                  "eval_version": EVAL_VERSION, "reranker": _reranker_record(load_info), "rerank_depth": depth}
        run_id = f"HR-{hashlib.sha256(dumps(config).encode()).hexdigest()[:10]}"
        scores = {"status": "complete", "aggregate": agg, "gate": gate, "created_at": utcnow(),
                  "latency_ms": _trial_latency(results, alone, queue, infer, loaded, unranked), "load": load_info}
        _write_run(settings, run_id, config, results, scores)
        report["depths"][depth] = {"run_id": run_id, **gate, "ndcg@5": agg["ndcg@5"]}
        if gate["passed"] and (best is None or gate["ndcg@5_gain"] > report["depths"][best]["ndcg@5_gain"]):
            best = depth
    report["gate"] = {"passed": best is not None, "decision": f"eligible at depth {best}" if best else "bypass",
                      "best_depth": best}
    return report


def _frozen_h(settings: Settings, analyzer, dataset: str, index_version: str | None) -> tuple:
    """The index, the evaluated rows and the frozen H run the trial reranks: same dataset bytes, index, evaluated
    population and analyzer."""
    from ..retrieval.retrieval import KeywordIndex, analyzer_fingerprint, index_compatibility
    from ..storage.store import get_app_setting

    with open_db(settings.db_path) as conn:
        index_version = index_version or get_app_setting(conn, "active_index")
    index = KeywordIndex.load(settings, index_version)
    rows, skipped, dataset_sha = load_eval_rows(settings, dataset, extractions=index.source_extraction)
    if index_compatibility(index, analyzer):
        raise EvaluationError(index_compatibility(index, analyzer))
    # A newer H over another population (e.g. before a source revision was reverted) must not shadow the one
    # matching today's population.
    h_run = _latest_run(settings, "H", dataset_sha, index.version, population_identity(rows, skipped))
    if h_run is None:
        raise EvaluationError("no complete frozen H run for this dataset, index and evaluated population; "
                              "run evaluate-retrieval with H")
    h_config, h_scores = load_run(settings, h_run)
    if h_config.get("population_sha256") != population_identity(rows, skipped):
        raise EvaluationError("the evaluated population changed since the frozen H run; rerun H before the trial")
    if h_config["analyzer"] != analyzer_fingerprint(analyzer):
        raise EvaluationError("the analyzer differs from the frozen H run; rerun H before the trial")
    return index, rows, skipped, h_run, h_config, h_scores


def _cached_query_vectors(trial_settings: Settings, rows: list[dict]) -> dict:
    """The frozen H query vectors, from the cache only."""
    from ..retrieval import dense as dense_mod

    vectors = {}
    for row in rows:
        if is_passage_row(row):
            vec, _ = dense_mod.query_vector(trial_settings, None, row["question"], request_id=None, member_id="owner-cli",
                                            purpose="gold_eval", allow_paid=False)
            if vec is None:
                raise EvaluationError("a frozen H query vector is missing from the cache; rerun H")
            vectors[row_id(row)] = vec
    return vectors


def _reranker_record(load_info: dict) -> dict:
    return {k: load_info.get(k) for k in ("model", "revision", "device", "max_length", "max_concurrency", "precision")}


def _inference_failure(results: list[dict]) -> str | None:
    return next((r["fallback"] for r in results if (r.get("fallback") or "").startswith("hybrid_rerank->")), None)


def _bypass_trial(settings: Settings, report: dict, h_run: str, h_config: dict, depths: list[int],
                  load_info: dict) -> dict:
    """The reranker did not load: a blocked trial with the bypass decision."""
    report["gate"] = {"passed": False, "decision": "bypass", "reason": f"reranker unavailable: "
                                                                       f"{load_info.get('error')}"}
    run_id = f"HR-trial-{hashlib.sha256(dumps([h_run, depths, load_info]).encode()).hexdigest()[:10]}"
    _write_run(settings, run_id, {**h_config, "label": "HR", "mode": "hybrid_rerank"},
               [], {"status": "blocked", "reason": report["gate"]["reason"], "trial": report})
    return {"run_id": run_id, **report}


def _failed_trial(settings: Settings, report: dict, h_run: str, h_config: dict, load_info: dict, reason: str,
                  depth=None) -> dict:
    """An inference error is recorded as a blocked trial with the bypass decision, never as a measurement."""
    report["gate"] = {"passed": False, "decision": "bypass", "reason": f"reranker inference failed: {reason}",
                      "depth": depth}
    run_id = f"HR-failed-{hashlib.sha256(dumps([h_run, depth, reason, utcnow()]).encode()).hexdigest()[:10]}"
    _write_run(settings, run_id, {**h_config, "label": "HR", "mode": "hybrid_rerank", "h_run": h_run,
                                  "eval_version": EVAL_VERSION, "rerank_depth": depth,
                                  "reranker": _reranker_record(load_info)},
               [], {"status": "blocked", "reason": report["gate"]["reason"], "trial": report, "load": load_info})
    return {"run_id": run_id, **report}


def _check_frozen_pool(results: list[dict], h_traces: dict, depth: int) -> None:
    """The reranked pool must be exactly the frozen H candidates."""
    for r in results:
        if r.get("metrics"):
            frozen = [c["chunk_id"] for c in h_traces[r["id"]]["candidates"] if c["channel"] == "rrf"][:depth]
            now = [c["chunk_id"] for c in r["candidates"] if c["channel"] == "rrf"][:depth]
            if frozen != now:
                raise EvaluationError(f"H candidates changed for {r['id']}; rerun H before the trial")


def _rerank_under_load(trial_settings: Settings, index, analyzer, passage: list[dict], dense, vectors: dict,
                       reranker, depth: int, users: int) -> tuple[list, list, list, list, str | None]:
    """HR and H latencies with `users` concurrent queries: HR totals, queue and inference times, H totals, and the
    first inference failure under load."""
    from concurrent.futures import ThreadPoolExecutor

    def one(row):
        t0 = time.perf_counter()
        (r,) = _execute(trial_settings, index, analyzer, [row], "hybrid_rerank", dense, vectors, reranker, depth)
        timings = r.get("timings_ms") or {}
        return ((time.perf_counter() - t0) * 1000, timings.get("rerank_queue"), timings.get("rerank_infer"),
                _inference_failure([r]))

    def base(row):
        t0 = time.perf_counter()
        _execute(trial_settings, index, analyzer, [row], "hybrid", dense, vectors)
        return (time.perf_counter() - t0) * 1000

    with ThreadPoolExecutor(max_workers=users) as pool:
        measured = list(pool.map(one, passage * max(1, users // max(1, len(passage)))))
        loaded = [m[0] for m in measured]
        queue = [m[1] for m in measured if m[1] is not None]
        infer = [m[2] for m in measured if m[2] is not None]
        unranked = list(pool.map(base, passage * max(1, users // max(1, len(passage)))))
    return loaded, queue, infer, unranked, next((m[3] for m in measured if m[3]), None)


def _reranker_gate(agg: dict, base_critical: set, base_ndcg: float, hr_p95: float | None, h_p95: float | None,
                   users: int) -> dict:
    new_critical = sorted(set(agg["critical_failures"]) - base_critical)
    added_p95 = round((hr_p95 or 0) - (h_p95 or 0), 1)
    gain = round((agg["ndcg@5"] or 0.0) - base_ndcg, 4)
    return {"ndcg@5_gain": gain, "required_gain": GATE_NDCG_GAIN, "new_critical_failures": new_critical,
            "added_p95_ms_under_load": added_p95, "allowed_added_p95_ms": GATE_ADDED_P95_MS, "users": users,
            "passed": gain >= GATE_NDCG_GAIN and not new_critical and added_p95 <= GATE_ADDED_P95_MS,
            "pilot_rows": agg["passage_rows"],
            "note": "pilot-sized sample: review a marginal gain on the larger dev set before final release"}


def _trial_latency(results: list[dict], alone: list, queue: list, infer: list, loaded: list,
                   unranked: list) -> dict:
    from ..retrieval.dense import percentile

    return {"rerank_alone_p50": percentile(alone, 0.5), "rerank_alone_p95": percentile(alone, 0.95),
            "queue_p95_under_load": percentile(queue, 0.95),
            "infer_p95_under_load": percentile(infer, 0.95),
            "truncated_pairs": sum((r.get("timings_ms") or {}).get("rerank_truncated") or 0 for r in results),
            "hr_under_load_p95": percentile(loaded, 0.95),
            "h_under_load_p95": percentile(unranked, 0.95)}


def run_errors(settings: Settings, run_id: str) -> list[str]:
    """Why a run cannot serve: incomplete, superseded policy, failed or unmeasured gate, artifacts not ready."""
    from ..retrieval import dense as dense_mod
    from ..retrieval.retrieval import KeywordIndex, RetrievalError, corpus_route_record

    config, scores = load_run(settings, run_id)
    errors = []
    if scores.get("status") != "complete":
        errors.append("the run is not complete")
    route = (config.get("limits") or {}).get("corpus_route")
    if route != corpus_route_record():
        errors.append(f"the run was measured under corpus-routing rule {(route or {}).get('rule')!r}, not "
                      f"{corpus_route_record()['rule']!r}; rerun the comparison")
    if config.get("eval_version") != EVAL_VERSION:
        errors.append(f"the run was scored under evaluation policy {config.get('eval_version')!r}, not "
                      f"{EVAL_VERSION!r}; rerun the comparison (and the reranker trial) — cached vectors are reused")
    if config.get("eval_version") == EVAL_VERSION:
        try:
            if current_population(settings, config["dataset"])[0] != config.get("population_sha256"):
                errors.append("the evaluated population changed since the run (source revision, review or dataset "
                              "edit); repin or rerun on the current dataset")
        except EvaluationError as exc:
            errors.append(f"cannot recheck the evaluated population: {exc}")
    # A failed reranker gate is a column the person reads, not a refusal (operating rule, 0-overview.md).
    if config["mode"] == "hybrid_rerank" and not (config.get("reranker") or {}).get("max_concurrency"):
        errors.append("the trial recorded no reranker concurrency bound; rerun trial-reranker")
    try:
        index = KeywordIndex.load(settings, config["index_version"])
        if index.manifest_hash != config["index_manifest_hash"]:
            errors.append("keyword index manifest changed since the run")
        from ..retrieval.retrieval import index_compatibility

        if index_compatibility(index):
            errors.append(index_compatibility(index))
    except RetrievalError as exc:
        errors.append(f"keyword index not ready: {exc}")
        index = None
    if config.get("dense_version") and index is not None:
        try:
            dense_mod.DenseIndex.load(settings, config["dense_version"], base=index)
        except dense_mod.DenseError as exc:
            errors.append(f"dense matrix not ready: {exc}")
    return errors


def decision_errors(settings: Settings, run_id: str, decision: dict) -> list[str]:
    config, _ = load_run(settings, run_id)
    errors = []
    if decision.get("run_id") != run_id:
        errors.append("decision run_id does not match --run-id")
    if decision.get("mode") != config["mode"]:
        errors.append(f"decision mode must be the run's mode ({config['mode']})")
    if not str(decision.get("decided_by") or "").strip():
        errors.append("decision requires 'decided_by' (the person who picks the row); the note is optional")
    embedding = config.get("embedding")
    if embedding:
        from ..retrieval.models import EMBEDDINGS

        spec = EMBEDDINGS.get(embedding.get("model"))
        if spec is None:
            errors.append(f"the run's embedding model {embedding.get('model')} is not a compared model")
        elif spec.backend != "openai" and embedding.get("policy") != spec.policy:
            errors.append(f"the run's {spec.key} revision or prefixes differ from the pinned registry entry; rerun")
    finalist = decision.get("finalist_run_id")
    if finalist:
        try:
            f_config, f_scores = load_run(settings, finalist)
        except EvaluationError:
            errors.append(f"finalist run {finalist} not found")
        else:
            if (f_scores.get("status") != "complete" or f_config.get("eval_version") != EVAL_VERSION
                    or f_config.get("population_sha256") != config.get("population_sha256")):
                errors.append("the finalist must be a complete current-policy run on the same evaluated population")
    # Unlabelled top-5 passages are shown beside the row's nDCG; the person reads them there, so none is required here.
    return errors + run_errors(settings, run_id)


def serving_config(run_id: str, config: dict) -> dict:
    """The serving configuration a recorded retrieval run describes (what `activate-run` stores and what the
    phase-4 answer evaluation pins)."""
    return {"run_id": run_id, "label": config["label"], "mode": config["mode"],
            "index_version": config["index_version"], "dense_version": config.get("dense_version"),
            "reranker": {**config["reranker"], "depth": config["rerank_depth"],
                         "protect": config.get("rerank_protect", 0)} if config.get("reranker") else None,
            "embedding": config.get("embedding"), "limits": config.get("limits"),
            "eval_version": config["eval_version"], "fallback_mode": "kiwi_bm25"}


def activate_run(settings: Settings, run_id: str, decision_path: Path, actor: str = "owner-cli") -> dict:
    """Validates a reviewed selection and its ready artifacts, then switches the serving configuration in one
    transaction. The previous configuration is kept in the append-only activation history."""
    if not decision_path.is_absolute():
        raise EvaluationError("--decision-file must be an absolute path")
    return activate_decision(settings, run_id, json.loads(decision_path.read_text(encoding="utf-8")), actor)


def activate_decision(settings: Settings, run_id: str, decision: dict, actor: str) -> dict:
    """`activate-run` with the decision given directly: the 실험 비교 view sends the person's name and note."""
    from ..storage.store import get_app_setting, set_app_setting, tx

    config, scores = load_run(settings, run_id)
    errors = decision_errors(settings, run_id, decision)
    if errors:
        raise EvaluationError("; ".join(errors))
    active = {**serving_config(run_id, config), "finalist_run_id": decision.get("finalist_run_id"),
              "activated_at": utcnow()}
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        previous = get_app_setting(conn, "active_run")
        set_app_setting(conn, "active_run", dumps(active))
        set_app_setting(conn, "active_index", config["index_version"])
        conn.execute("INSERT INTO activations(activation_id, run_id, config_json, decision_json, previous_json, actor, "
                     "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (hashlib.sha256(f"{run_id}:{utcnow()}".encode()).hexdigest()[:20], run_id, dumps(active),
                      dumps(decision), previous, actor, utcnow()))
    return active


MIN_SELECTION_GAIN = 0.03  # nDCG@5 gain a dense or reranked mode needs over K1, as for the reranker gate


def run_summary(settings: Settings, run_id: str) -> dict:
    config, scores = load_run(settings, run_id)
    agg = scores.get("aggregate") or {}
    single = (agg.get("single_evidence") or {}).get("hit@20") or {}
    multi = (agg.get("multi_evidence") or {}).get("complete@20") or {}
    packed = agg.get("packed_complete") or {}
    return {
        "run_id": run_id, "label": config.get("label"), "mode": config.get("mode"),
        "eval_version": config.get("eval_version"), "dataset_sha256": config.get("dataset_sha256"),
        "population_sha256": config.get("population_sha256"), "population_size": config.get("population_size"),
        "limits": config.get("limits"),
        "index_version": config.get("index_version"), "profile": config.get("profile"),
        "dense_version": config.get("dense_version"), "status": scores.get("status"), "reason": scores.get("reason"),
        "scored": agg.get("passage_rows"), "hit@20": single, "complete@20": multi, "packed_complete": packed,
        "ndcg@5": agg.get("ndcg@5"), "mrr": agg.get("mrr"),
        "critical_failures": agg.get("critical_failures"), "code_checks": agg.get("code_checks"),
        "wrong_scope": agg.get("wrong_scope_candidates"), "latency_ms": agg.get("latency_ms"),
        "query_cost_micro_usd": (scores.get("query_embedding") or {}).get("settled_micro_usd"),
        "gate": scores.get("gate"), "ndcg_pool": agg.get("ndcg_pool"), "blocking": run_errors(settings, run_id)}


def pool_pending(summary: dict) -> int:
    """Returned top-5 passages outside every gold label that no review has judged: while any remain, an nDCG
    difference may only reflect missing labels (pilot runs have no pool and report 0)."""
    return int(((summary.get("ndcg_pool") or {}).get("unlabelled_top5_passages")) or 0)


def recommend(summaries: list[dict]) -> dict:
    """K1 serves unless another complete current-policy mode on the same dataset shows a measured benefit:
    no critical failure K1 does not have, and nDCG@5 at least MIN_SELECTION_GAIN higher. The runner-up becomes the
    phase 4 finalist. Every reason is stated; nothing is chosen from a run with blocking problems."""
    usable = [r for r in summaries if not r["blocking"]]
    k1 = [r for r in usable if r["label"] == "K1"]
    if not k1:
        return {"selected": None, "finalist": None, "status": "none",
                "reasons": ["no usable current-policy K1 run to compare against"]}
    base = max(k1, key=lambda r: (r["ndcg@5"] or 0, r["run_id"]))
    compared = [r for r in usable if r["label"] != "K0" and r["population_sha256"] == base["population_sha256"]]
    pending = {r["run_id"]: pool_pending(r) for r in compared if pool_pending(r)}
    reasons, eligible = [], [base]
    for r in usable:
        if r is base or r["label"] in ("K0",):
            continue
        if r["population_sha256"] != base["population_sha256"]:
            reasons.append(f"{r['run_id']} ({r['mode']}): scored a different population than K1; not compared")
            continue
        new = sorted(set(r["critical_failures"] or []) - set(base["critical_failures"] or []))
        gain = round((r["ndcg@5"] or 0) - (base["ndcg@5"] or 0), 4)
        if new:
            reasons.append(f"{r['run_id']} ({r['mode']}): new critical failures {new}")
        elif gain < MIN_SELECTION_GAIN:
            reasons.append(f"{r['run_id']} ({r['mode']}): nDCG@5 gain {gain} below {MIN_SELECTION_GAIN}")
        else:
            reasons.append(f"{r['run_id']} ({r['mode']}): nDCG@5 gain {gain}, no new critical failure")
            eligible.append(r)
    if pending:
        # Plan §3: unjudged passages are reviewed before a development comparison is final. Until then K1 stays as
        # the provisional default, and no gain counts as a measured benefit or names a finalist.
        reasons.append(f"comparison pending pool review: unreviewed top-5 passages outside the gold labels {pending}; "
                       "gains above are provisional. Review them (`unlabelled_chunks@5` in each run's traces): add a "
                       "passage holding a required fact as a new alternative and rescore, or record the review in "
                       "the decision's `pool_review`")
        reasons.append(f"K1 {base['run_id']} stays provisionally; no finalist while the review is pending")
        return {"selected": base["run_id"], "finalist": None, "status": "pending_pool_review", "provisional": True,
                "pool_pending": pending, "reasons": reasons}
    order = sorted(eligible, key=lambda r: (-(r["ndcg@5"] or 0), len(r["critical_failures"] or []), r["run_id"]))
    selected = order[0]
    others = [r for r in usable if r is not selected and r["population_sha256"] == selected["population_sha256"]
              and r["label"] != "K0"]
    finalist = max(others, key=lambda r: (r["ndcg@5"] or 0, r["run_id"])) if others else None
    if selected is base:
        reasons.append(f"K1 {base['run_id']} stays: no other mode showed a measured benefit")
    return {"selected": selected["run_id"], "finalist": finalist["run_id"] if finalist else None, "status": "final",
            "reasons": reasons}


def _fmt_rate(x: dict) -> str:
    if not x or x.get("denominator") in (None, 0):
        return "—"
    return f"{x['rate']} ({x['numerator']}/{x['denominator']}, {x['wilson95']})"


def compare_runs(settings: Settings, run_ids: list[str]) -> dict:
    """Comparison table from recorded scores only; missing measurements stay empty. Writes Markdown and JSON
    under runs/comparisons/."""
    summaries = [run_summary(settings, r) for r in run_ids]
    datasets = {r["population_sha256"] for r in summaries}
    policies = {r["eval_version"] for r in summaries}
    lines = ["# Retrieval comparison", "", f"Generated {utcnow()}. Values come from each run's scores.json.", ""]
    if len(datasets) > 1 or len(policies) > 1:
        lines += ["**Not directly comparable:** runs differ in evaluated population or evaluation policy.", ""]
    lines += ["| Run | Mode | Dataset/population | Index (profile) | Policy | Scored | hit@20 single | complete@20 multi | "
              "packed complete | nDCG@5 | MRR | Critical | Code ok/n | Wrong scope | p50/p95 ms | Query cost µ$ | "
              "Blocking |", "|" + " --- |" * 17]
    for r in summaries:
        lat = r["latency_ms"] or {}
        code = r["code_checks"] or {}
        lines.append(
            f"| `{r['run_id']}` | {r['mode']} | `{(r['dataset_sha256'] or '')[:12]}`/`{(r['population_sha256'] or '')[:8]}` | `{r['index_version']}` "
            f"({r['profile']}) | {r['eval_version']} | {r['scored'] if r['scored'] is not None else '—'} | "
            f"{_fmt_rate(r['hit@20'])} | {_fmt_rate(r['complete@20'])} | {_fmt_rate(r['packed_complete'])} | "
            f"{r['ndcg@5'] if r['ndcg@5'] is not None else '—'} | {r['mrr'] if r['mrr'] is not None else '—'} | "
            f"{r['critical_failures'] if r['critical_failures'] is not None else '—'} | "
            f"{code.get('ok', '—')}/{code.get('n', '—')} | {r['wrong_scope'] if r['wrong_scope'] is not None else '—'} | "
            f"{lat.get('p50', '—')}/{lat.get('p95', '—')} | {r['query_cost_micro_usd'] if r['query_cost_micro_usd'] is not None else '—'} | "
            f"{'; '.join(r['blocking']) or 'none'} |")
    rec = recommend(summaries)
    lines += ["", "## Recommendation (draft for the owner)", "",
              f"- status: {rec.get('status')}" + (" (provisional)" if rec.get("provisional") else ""),
              f"- selected: `{rec['selected']}`",
              f"- phase 4 finalist: `{rec['finalist']}`", *[f"- {x}" for x in rec["reasons"]], "",
              "Small pilot denominators make single-question differences large; read the Wilson intervals.", ""]
    key = hashlib.sha256("|".join(sorted(run_ids)).encode()).hexdigest()[:12]
    out = settings.data_dir / "runs" / "comparisons" / f"{key}.md"
    write_text_atomic(out, "\n".join(lines))
    write_text_atomic(out.with_suffix(".json"), json.dumps({"runs": summaries, "recommendation": rec},
                                                           ensure_ascii=False, indent=1))
    return {"markdown": str(out), "recommendation": rec, "runs": summaries}


def draft_activation(settings: Settings, run_ids: list[str], out_path: Path, select: str | None = None) -> dict:
    """An activation decision file the owner completes: selection and finalist with their evidence, a rationale
    draft built from recorded numbers, and every check `activate-run` would still fail. `decided_by` and
    `rationale` stay empty, so the draft cannot activate anything by itself."""
    if not out_path.is_absolute():
        raise EvaluationError("--out must be an absolute path")
    comparison = compare_runs(settings, run_ids)
    rec = comparison["recommendation"]
    chosen = select or rec["selected"]
    if chosen is None:
        raise EvaluationError("; ".join(rec["reasons"]))
    by_id = {r["run_id"]: r for r in comparison["runs"]}
    if chosen not in by_id:
        raise EvaluationError("--select must be one of --runs")
    s = by_id[chosen]
    finalist = rec["finalist"] if rec["finalist"] != chosen else None
    draft = {"run_id": chosen, "mode": s["mode"], "decided_by": "", "rationale": "",
             "finalist_run_id": finalist,
             "rationale_draft": (f"{s['mode']} run {chosen} on dataset {s['dataset_sha256'][:12]} "
                                 f"(scored {s['scored']}): nDCG@5 {s['ndcg@5']}, single hit@20 "
                                 f"{_fmt_rate(s['hit@20'])}, packed complete {_fmt_rate(s['packed_complete'])}, "
                                 f"critical failures {s['critical_failures']}. " + " ".join(rec["reasons"])),
             "overridden_recommendation": select is not None and select != rec["selected"],
             "recommendation_status": rec.get("status"), "pool_pending": rec.get("pool_pending") or {},
             "comparison": comparison["markdown"]}
    draft["blocking"] = [e for e in decision_errors(settings, chosen, draft)
                         if "decided_by" not in e and "rationale" not in e]
    write_text_atomic(out_path, json.dumps(draft, ensure_ascii=False, indent=1))
    return draft


def write_phase2_report(settings: Settings) -> Path:
    """`.runtime/releases/phase-2/report.md`: what is actually recorded in this data directory, nothing
    assumed. Missing steps appear as missing."""
    from ..corpus.ingestion import identity_report, manifest_report, review_coverage
    from ..storage.store import get_app_setting

    manifest = manifest_report(settings)
    coverage = review_coverage(settings)
    identity = identity_report(settings)
    fam_path = settings.data_dir / "datasets" / "families.json"
    families = json.loads(fam_path.read_text(encoding="utf-8")) if fam_path.exists() else {"families": {}}
    with open_db(settings.db_path) as conn:
        indexes = [dict(r) for r in conn.execute("SELECT index_version, state, config_json, created_at FROM indexes "
                                                 "ORDER BY created_at")]
        recoveries = [dict(r) for r in conn.execute(
            "SELECT e.source_hash, e.extraction_id, e.recovery_json FROM extractions e WHERE e.recovery_json IS NOT NULL")]
        spend = [dict(r) for r in conn.execute(
            "SELECT purpose, stage, state, COUNT(*) AS n, COALESCE(SUM(settled_micro_usd), 0) AS settled, "
            "COALESCE(SUM(reserved_micro_usd), 0) AS reserved FROM attempts GROUP BY purpose, stage, state")]
        envelopes = json.loads(conn.execute("SELECT envelopes_json FROM budget_settings WHERE id = 1").fetchone()[0])
        from ..gateway.budget import _purpose_used

        used = {k: _purpose_used(conn, k) for k in envelopes}
        activations = [dict(r) for r in conn.execute("SELECT run_id, config_json, decision_json, actor, created_at "
                                                     "FROM activations ORDER BY created_at")]
        active_run = get_app_setting(conn, "active_run")
        active_index = get_app_setting(conn, "active_index")
    runs = _phase2_runs(settings)
    checked = [c for c in coverage if c["review_status"] in ("sample_checked", "reviewed")]
    L = ["# Phase 2 report — corpus coverage and retrieval selection", "",
         f"Generated {utcnow()} from `{settings.data_dir.name}/`. Every figure below is read from recorded state; "
         "steps that were not run are listed as missing.", ""]
    L += _phase2_source_lines(manifest, checked, recoveries, identity)
    L += _phase2_retrieval_lines(families, fam_path, indexes, runs)
    L += _phase2_spend_lines(spend, used, envelopes)
    from ..service.service import active_serving

    now = active_serving(settings)  # what requests serve; a stored activation can be refused (routing changed)
    L += _phase2_selection_lines(now, active_run, activations, active_index)
    gates, a = _phase2_gates(settings, manifest, checked, recoveries, runs, active_run)
    L += ["", "## Phase 2 exit gates", "", "| Gate | Met | Evidence |", "| --- | --- | --- |"]
    L += [f"| {g} | {'yes' if ok else '**no**'} | {ev} |" for g, ok, ev in gates]

    # ---- inputs for phase 3 (and the phase 4 finalist)
    with open_db(settings.db_path) as conn:
        source_map = [dict(r) for r in conn.execute(
            "SELECT d.doc_id, d.active_source_hash AS source_hash, s.active_extraction_id AS extraction_id, "
            "s.parse_status, s.review_status FROM documents d JOIN sources s ON s.source_hash = d.active_source_hash "
            "ORDER BY d.csv_row_id")]
    release = settings.data_dir / "releases" / "phase-2"
    write_text_atomic(release / "source-map.json", json.dumps(source_map, ensure_ascii=False, indent=1))
    L += _phase2_inputs_lines(now, active_index, activations, len(source_map), a, gates)
    path = release / "report.md"
    write_text_atomic(path, "\n".join(L))
    write_text_atomic(release / "manifest.json", json.dumps({
        "release_id": "phase-2", "created_at": utcnow(), "eval_version": EVAL_VERSION, "active_run": a, "serving": now,
        "active_index": active_index, "gates": [{"gate": g, "met": ok, "evidence": ev} for g, ok, ev in gates],
        "runs": [r["run_id"] for r in runs]}, ensure_ascii=False, indent=1))
    return path


def _phase2_runs(settings: Settings) -> list[dict]:
    """Every recorded retrieval run with its headline scores, in run-directory order."""
    runs = []
    base = settings.data_dir / "runs"
    for d in sorted(base.iterdir()) if base.exists() else []:
        try:
            config, scores = load_run(settings, d.name)
        except (EvaluationError, json.JSONDecodeError):
            continue
        runs.append({"run_id": d.name, "label": config.get("label"), "profile": config.get("profile"),
                     "eval_version": config.get("eval_version"), "population": config.get("population_sha256"),
                     "dataset_name": config.get("dataset"), "limits": config.get("limits"),
                     "index": config.get("index_version"), **_headline(scores),
                     "gate": (scores.get("gate") or {}).get("passed"), "load": scores.get("load")})
    return runs


def _phase2_source_lines(manifest: dict, checked: list[dict], recoveries: list[dict], identity: list[dict]) -> list[str]:
    """Manifest and source review, quarantine and recovery, identity and provenance."""
    L = ["## Manifest and source review", "",
         f"- associations: {manifest['counts']['associations']} ({manifest['counts']['hwp']} HWP, "
         f"{manifest['counts']['pdf']} PDF), unique sources: {manifest['counts']['unique_sources']}",
         f"- parse status: {manifest['parse_status']}", f"- review status: {manifest['review_status']}",
         f"- audit differences: {manifest['audit_differences'] or 'none'}",
         f"- human-checked sources (sample_checked/reviewed): {len(checked)}; automatic verdicts are not counted as "
         "reviewed coverage", "",
         "| Source | Status | Reviewer | Current revision | Locations | Coverage | Limitations |",
         "| --- | --- | --- | --- | --- | --- | --- |"]
    for c in checked:
        L.append(f"| {c['filenames'][:60]} | {c['review_status']} | {c['reviewer']} | {c['review_is_current']} | "
                 f"{c['locations']} | {json.dumps(c['coverage'], ensure_ascii=False)[:80]} | "
                 f"{json.dumps(c['limitations'], ensure_ascii=False)[:80]} |")
    L += ["", "## Quarantine and recovery", ""]
    for q in manifest["quarantined"]:
        L.append(f"- quarantined: {q['filename']} — `{q['reason_code']}`")
    for r in recoveries:
        rec = json.loads(r["recovery_json"])
        L.append(f"- recovered: source `{r['source_hash'][:16]}` → extraction `{r['extraction_id']}` via "
                 f"{rec.get('method')} (converted `{rec.get('converted_hash', '')[:16]}`, reviewer {rec.get('reviewer')}; "
                 f"limitations: {rec.get('mapping_limitations')})")
    if not manifest["quarantined"] and not recoveries:
        L.append("- none")
    conflicts = [i for i in identity if i["provenance_conflicts"]]
    disagree = [i for i in identity if False in i["agreement"].values()]
    L += ["", "## Identity and provenance", "",
          f"- associations sharing bytes with conflicting metadata: {len(conflicts)}",
          f"- CSV institution/title that the original's own cues contradict: {len(disagree)}"]
    for i in conflicts + disagree:
        L.append(f"  - {i['filename']}: conflicts {[c['field'] for c in i['provenance_conflicts']]}, agreement "
                 f"{i['agreement']}, resolutions {list(i['resolutions'])}")
    return L


def _phase2_retrieval_lines(families: dict, fam_path: Path, indexes: list[dict], runs: list[dict]) -> list[str]:
    """Evaluation families, index versions, retrieval runs and reranker load."""
    fams = families["families"]
    L = ["", "## Evaluation families", ""]
    if not fam_path.exists():
        L.append("- families.json is missing: run `validate-gold` or `gold submit`, which assign families")
    L += [f"- families: {len(fams)} (dev {sum(f['split'] == 'dev' for f in fams.values())}, "
          f"test {sum(f['split'] == 'test' for f in fams.values())}); multi-document families: "
          f"{sum(len(f['doc_ids']) > 1 for f in fams.values())}",
          f"- related documents across splits: {families.get('related_across_splits') or 'none'}", "",
          "## Index versions", "", "| Version | Kind/profile | State | Created |", "| --- | --- | --- | --- |"]
    for ix in indexes:
        cfg = json.loads(ix["config_json"])
        kind = f"dense {cfg.get('model')}/{cfg.get('dimensions')} over {cfg.get('base_index_version')}" \
            if cfg.get("kind") == "dense" else f"keyword {cfg.get('profile', 'structural')} ({cfg.get('review_scope')})"
        L.append(f"| `{ix['index_version']}` | {kind} | {ix['state']} | {ix['created_at'][:19]} |")
    L += ["", "## Retrieval runs (development, retrieval only)", "",
          "| Run | Label | Profile | Status | hit@20 | complete@20 | nDCG@5 | MRR | packed complete | critical | "
          "p95 ms | gate |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in runs:
        L.append(f"| `{r['run_id']}` | {r['label']} | {r['profile']} | {r.get('status')} | {r.get('hit@20')} | "
                 f"{r.get('complete@20')} | {r.get('ndcg@5')} | {r.get('mrr')} | {r.get('packed_complete')} | "
                 f"{r.get('critical_failures')} | {r.get('p95_ms')} | {r.get('gate')} |")
    if not runs:
        L.append("| — | no runs recorded | | | | | | | | | | |")
    loads = [r["load"] for r in runs if r.get("load")]
    L += ["", "## Reranker load and latency", ""]
    L += [f"- {json.dumps(x, ensure_ascii=False)}" for x in loads] or ["- no reranker trial recorded"]
    return L


def _phase2_spend_lines(spend: list[dict], used: dict, envelopes: dict) -> list[str]:
    usd = lambda m: f"${m / 1_000_000:.6f}"  # noqa: E731
    L = ["", "## Spend (tracked by this ledger)", "", "| Purpose | Stage | State | Attempts | Settled | Reserved |",
         "| --- | --- | --- | --- | --- | --- |"]
    L += [f"| {s['purpose']} | {s['stage']} | {s['state']} | {s['n']} | {usd(s['settled'])} | {usd(s['reserved'])} |"
          for s in spend] or ["| — | — | — | 0 | $0 | $0 |"]
    return L + ["", "Envelopes (used incl. open reservations / envelope): " + ", ".join(
        f"{k} {usd(used[k])}/{usd(v)}" for k, v in envelopes.items()), ""]


def _phase2_selection_lines(now: dict, active_run: str | None, activations: list[dict],
                            active_index: str | None) -> list[str]:
    from ..service.service import describe_serving

    L = ["## Selection", ""]
    if active_run:
        a = json.loads(active_run)
        L += [f"- activated: run `{a['run_id']}` ({a['label']}, {a['mode']}), keyword index `{a['index_version']}`, "
              f"dense `{a.get('dense_version')}`, reranker {a.get('reranker')}",
              f"- requests serve: {describe_serving(now)}",
              f"- fallback: {a.get('fallback_mode')}; retrieval finalist for phase 4: {a.get('finalist_run_id')}"]
        for act in activations:
            L.append(f"  - {act['created_at'][:19]} {act['actor']}: {json.loads(act['decision_json']).get('rationale')}")
    else:
        L += [f"- no `activate-run` decision recorded: serving the keyword default (kiwi_bm25) on index "
              f"`{active_index}`; dense and reranker stay inactive"]
    return L


def _phase2_inputs_lines(now: dict, active_index: str | None, activations: list[dict], mapped: int, a: dict | None,
                         gates: list[tuple[str, bool, str]]) -> list[str]:
    """Inputs for phase 3 (and the phase 4 finalist)."""
    from ..service.service import describe_serving

    previous = [json.loads(x["config_json"])["run_id"] for x in activations[:-1]] if activations else []
    return ["", "## Inputs for phase 3", "",
            f"- serving: {describe_serving(now)}, keyword index "
            f"`{now.get('index_version') or active_index}`, dense `{now.get('dense_version')}`, "
            f"reranker {now.get('reranker')}, limits {now.get('limits')}",
            f"- rollback: keyword fallback `kiwi_bm25` always; earlier activations {previous or 'none'}; every earlier "
            "index directory stays on disk",
            f"- immutable evidence mapping: `releases/phase-2/source-map.json` ({mapped} associations → "
            "source hash → active extraction)",
            f"- phase 4 retrieval finalist: {(a or {}).get('finalist_run_id')}",
            f"- open gates: {[g for g, ok, _ in gates if not ok] or 'none'}", ""]


def _phase2_gates(settings: Settings, manifest: dict, checked: list[dict], recoveries: list[dict], runs: list[dict],
                  active_run: str | None) -> tuple[list[tuple[str, bool, str]], dict | None]:
    """The exit gates, computed from recorded state, never assumed, and the stored activation they judged."""
    # Revalidated now (free, no provider): a stored passing report says nothing about today's dataset or sources.
    validation = validate_gold(settings, "dev-pilot") if dataset_path(settings, "dev-pilot").exists() else None
    try:
        population = current_population(settings, "dev-pilot")[0]
    except EvaluationError:
        population = None
    current = [r for r in runs if r.get("status") == "complete" and r.get("eval_version") == EVAL_VERSION
               and r.get("dataset_name") == "dev-pilot" and population and r.get("population") == population]
    labels = {r["label"] for r in current}
    lexical_pair = next(((k0, k1) for k0 in current if k0["label"] == "K0" for k1 in current if k1["label"] == "K1"
                         and k1["index"] == k0["index"] and k1["limits"] == k0["limits"]), None)
    a = json.loads(active_run) if active_run else None
    active_errors = []
    if a and a.get("run_id"):
        try:
            active_errors = run_errors(settings, a["run_id"])
        except EvaluationError as exc:
            active_errors = [str(exc)]
    active_ok = bool(a and a.get("eval_version") == EVAL_VERSION and not active_errors)
    gates = [
        ("manifest covers every CSV record with a status", manifest["counts"]["associations"] > 0
         and sum(manifest["parse_status"].values()) == manifest["counts"]["associations"],
         f"{manifest['counts']['associations']} associations; audit differences {manifest['audit_differences'] or 'none'}"),
        ("human-reviewed source coverage recorded", bool(checked), f"{len(checked)} sources sample_checked/reviewed"),
        ("failed originals recovered or visibly quarantined", True,
         f"{len(manifest['quarantined'])} quarantined, {len(recoveries)} recovered"),
        ("independently reviewed dev dataset validates", bool(validation and validation.get("ok")),
         "dataset file missing" if validation is None else
         f"revalidated now: rows {validation.get('rows')}, errors {len(validation.get('errors', []))}"),
        ("frozen K0/K1 comparison under the current policy", bool(lexical_pair and validation and validation.get("ok")),
         f"pair {lexical_pair[0]['run_id']} / {lexical_pair[1]['run_id']} on index {lexical_pair[0]['index']}"
         + ("" if validation and validation.get("ok") else ", but the dataset does not validate")
         if lexical_pair else f"no K0/K1 pair on the current population, same index and limits "
                              f"(compatible runs: {sorted(r['run_id'] for r in current) or 'none'})"),
        ("dense decided (run or explicit gap)", "H" in labels or "D" in labels,
         "D/H runs present" if {"D", "H"} & labels else "no current D/H run: record the owner's D3 decision"),
        ("reranker decided (gate measured or explicit gap)", "HR" in labels,
         "current-policy trial recorded" if "HR" in labels else "no current trial: record the owner's D4 decision"),
        ("selection activated under the current policy", active_ok,
         (f"active run {a['run_id']} under policy {a.get('eval_version')} (current {EVAL_VERSION})"
          + (f"; {'; '.join(active_errors)}" if active_errors else "")) if a else "no activate-run decision"),
    ]
    return gates, a


# ================================================================ phase 4: reviewed gold (schema gold-2)
# Rows of `dev` (datasets/dev.jsonl) and the sealed `test` (sealed/test.jsonl). Labels point at stable source
# evidence (source hash, extraction revision, element, raw offsets/cells, exact quote), never at chunk IDs, so a
# rechunked index is scored against the same ground truth.

GOLD_SCHEMA = "gold-2"
# corpus: whole-corpus needle questions; its rows belong to dev families (split "dev") and are never sealed.
GOLD_DATASETS = ("dev", "test", "corpus")


def split_of(dataset: str) -> str:
    """The family split a gold dataset's rows must belong to."""
    return "test" if dataset in SEALED_SPLITS else "dev"
GOLD_TYPE_TARGETS = {  # per split; the combined target is twice this (120 rows)
    "direct_fact": 12, "semantic_paraphrase": 9, "exact_identifier": 6, "table_numeric": 9, "multi_passage": 6,
    "cross_document": 6, "missing_false_premise": 6, "revision_conflict": 6,
}
METADATA_STRATUM = "metadata_direct"  # answered from typed CSV metadata; never in passage denominators
GOLD_TYPES = set(GOLD_TYPE_TARGETS) | {METADATA_STRATUM}
EXPECTED_STATUS = {"answerable": ("answered",), "unanswerable": ("insufficient_evidence", "clarification_required"),
                   "ambiguous": ("clarification_required",), "conflicting": ("conflicting_evidence",)}
CLAIM_MATCH_TYPES = ("number", "date", "text")
CRITICAL_KINDS = ("deadline", "amount", "mandatory_condition", "institution")
METADATA_FIELDS = ("title", "institution", "notice", "revision", "amount_krw", "published_at", "bid_start",
                   "bid_close")
METADATA_STATES = ("known", "unknown", "conflict", "zero_review", "resolved")
QID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
LEAK_SIMILARITY = 0.85  # character-bigram Jaccard at or above which two questions count as one paraphrase
EARLY_BEFORE, LATE_FROM_POSITION = 0.3, 0.7


def norm_text(text) -> str:
    """NFC without any whitespace: the comparison form for quotes, patterns and qualifiers."""
    return re.sub(r"\s+", "", nfc(str(text or "")))


_UNITS = {"조": 10 ** 12, "억": 10 ** 8, "천만": 10 ** 7, "백만": 10 ** 6, "십만": 10 ** 5, "만": 10 ** 4, "천": 10 ** 3}
_NUMBER_PART = re.compile(r"(?<![\d.,])(\d[\d,]*(?:\.\d+)?)\s*(조|억|천만|백만|십만|만|천)?")
_DATE_RE = re.compile(
    r"(20\d{2})\s*(?:[.\-/]|년)\s*(\d{1,2})\s*(?:[.\-/]|월)\s*(\d{1,2})\s*일?\.?(?:\s*\([^)]{0,4}\))?"
    r"(?:\s*(오전|오후)?\s*(\d{1,2})\s*(?::|시)\s*(\d{2})?\s*분?)?")


def number_spans(text: str) -> list[tuple[Decimal, int, int]]:
    """(value, start, end) of every amount or count as written in Korean RFPs: `130,000,000원`, `1억 3천만 원`,
    `130백만원`, `1.3억`, `12개월`. A unit-bearing part followed by a smaller part (`1억 3천만`) is one amount."""
    out: list[tuple[Decimal, int, int]] = []
    prev: tuple[int, int] | None = None  # unit magnitude and end of the previous part
    t = nfc(text or "")
    for m in _NUMBER_PART.finditer(t):
        try:
            base = Decimal(m.group(1).rstrip(",").replace(",", ""))
        except InvalidOperation:
            prev = None
            continue
        mag = _UNITS.get(m.group(2) or "", 1)
        value = base * mag
        if prev and out and prev[0] > 1 and mag < prev[0] and not t[prev[1]:m.start()].strip():
            out[-1] = (out[-1][0] + value, out[-1][1], m.end())
        else:
            out.append((value, m.start(), m.end()))
        prev = (mag, m.end())
    return out


def _plain(v: Decimal) -> Decimal:
    return Decimal(int(v)) if v == v.to_integral_value() else v.normalize()


def extract_numbers(text: str) -> set[Decimal]:
    return {_plain(v) for v, _, _ in number_spans(text)}


def extract_dates(text: str) -> set[str]:
    """`2024. 6. 11.(화) 17:00`, `2024-06-11`, `2024년 6월 11일 오후 5시` -> {'2024-06-11', '2024-06-11T17:00'}."""
    out: set[str] = set()
    for m in _DATE_RE.finditer(nfc(text or "")):
        try:
            day = date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            continue
        out.add(day)
        if m.group(5):
            hour = int(m.group(5)) + (12 if m.group(4) == "오후" and int(m.group(5)) < 12 else 0)
            minute = int(m.group(6) or 0)
            if hour < 24 and minute < 60:
                out.add(f"{day}T{hour:02d}:{minute:02d}")
    return out


def _unit_marker(unit) -> str:
    return "원" if str(unit or "").strip().upper() in ("KRW", "원") else str(unit or "").strip()


def stated_numbers(text: str, unit) -> set[Decimal]:
    """Numbers written with the given unit right after them (`12개월`, `1억 3천만 원`); a bare or differently
    united number is not a statement of this quantity."""
    marker, t = _unit_marker(unit), nfc(text or "")
    if not marker:
        return set()
    return {_plain(v) for v, _, end in number_spans(t) if t[end:end + len(marker) + 1].lstrip().startswith(marker)}


def _expected(claim: dict) -> list:
    match = claim.get("match") or {}
    return [v for v in [match.get("value")] + list(claim.get("alternatives") or []) if v not in (None, "")]


def claim_value_found(claim: dict, text: str) -> bool:
    """The claim's typed value (or a permitted alternative) is stated in `text`: a number with its unit, a date
    with its required time, or a text pattern."""
    match = claim.get("match") or {}
    kind = match.get("type")
    if kind == "number":
        return bool({_plain(Decimal(str(v))) for v in _expected(claim)} & stated_numbers(text, match.get("unit")))
    if kind == "date":
        found = extract_dates(text)
        return any(_date_key(match, v) in found for v in _expected(claim))
    patterns = list(match.get("patterns") or []) + [v for v in _expected(claim)[1:] if isinstance(v, str)]
    hay = norm_text(text)
    return any(norm_text(p) and norm_text(p) in hay for p in patterns)


def _date_key(match: dict, value: str) -> str:
    """The form a date value must be stated in: with the claim's time when the claim has one."""
    return f"{value}T{match['time']}" if match.get("time") and "T" not in str(value) else str(value)


def typed_verdict(claim: dict, text: str) -> str:
    """One claim against the text an answer states about the claim's own document.

    correct: the value is stated (with its unit/time) and nothing contradicting it is; contested: the value and a
    different value of the same kind are both stated, so the answer is not credited until a reviewer decides;
    wrong_value: only other values of the same kind are stated; incomplete_qualifier: the value is stated without a
    required qualifier (or a deadline without its time); missing: nothing of this kind is stated; needs_review: a
    text claim whose patterns do not appear (a paraphrase may still be right)."""
    match = claim.get("match") or {}
    kind = match.get("type")
    if kind == "text":
        if claim_value_found(claim, text):
            return "incomplete_qualifier" if qualifiers_found(claim, text) else "correct"
        return "needs_review" if (text or "").strip() else "missing"
    if kind == "number":
        expected = {_plain(Decimal(str(v))) for v in _expected(claim)}
        stated = stated_numbers(text, match.get("unit"))
        if stated & expected:
            if stated - expected:
                return "contested"
            return "incomplete_qualifier" if qualifiers_found(claim, text) else "correct"
        return "wrong_value" if stated else "missing"
    found = extract_dates(text)
    stated_dt = {d for d in found if "T" in d}
    stated_d = {d for d in found if "T" not in d}
    want_d = {str(v).split("T")[0] for v in _expected(claim)}
    if match.get("time"):
        want_dt = {_date_key(match, v) for v in _expected(claim)}
        if stated_dt & want_dt:
            if (stated_dt - want_dt) or (stated_d - want_d):
                return "contested"
            return "incomplete_qualifier" if qualifiers_found(claim, text) else "correct"
        if stated_dt:  # a stated cutoff on another day or at another time
            return "wrong_value"
        if stated_d & want_d:
            return "contested" if stated_d - want_d else "incomplete_qualifier"  # the required time is missing
        return "wrong_value" if stated_d else "missing"
    if stated_d & want_d:
        if stated_d - want_d:
            return "contested"
        return "incomplete_qualifier" if qualifiers_found(claim, text) else "correct"
    return "wrong_value" if stated_d else "missing"


_NEGATION_AFTER = ("않", "안됨", "아니", "제외", "불포함", "미포함", "별도", "없")
_NEGATION_BEFORE = ("미", "불", "비")


def _affirmed(spelling: str, hay: str) -> bool:
    """`spelling` occurs in `hay` at least once without a negation right after it (`부가가치세를 포함하지 않은`,
    `부가세 별도`) or a negating prefix before it (`미포함`)."""
    s = norm_text(spelling)
    start = hay.find(s) if s else -1
    while start >= 0:
        after = hay[start + len(s):start + len(s) + 6]
        negated = any(n in after for n in _NEGATION_AFTER) or hay[max(0, start - 1):start] in _NEGATION_BEFORE
        if not negated:
            return True
        start = hay.find(s, start + 1)
    return False


def qualifiers_found(claim: dict, text: str) -> list[list[str]]:
    """Qualifier groups (each a list of permitted spellings) that `text` does not affirm; a negated mention does not
    state the qualifier."""
    hay = norm_text(text)
    return [q for q in claim.get("qualifiers") or [] if not any(_affirmed(x, hay) for x in q)]


def _bigrams(text: str) -> set[str]:
    t = norm_text(text)
    return {t[i:i + 2] for i in range(len(t) - 1)} or {t}


def question_similarity(a: str, b: str) -> float:
    x, y = _bigrams(a), _bigrams(b)
    return len(x & y) / len(x | y) if x | y else 1.0


def load_families(settings: Settings) -> dict:
    path = settings.data_dir / "datasets" / "families.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"families": {}}


class GoldChecker:
    """Checks one gold-2 row against the managed originals and the family map. Shared by `gold submit` (no review
    yet) and `validate-gold` (independent review required). Messages name IDs, never question text or quotes, so
    a sealed validation report can be shown without revealing the test set."""

    def __init__(self, settings: Settings, conn) -> None:
        self.families = load_families(settings)["families"]
        self.fam_of_doc = {d: (k, f) for k, f in self.families.items() for d in f["doc_ids"]}
        self.conn = conn
        self.docs = {r["doc_id"]: dict(r) for r in conn.execute(
            "SELECT d.doc_id, d.active_source_hash, s.active_extraction_id, s.parse_status "
            "FROM documents d JOIN sources s ON s.source_hash = d.active_source_hash")}
        self._last_order: dict[str, int] = {}

    def element(self, extraction_id: str | None, element_id: str | None):
        return self.conn.execute("SELECT raw_text, table_json, source_order FROM elements WHERE extraction_id = ? "
                                 "AND element_id = ?", (extraction_id, element_id)).fetchone()

    def position(self, extraction_id: str, source_order: int) -> float:
        if extraction_id not in self._last_order:
            self._last_order[extraction_id] = self.conn.execute(
                "SELECT COALESCE(MAX(source_order), 0) FROM elements WHERE extraction_id = ?",
                (extraction_id,)).fetchone()[0]
        return source_order / max(1, self._last_order[extraction_id])

    def row_position(self, row: dict) -> str | None:
        """early / middle / late by the first evidence alternative's place in its extraction."""
        for g in row.get("evidence_groups") or []:
            for alt in g.get("alternatives") or []:
                el = self.element(alt.get("extraction_id"), alt.get("element_id"))
                if el is not None:
                    p = self.position(alt["extraction_id"], el["source_order"])
                    return "early" if p < EARLY_BEFORE else "late" if p >= LATE_FROM_POSITION else "middle"
        return None

    def family_leaks(self) -> list[str]:
        """Source hashes that sit in both dev and test families: byte-identical or related originals split apart."""
        splits: dict[str, set[str]] = {}
        for f in self.families.values():
            hashes = {f.get("source_hash"), *f.get("related_sources", [])}
            hashes |= {self.docs[d]["active_source_hash"] for d in f["doc_ids"] if d in self.docs}
            for h in hashes - {None}:
                splits.setdefault(h, set()).add(f["split"])
        return sorted(h for h, s in splits.items() if len(s) > 1)

    def _check_alternative(self, alt: dict, scoped: dict, tag: str) -> list[str]:
        if "chunk_id" in alt or not alt.get("element_id"):
            return [f"{tag}: evidence must name a source element; chunk-only labels change with every rechunk"]
        e = []
        if alt.get("source_hash") != scoped.get("source_hash"):
            e.append(f"{tag}: evidence source hash is not the scoped original's")
        if alt.get("extraction_id") != scoped.get("extraction_id"):
            e.append(f"{tag}: evidence extraction revision is not the scoped one")
        el = self.element(alt.get("extraction_id"), alt.get("element_id"))
        if el is None:
            return e + [f"{tag}: evidence element not in extraction"]
        quote = str(alt.get("quote") or "")
        cells = json.loads(el["table_json"] or '{"cells": []}')["cells"]
        haystacks = [el["raw_text"]] + [c["text"] for c in cells]
        if not quote.strip() or not any(_ws(quote) in _ws(h) for h in haystacks):
            e.append(f"{tag}: quote not found in the original extraction element")
        offsets = alt.get("offsets")
        if offsets is not None:
            ok = (isinstance(offsets, list) and len(offsets) == 2 and all(isinstance(x, int) for x in offsets)
                  and 0 <= offsets[0] < offsets[1] <= len(el["raw_text"]))
            if not ok or _ws(quote) not in _ws(el["raw_text"][offsets[0]:offsets[1]]):
                e.append(f"{tag}: raw offsets do not hold the quote")
        wanted = alt.get("cells")
        if wanted is not None:
            by_pos = {(c["row"], c["col"]): c["text"] for c in cells}
            texts = [by_pos.get(tuple(rc)) if isinstance(rc, list) and len(rc) == 2 else None for rc in wanted]
            if not wanted or None in texts:
                e.append(f"{tag}: cell coordinates are not in the table")
            elif _ws(quote) not in _ws(" ".join(texts)) and not all(_ws(t) in _ws(quote) for t in texts):
                e.append(f"{tag}: the named cells do not hold the quote")
        return e

    def _check_claim(self, c: dict, groups: dict, tag: str, seen: set) -> list[str]:
        cid = c.get("claim_id")
        tag = f"{tag} claim {cid}"
        e = []
        if not cid or cid in seen:
            e.append(f"{tag}: claim_id missing or repeated")
        seen.add(cid)
        match = c.get("match") or {}
        kind = match.get("type")
        if kind not in CLAIM_MATCH_TYPES:
            return e + [f"{tag}: match.type must be one of {CLAIM_MATCH_TYPES}"]
        if kind == "number" and (not isinstance(match.get("value"), int) or isinstance(match.get("value"), bool)
                                 or not str(match.get("unit") or "").strip()):
            e.append(f"{tag}: a number claim needs an integer value and its unit")
        if kind == "date":
            try:
                date.fromisoformat(str(match.get("value")))
            except ValueError:
                e.append(f"{tag}: a date claim needs an ISO date value")
            if match.get("time") is not None and not re.fullmatch(r"\d{2}:\d{2}", str(match["time"])):
                e.append(f"{tag}: time must be HH:MM")
        if kind == "text" and not [p for p in match.get("patterns") or [] if str(p).strip()]:
            e.append(f"{tag}: a text claim needs matching patterns")
        if c.get("criticality") not in ("critical", "normal"):
            e.append(f"{tag}: criticality must be critical or normal")
        elif (c["criticality"] == "critical") != (c.get("critical_kind") in CRITICAL_KINDS):
            e.append(f"{tag}: a critical claim names its kind {CRITICAL_KINDS}; a normal claim names none")
        quals = c.get("qualifiers") or []
        if not isinstance(quals, list) or not all(isinstance(q, list) and q and all(str(x).strip() for x in q)
                                                  for q in quals):
            e.append(f"{tag}: qualifiers are lists of permitted spellings")
            quals = []
        support = c.get("support_groups") or []
        if not support or any(g not in groups for g in support):
            return e + [f"{tag}: support_groups must name this row's evidence groups"]
        quotes = " ".join(a.get("quote", "") for g in support for a in groups[g].get("alternatives") or [])
        if kind in ("number", "date") and not claim_value_found({"match": match}, quotes):
            e.append(f"{tag}: the {kind} value is not stated in its support quotes")
        if kind == "text" and not claim_value_found({"match": match}, quotes):
            e.append(f"{tag}: no pattern appears in its support quotes")
        if qualifiers_found({"qualifiers": quals}, quotes):
            e.append(f"{tag}: a qualifier is not stated in its support quotes (add its evidence group)")
        return e

    def check(self, row: dict, tag: str, *, split: str | None = None, require_review: bool = True) -> list[str]:
        e = self._check_identity(row, tag, split)
        if row.get("operational_case") or row.get("question_type") in OPERATIONAL_TYPES:
            return e + [f"{tag}: converter failures, interrupted calls and ownership races belong to the operational "
                        "suite, not to gold"]
        e += self._check_kind(row, tag)
        scope = row.get("scope")
        if not isinstance(scope, list) or not 1 <= len(scope) <= 2 or not all(isinstance(s, dict) for s in scope):
            return e + [f"{tag}: scope names one or two documents"]
        scoped = self._check_scope(row, tag, scope, e)
        groups = row.get("evidence_groups") or []
        by_id = self._check_groups(groups, scoped, tag, e)
        e += self._check_content(row, tag, scoped, groups, by_id)
        return e + self._check_review(row, tag, require_review)

    def _check_identity(self, row: dict, tag: str, split: str | None) -> list[str]:
        e: list[str] = []
        if row.get("dataset_version") != GOLD_SCHEMA:
            e.append(f"{tag}: dataset_version must be {GOLD_SCHEMA!r}")
        if not QID_RE.fullmatch(str(row.get("question_id") or "")):
            e.append(f"{tag}: question_id uses lowercase letters, digits and hyphens")
        rev = row.get("revision")
        if not isinstance(rev, int) or isinstance(rev, bool) or rev < 1:
            e.append(f"{tag}: revision must be an integer >= 1")
        if row.get("split") not in GOLD_DATASETS:
            e.append(f"{tag}: split must be dev or test")
        elif split and row["split"] != split:
            e.append(f"{tag}: a {row['split']} row cannot enter dataset {split}")
        return e

    def _check_kind(self, row: dict, tag: str) -> list[str]:
        """Question type, text, mode, date and the answerability/status pair."""
        e: list[str] = []
        qtype, mode, answerability = row.get("question_type"), row.get("mode"), row.get("answerability")
        if qtype not in GOLD_TYPES:
            e.append(f"{tag}: unknown question_type {qtype!r}")
        for key in ("question", "difficulty_reason"):
            if not str(row.get(key) or "").strip():
                e.append(f"{tag}: {key} is empty")
        if mode not in ("single", "compare", "metadata"):
            e.append(f"{tag}: mode must be single, compare or metadata")
        try:
            date.fromisoformat(str(row.get("as_of_date")))
        except ValueError:
            e.append(f"{tag}: as_of_date must be an ISO date")
        if answerability not in EXPECTED_STATUS:
            e.append(f"{tag}: answerability must be one of {sorted(EXPECTED_STATUS)}")
        elif row.get("expected_status") not in EXPECTED_STATUS[answerability]:
            e.append(f"{tag}: expected_status for {answerability} must be one of {EXPECTED_STATUS[answerability]}")
        return e

    def _check_scope(self, row: dict, tag: str, scope: list[dict], e: list[str]) -> dict[str, dict]:
        """The scoped documents by ID, checked against the managed revisions and their families; problems go to `e`."""
        qtype, mode, answerability = row.get("question_type"), row.get("mode"), row.get("answerability")
        if len({s.get("doc_id") for s in scope}) != len(scope):
            e.append(f"{tag}: the same document is scoped twice")
        if (mode == "compare") != (len(scope) == 2):
            e.append(f"{tag}: compare mode scopes exactly two documents; other modes one")
        if (qtype == "cross_document") != (mode == "compare"):
            e.append(f"{tag}: cross_document questions and compare mode go together")
        if (qtype == METADATA_STRATUM) != (mode == "metadata"):
            e.append(f"{tag}: metadata_direct questions and metadata mode go together")
        scoped: dict[str, dict] = {}
        for s in scope:
            doc = self.docs.get(s.get("doc_id"))
            if doc is None:
                e.append(f"{tag}: unknown doc_id")
                continue
            short = s["doc_id"][:8]
            if s.get("source_hash") != doc["active_source_hash"]:
                e.append(f"{tag}: {short}: source revision does not match the managed original")
            if mode != "metadata":
                if doc["parse_status"] != "parsed":
                    e.append(f"{tag}: {short} is {doc['parse_status']}: " + (
                        "a source failure is not source absence; move this case to the operational suite"
                        if answerability != "answerable" else "its original text is unavailable"))
                elif s.get("extraction_id") != doc["active_extraction_id"]:
                    e.append(f"{tag}: {short}: extraction revision is not the active one")
            scoped[s["doc_id"]] = s
        fams = set()
        for d in scoped:
            fam = self.fam_of_doc.get(d)
            if fam is None:
                e.append(f"{tag}: {d[:8]} has no assigned family")
                continue
            fams.add(fam[0])
            if fam[1]["split"] != row.get("split"):
                e.append(f"{tag}: {d[:8]} belongs to a {fam[1]['split']} family")
        if sorted(set(row.get("family_ids") or [])) != sorted(fams):
            e.append(f"{tag}: family_ids must be exactly the scoped documents' assigned families")
        return scoped

    def _check_groups(self, groups: list[dict], scoped: dict[str, dict], tag: str, e: list[str]) -> dict[str, dict]:
        """Evidence groups by ID, each source span checked; problems go to `e`."""
        by_id: dict[str, dict] = {}
        for g in groups:
            gid = g.get("group_id")
            if not gid or gid in by_id:
                e.append(f"{tag}: evidence group id missing or repeated")
                continue
            by_id[gid] = g
            if g.get("doc_id") not in scoped:
                e.append(f"{tag} {gid}: evidence group outside the scope")
                continue
            alts = g.get("alternatives") or []
            if not alts:
                e.append(f"{tag} {gid}: an evidence group needs at least one source span")
            for i, alt in enumerate(alts, 1):
                e += self._check_alternative(alt, scoped[g["doc_id"]], f"{tag} {gid}#{i}")
        return by_id

    def _check_content(self, row: dict, tag: str, scoped: dict[str, dict], groups: list[dict],
                       by_id: dict[str, dict]) -> list[str]:
        """Evidence and claims an answerable row needs, metadata expectations, and negative validation."""
        e: list[str] = []
        mode, answerability = row.get("mode"), row.get("answerability")
        claims = row.get("required_claims") or []
        if mode != "metadata":
            if answerability == "answerable" and not groups:
                e.append(f"{tag}: an answerable passage question needs evidence groups")
            if answerability == "answerable" and not claims:
                e.append(f"{tag}: an answerable passage question needs required claims")
            if mode == "compare" and answerability == "answerable" and set(scoped) - {g.get("doc_id") for g in groups}:
                e.append(f"{tag}: every compared document needs at least one evidence group")
            if answerability == "conflicting" and len(by_id) < 2:
                e.append(f"{tag}: a conflict needs one evidence group per competing value")
        seen: set = set()
        for c in claims:
            e += self._check_claim(c, by_id, tag, seen)
        if mode == "metadata":
            fields = row.get("metadata_fields") or []
            states = row.get("expected_states") or {}
            if not fields or set(fields) - set(METADATA_FIELDS):
                e.append(f"{tag}: metadata_fields must name CSV fields {METADATA_FIELDS}")
            if set(states) != set(fields) or set(states.values()) - set(METADATA_STATES):
                e.append(f"{tag}: expected_states gives one of {METADATA_STATES} for every metadata field")
        if answerability in ("unanswerable", "ambiguous") and mode != "metadata":
            nv = row.get("negative_validation") or {}
            if not nv:
                e.append(f"{tag}: a negative or ambiguous case needs negative_validation")
            else:
                if set(scoped) - set(nv.get("scope_searched") or []):
                    e.append(f"{tag}: negative_validation must search every scoped document")
                if not nv.get("methods"):
                    e.append(f"{tag}: negative_validation must record how absence was checked")
                if nv.get("original_complete") is not True:
                    e.append(f"{tag}: absence needs an original complete enough to establish it")
                if not str(nv.get("rationale") or "").strip():
                    e.append(f"{tag}: negative_validation needs a rationale")
                if answerability == "unanswerable" and not nv.get("locations"):
                    e.append(f"{tag}: verified absence lists the original locations inspected")
        return e

    def _check_review(self, row: dict, tag: str, require_review: bool) -> list[str]:
        """Drafting provenance, and for gold an independent review against the original."""
        e: list[str] = []
        review = row.get("review") or {}
        prov = row.get("generation_provenance") or {}
        drafter = str(review.get("drafted_by") or "").strip()
        if not drafter:
            e.append(f"{tag}: review.drafted_by is required")
        if prov.get("method") not in ("human", "llm"):
            e.append(f"{tag}: generation_provenance.method must be human or llm")
        elif prov["method"] == "llm" and not all(prov.get(k) for k in ("model", "prompt_version", "source_set_hash")):
            e.append(f"{tag}: an LLM draft records its model, prompt version and source-set hash")
        if not require_review:
            if review.get("reviewed_by") or review.get("approved_at"):
                e.append(f"{tag}: drafts cannot carry a review")
            return e
        reviewer = str(review.get("reviewed_by") or "").strip()
        if review.get("status") != "approved" or not reviewer or not review.get("approved_at"):
            e.append(f"{tag}: pending or unreviewed rows are not gold")
        elif reviewer == drafter or (prov.get("method") == "llm" and reviewer == prov.get("model")):
            e.append(f"{tag}: needs an independent reviewer; a drafter (or drafting model) cannot approve itself")
        elif review.get("original_inspected") is not True:
            e.append(f"{tag}: the reviewer must have inspected the original")
        if review.get("disputed") or review.get("second_review"):
            second = review.get("second_review") or {}
            if (not second or second.get("reviewer") in (None, "", drafter, reviewer)
                    or (prov.get("method") == "llm" and second.get("reviewer") == prov.get("model"))):
                e.append(f"{tag}: a disputed row needs an independent second review")
            elif second.get("agreed") is not True:
                e.append(f"{tag}: the second reviewer disagreed; correct the row as a new revision")
        return e


def _gold_split_rows(settings: Settings, name: str) -> list[dict]:
    path = dataset_path(settings, name)
    return read_jsonl(path) if path.exists() else []


def _split_leak(row: dict, tag: str, other_name: str, other: list[dict]) -> list[str]:
    """The first row of the other split this row repeats or paraphrases, without naming a sealed row."""
    for o in other:
        if question_similarity(row.get("question", ""), o.get("question", "")) >= LEAK_SIMILARITY:
            which = "a sealed row (ID withheld)" if other_name in SEALED_SPLITS else f"row {o.get('question_id')}"
            return [f"{tag}: repeats or paraphrases {other_name} {which}: leakage"]
    return []


def validate_gold_v2(settings: Settings, name: str) -> dict:
    """Accepted gold only: independently reviewed against the original, quotes present in the pinned extraction,
    families inside one split, no question repeated or paraphrased across splits. The report counts types against
    the phase-4 targets and source positions; a smaller valid set is labeled `pilot`, never `gold`."""
    if name not in GOLD_DATASETS:
        raise EvaluationError(f"{name} is not a gold-2 dataset")
    path = dataset_path(settings, name)
    if not path.exists():
        return {"ok": False, "dataset": name, "errors": [f"dataset file missing: {path.name}"], "rows": 0}
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return {"ok": False, "dataset": name, "errors": ["dataset must be UTF-8 without BOM"], "rows": 0}
    rows = read_jsonl(path)
    other_name = "dev" if name == "test" else "test"
    other = _gold_split_rows(settings, other_name)
    errors: list[str] = []
    rejected: dict[str, list[str]] = {}
    positions = {"early": 0, "middle": 0, "late": 0, "none": 0}
    with open_db(settings.db_path) as conn:
        checker = GoldChecker(settings, conn)
        leaks = checker.family_leaks()
        errors += [f"source {h[:12]} sits in both dev and test families: split leakage" for h in leaks]
        affected = {notice_fam for r in load_families(settings).get("related_across_splits", [])
                    for notice_fam in r["families"]}
        seen_ids, seen_q = set(), {}
        for i, row in enumerate(rows, start=1):
            qid = str(row.get("question_id") or f"row-{i}")
            tag = qid
            errs = checker.check(row, tag, split=split_of(name))
            if qid in seen_ids:
                errs.append(f"{tag}: question_id repeated (keep only the latest revision)")
            seen_ids.add(qid)
            key = norm_text(row.get("question"))
            if key in seen_q:
                errs.append(f"{tag}: same question as {seen_q[key]}")
            seen_q.setdefault(key, qid)
            if set(row.get("family_ids") or []) & affected:
                errs.append(f"{tag}: its family has related revisions in the other split; fix the family map")
            errs += _split_leak(row, tag, other_name, other)
            if errs:
                rejected[qid] = errs
                errors += errs
            if row.get("mode") != "metadata":
                positions[checker.row_position(row) or "none"] += 1
    types = {t: sum(r.get("question_type") == t for r in rows) for t in sorted(GOLD_TYPES)}
    targets = {t: {"rows": types[t], "target": n, "met": types[t] >= n} for t, n in GOLD_TYPE_TARGETS.items()}
    ok = not errors
    report = {"ok": ok, "dataset": name, "schema": GOLD_SCHEMA, "rows": len(rows),
              "dataset_sha256": hashlib.sha256(raw).hexdigest(),
              "label": "gold" if ok and all(t["met"] for t in targets.values()) else "pilot",
              "targets": targets, "metadata_stratum": types[METADATA_STRATUM],
              "answerability": {a: sum(r.get("answerability") == a for r in rows) for a in EXPECTED_STATUS},
              "source_positions": positions, "rejected_rows": [{"question_id": k, "errors": v}
                                                               for k, v in rejected.items()],
              "errors": errors, "validated_at": utcnow()}
    write_text_atomic(path.with_suffix(".validation.json"), json.dumps(report, ensure_ascii=False, indent=1))
    return report


def gold_manifest_path(settings: Settings, name: str) -> Path:
    return dataset_path(settings, name).with_name(f"{name}-manifest.json")


def review_log_path(settings: Settings, name: str) -> Path:
    return dataset_path(settings, name).with_name("review-log.jsonl" if name == "dev" else f"{name}-review-log.jsonl")


def _review_log_text(settings: Settings, name: str) -> str:
    with open_db(settings.db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT r.review_id, r.candidate_id, r.reviewer, r.kind, r.decision, r.original_inspected, r.note, "
            "r.created_at FROM gold_reviews r JOIN gold_candidates c ON c.candidate_id = r.candidate_id "
            "WHERE c.dataset = ? ORDER BY r.created_at, r.review_id", (name,))]
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


def export_review_log(settings: Settings, name: str) -> tuple[Path, str]:
    """The append-only review decisions behind a dataset (first decisions and second reviews), oldest first,
    rendered from the database."""
    text = _review_log_text(settings, name)
    path = review_log_path(settings, name)
    write_text_atomic(path, text)
    return path, hashlib.sha256(text.encode("utf-8")).hexdigest()


def _families_sha(settings: Settings) -> str | None:
    fam_path = settings.data_dir / "datasets" / "families.json"
    return hashlib.sha256(fam_path.read_bytes()).hexdigest() if fam_path.exists() else None


def freeze_dataset(settings: Settings, name: str, actor: str, reason: str) -> dict:
    """Freezes a validated gold split: dataset, review log and family map hashes in its manifest, with an audit
    event. The test manifest stays under sealed/."""
    if not (reason or "").strip():
        raise EvaluationError("freezing a dataset needs a reason")
    report = validate_gold_v2(settings, name)
    if not report["ok"]:
        raise EvaluationError(f"{name} does not validate ({len(report['errors'])} errors); freeze refused")
    log_path, log_sha = export_review_log(settings, name)
    manifest = {"dataset": name, "schema": GOLD_SCHEMA, "dataset_sha256": report["dataset_sha256"],
                "rows": report["rows"], "label": report["label"], "targets": report["targets"],
                "metadata_stratum": report["metadata_stratum"], "answerability": report["answerability"],
                "source_positions": report["source_positions"], "review_log_sha256": log_sha,
                "families_sha256": _families_sha(settings),
                "frozen_by": actor, "reason": reason.strip(), "frozen_at": utcnow()}
    write_text_atomic(gold_manifest_path(settings, name), json.dumps(manifest, ensure_ascii=False, indent=1))
    record_audit(settings, actor, "freeze_dataset", name, reason, {k: manifest[k] for k in (
        "dataset_sha256", "rows", "label", "review_log_sha256")})
    return manifest


def frozen_dataset(settings: Settings, name: str) -> dict | None:
    """The frozen manifest with `current`: today's dataset bytes, family map, review log file and the review history
    in the database are all still the frozen ones. `changed` names whatever differs."""
    path = gold_manifest_path(settings, name)
    if not path.exists():
        return None
    manifest = json.loads(path.read_text(encoding="utf-8"))
    data, log = dataset_path(settings, name), review_log_path(settings, name)
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None  # noqa: E731
    changed = []
    if sha(data) != manifest["dataset_sha256"]:
        changed.append("dataset")
    if _families_sha(settings) != manifest.get("families_sha256"):
        changed.append("family map")
    if sha(log) != manifest.get("review_log_sha256"):
        changed.append("review log file")
    if hashlib.sha256(_review_log_text(settings, name).encode("utf-8")).hexdigest() != manifest.get("review_log_sha256"):
        changed.append("review history in the database")
    manifest["changed"] = changed
    manifest["current"] = not changed
    return manifest


def record_audit(settings: Settings, actor: str, action: str, target: str, reason: str, details: dict) -> str:
    from ..storage.store import tx

    event_id = str(uuid.uuid4())
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        conn.execute("INSERT INTO audit_events(event_id, actor, action, target, reason, details_json, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (event_id, actor, action, target, reason, dumps(details),
                                                       utcnow()))
    return event_id
