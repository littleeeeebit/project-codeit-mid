"""Development-pilot dataset validation and source-family assignment."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from pathlib import Path

from .ingestion import CODE_RE, nfc
from .settings import Settings
from .store import dumps, open_db, read_jsonl, utcnow, write_text_atomic

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


# ================================================================ phase 2: frozen retrieval comparison

EVAL_VERSION = "retrieval-eval-1"
RANK_DEPTH = 20
CRITICAL_CODE_TYPES = {"repeated_code", "requirement_detail"}
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


def load_eval_rows(settings: Settings, name: str) -> tuple[list[dict], list[dict], str]:
    """(scored rows, skipped rows with reasons, dataset sha256). Only independently reviewed dev rows whose
    evidence is pinned to the document's active extraction are scored; the rest are listed, not dropped."""
    path = dataset_path(settings, name)
    if not path.exists():
        raise EvaluationError(f"dataset file missing: {path.name}")
    raw = path.read_bytes()
    rows = read_jsonl(path)
    with open_db(settings.db_path) as conn:
        active = {r["doc_id"]: (r["active_source_hash"], r["active_extraction_id"]) for r in conn.execute(
            "SELECT d.doc_id, d.active_source_hash, s.active_extraction_id FROM documents d "
            "JOIN sources s ON s.source_hash = d.active_source_hash")}
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


def is_passage_row(row: dict) -> bool:
    return bool(row.get("answerable")) and bool(row.get("evidence")) and row.get("type") not in (
        METADATA_TYPES | OPERATIONAL_TYPES)


def _quote_range(text: str, quote: str) -> tuple[int, int] | None:
    words = nfc(quote).split()
    if not words:
        return None
    m = re.search(r"\s*".join(re.escape(w) for w in words), nfc(text))
    return (m.start(), m.end()) if m else None


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
    rng = _quote_range(text, quote)
    if rng:
        rows = {r for r, a, b in spans if a < rng[1] and rng[0] < b}
    return rows


def grade(chunk: dict, unit: dict, element: dict | None) -> int:
    """2: the chunk carries the whole quoted evidence; 1: it carries part of the same element around it;
    0: unrelated. Judged on source spans, so any chunking is scored against the same target."""
    if element is None:
        return 0
    best = 0
    for span in chunk["spans"]:
        if span["element_id"] != unit["element_id"]:
            continue
        if "rows" in span:
            rows = _quote_rows(element, unit["quote"])
            if rows and rows <= set(span["rows"]):
                return 2
            best = max(best, 1)
        else:
            rng = _quote_range(element["raw_text"], unit["quote"])
            if rng and span["start"] <= rng[0] and rng[1] <= span["end"]:
                return 2
            if rng is None or (span["start"] < rng[1] and rng[0] < span["end"]):
                best = max(best, 1)
    return best


def score_row(row: dict, ranking: list[dict], packed: list[dict], elements: dict) -> dict:
    """Ranked metrics for one passage row. Each gold unit is credited once, at the first rank where it reaches
    its grade: overlapping chunks repeating one fact earn nothing more. Ideal DCG places one complete unit per
    rank (capped at 1 when a single chunk carries several units)."""
    units = row["evidence"]
    x = row["extraction_id"]
    els = [elements.get((x, u["element_id"])) for u in units]
    credited = [0] * len(units)
    dcg, first_full = 0.0, None
    for rank, chunk in enumerate(ranking[:RANK_DEPTH], start=1):
        gain = 0
        for j, u in enumerate(units):
            g = grade(chunk, u, els[j])
            if g > credited[j]:
                if rank <= NDCG_AT:
                    gain += g - credited[j]
                credited[j] = g
            if g == 2 and first_full is None:
                first_full = rank
        dcg += gain / math.log2(rank + 1)
    at = lambda k: [max((grade(c, u, els[j]) for c in ranking[:k]), default=0)  # noqa: E731
                    for j, u in enumerate(units)]
    top5, top20 = at(5), at(RANK_DEPTH)
    packed_grades = [max((grade(c, u, els[j]) for c in packed), default=0) for j, u in enumerate(units)]
    n = len(units)
    return {
        "units": n, "hit@1": int(any(g == 2 for g in at(1))), "hit@5": int(any(g == 2 for g in top5)),
        "hit@20": int(any(g == 2 for g in top20)),
        "recall@20": round(sum(g == 2 for g in top20) / n, 4), "complete@20": int(all(g == 2 for g in top20)),
        "ndcg@5": round(min(1.0, dcg / sum(2 / math.log2(j + 1) for j in range(1, min(n, NDCG_AT) + 1))), 4), "mrr": round(1 / first_full, 4) if first_full else 0.0,
        "packed_recall": round(sum(g == 2 for g in packed_grades) / n, 4),
        "packed_complete": int(all(g == 2 for g in packed_grades)),
        "qualifier_loss": sum(g == 1 for g in packed_grades), "unit_grades@20": top20,
        "missing_units": [u["element_id"] for u, g in zip(units, top20) if g < 2],
    }


def code_check(row: dict, packed: list[dict]) -> dict | None:
    """Critical check: an explicit requirement code must bring its own block first."""
    codes = list(dict.fromkeys(CODE_RE.findall(nfc(row.get("question", "")))))
    if not codes or row.get("type") not in CRITICAL_CODE_TYPES:
        return None
    first = packed[0] if packed else None
    ok = bool(first and first.get("requirement_key") in codes)
    return {"codes": codes, "ok": ok, "first_key": first.get("requirement_key") if first else None}


def aggregate(results: list[dict], skipped: list[dict]) -> dict:
    passage = [r for r in results if r.get("metrics")]
    single = [r for r in passage if r["metrics"]["units"] == 1]
    multi = [r for r in passage if r["metrics"]["units"] > 1]

    def rate(rows: list[dict], key: str) -> dict:
        k = sum(r["metrics"][key] for r in rows)
        return {"numerator": k, "denominator": len(rows), "rate": round(k / len(rows), 4) if rows else None,
                "wilson95": wilson(k, len(rows))}

    def mean(rows: list[dict], key: str) -> float | None:
        return round(sum(r["metrics"][key] for r in rows) / len(rows), 4) if rows else None

    by_type: dict[str, dict] = {}
    for t in sorted({r["type"] for r in passage}):
        rows = [r for r in passage if r["type"] == t]
        by_type[t] = {"n": len(rows), "hit@20": rate(rows, "hit@20")["rate"], "ndcg@5": mean(rows, "ndcg@5"),
                      "packed_complete": rate(rows, "packed_complete")["rate"]}
    latencies = [r["timings_ms"]["total"] for r in results if r.get("timings_ms")]
    codes = [r["code_check"] for r in results if r.get("code_check")]
    from .dense import percentile

    return {
        "passage_rows": len(passage), "single_evidence": {"hit@20": rate(single, "hit@20"),
                                                          "hit@5": rate(single, "hit@5")},
        "multi_evidence": {"complete@20": rate(multi, "complete@20"), "recall@20": mean(multi, "recall@20")},
        "ndcg@5": mean(passage, "ndcg@5"), "mrr": mean(passage, "mrr"),
        "packed_complete": rate(passage, "packed_complete"),
        "qualifier_losses": sum(r["metrics"]["qualifier_loss"] for r in passage),
        "wrong_scope_candidates": sum(r.get("wrong_scope", 0) for r in results),
        "code_checks": {"ok": sum(c["ok"] for c in codes), "n": len(codes)},
        "critical_failures": [r["id"] for r in results if r.get("code_check") and not r["code_check"]["ok"]]
        + [r["id"] for r in results if r.get("wrong_scope")],
        "latency_ms": {"p50": percentile(latencies, 0.5), "p95": percentile(latencies, 0.95), "n": len(latencies)},
        "fallbacks": sorted({r["fallback"] for r in results if r.get("fallback")}),
        "by_type": by_type,
        "not_scored": {"non_passage_rows": sum(1 for r in results if not r.get("metrics")),
                       "types": sorted({r["type"] for r in results if not r.get("metrics")}),
                       "skipped": skipped},
    }


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


def _ready_dense_for(settings: Settings, index_version: str) -> str | None:
    with open_db(settings.db_path) as conn:
        for r in conn.execute("SELECT index_version, config_json FROM indexes WHERE state = 'ready' "
                              "ORDER BY created_at DESC"):
            cfg = json.loads(r["config_json"])
            if (cfg.get("kind") == "dense" and cfg.get("base_index_version") == index_version
                    and cfg.get("model") == settings.embedding_model
                    and cfg.get("dimensions") == settings.embedding_dimensions):
                return r["index_version"]
    return None


def _frozen_config(settings: Settings, label: str, dataset: str, dataset_sha: str, index, dense, analyzer,
                   extra: dict | None = None) -> dict:
    from .retrieval import RUN_MODES, WhitespaceAnalyzer, analyzer_fingerprint

    return {"eval_version": EVAL_VERSION, "label": label, "mode": RUN_MODES[label], "dataset": dataset,
            "dataset_sha256": dataset_sha, "index_version": index.version, "index_manifest_hash": index.manifest_hash,
            "profile": index.profile, "analyzer": WhitespaceAnalyzer.version if label == "K0"
            else analyzer_fingerprint(analyzer),
            "dense_version": dense.version if dense is not None and label in ("D", "H", "HR") else None,
            "embedding": {"model": settings.embedding_model, "dims": settings.embedding_dimensions}
            if label in ("D", "H", "HR") else None,
            "limits": {"channel_top_k": settings.channel_top_k, "fused_top_k": settings.fused_top_k,
                       "rrf_k": settings.rrf_k, "evidence_target_tokens": settings.evidence_target_tokens,
                       "evidence_max_tokens": settings.evidence_max_tokens,
                       "evidence_max_units": settings.evidence_max_units}, **(extra or {})}


def _execute(settings: Settings, index, analyzer, rows: list[dict], mode: str, dense=None, vectors=None,
             reranker=None, rerank_depth=None) -> list[dict]:
    from .contracts import DocRef
    from .retrieval import retrieve

    results = []
    for row in rows:
        out = {"id": row.get("id"), "type": row.get("type"), "question": row.get("question")}
        if not is_passage_row(row):
            results.append(out)  # metadata, operational and unanswerable rows: reported, not ranked
            continue
        scope = [(DocRef(row["doc_id"], row["source_hash"]), row["extraction_id"])]
        r = retrieve(settings, index, analyzer, row["question"], scope, mode=mode, dense=dense,
                     query_vector=(vectors or {}).get(row.get("id")), reranker=reranker, rerank_depth=rerank_depth)
        ranking = [index.chunks[index.row_of[c]] for c in r.ranking]
        packed = [index.chunks[index.row_of[e.chunk_id]] for e in r.evidence]
        out.update(
            metrics=score_row(row, ranking, packed, index.elements), code_check=code_check(row, packed),
            wrong_scope=sum(c["extraction_id"] != row["extraction_id"] for c in ranking),
            ranking=r.ranking[:RANK_DEPTH], packed=[e.chunk_id for e in r.evidence], fallback=r.fallback,
            candidates=r.candidates, limitations=r.limitations, timings_ms=r.timings_ms,
            evidence_tokens=r.evidence_tokens)
        results.append(out)
    return results


def _write_run(settings: Settings, run_id: str, config: dict, results: list[dict], scores: dict) -> None:
    d = _run_dir(settings, run_id)
    write_text_atomic(d / "config.json", json.dumps(config, ensure_ascii=False, indent=1))
    from .store import write_jsonl_atomic

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
              f"- qualifier losses in packed context: {agg['qualifier_losses']}",
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
    from . import dense as dense_mod
    from .retrieval import RUN_MODES, KeywordIndex, RetrievalError
    from .store import get_app_setting

    bad = [x for x in labels if x not in RUN_MODES or x == "HR"]
    if bad:
        raise EvaluationError(f"unknown or unsupported run labels {bad}; HR runs through trial-reranker")
    rows, skipped, dataset_sha = load_eval_rows(settings, dataset)
    if not rows:
        raise EvaluationError("no independently reviewed dev rows to evaluate")
    with open_db(settings.db_path) as conn:
        index_version = index_version or get_app_setting(conn, "active_index")
    try:
        index = KeywordIndex.load(settings, index_version)
    except RetrievalError as exc:
        raise EvaluationError(str(exc)) from None
    dense, dense_error = None, None
    if any(x in ("D", "H") for x in labels):
        version = _ready_dense_for(settings, index.version)
        if version is None:
            dense_error = "no ready dense matrix for this keyword index; run plan-embeddings and build-dense"
        else:
            try:
                dense = dense_mod.DenseIndex.load(settings, version, base=index)
            except dense_mod.DenseError as exc:
                dense_error = f"dense matrix failed verification: {exc}"
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
            if not is_passage_row(row) or not index.rows_by_extraction.get(row["extraction_id"]):
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
            vectors[row["id"]] = vec
            query_info["hits" if info["cache"] == "hit" else "paid"] += 1
            if info.get("attempt_id"):
                query_info["attempts"].append(info["attempt_id"])
                query_info["settled_micro_usd"] += info.get("settled_micro_usd") or 0
        if query_info["unavailable"]:
            dense_error = (f"{query_info['unavailable']} query vectors unavailable "
                           f"({sorted(set(r for r in query_info.get('reasons', []) if r))}); "
                           "pass --allow-paid-queries with paid mode enabled, or keep K1")
    stats = profile_stats(index)
    summaries = []
    for label in labels:
        config = _frozen_config(settings, label, dataset, dataset_sha, index, dense, analyzer)
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
                  "query_embedding": query_info if label in ("D", "H") else None, "created_at": utcnow()}
        _write_run(settings, run_id, config, results, scores)
        summaries.append({"run_id": run_id, "label": label, "reused": False, **_headline(scores)})
    return summaries


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


def _latest_run(settings: Settings, label: str, dataset_sha: str, index_version: str) -> str | None:
    base = settings.data_dir / "runs"
    found = []
    for d in base.glob(f"{label}-*") if base.exists() else []:
        try:
            config, scores = load_run(settings, d.name)
        except (EvaluationError, json.JSONDecodeError):
            continue
        if (config["dataset_sha256"] == dataset_sha and config["index_version"] == index_version
                and scores.get("status") == "complete"):
            found.append((scores.get("created_at", ""), d.name))
    return max(found)[1] if found else None


def trial_reranker(settings: Settings, analyzer, dataset: str, depths: list[int], reranker=None,
                   load_info: dict | None = None, index_version: str | None = None, users: int = LOAD_USERS) -> dict:
    """Local-only reranker trial on the frozen H candidate pool: quality at each depth, warm latency alone and
    under `users` concurrent queries, and the promotion gate. A load failure records the bypass."""
    from concurrent.futures import ThreadPoolExecutor

    from . import dense as dense_mod
    from .dense import percentile
    from .retrieval import KeywordIndex
    from .store import get_app_setting

    rows, skipped, dataset_sha = load_eval_rows(settings, dataset)
    with open_db(settings.db_path) as conn:
        index_version = index_version or get_app_setting(conn, "active_index")
    index = KeywordIndex.load(settings, index_version)
    h_run = _latest_run(settings, "H", dataset_sha, index.version)
    if h_run is None:
        raise EvaluationError("no complete frozen H run for this dataset and index; run evaluate-retrieval with H")
    h_config, h_scores = load_run(settings, h_run)
    h_traces = {t["id"]: t for t in read_jsonl(_run_dir(settings, h_run) / "traces.jsonl")}
    dense = dense_mod.DenseIndex.load(settings, h_config["dense_version"], base=index)
    vectors = {}
    for row in rows:
        if is_passage_row(row):
            vec, _ = dense_mod.query_vector(settings, None, row["question"], request_id=None, member_id="owner-cli",
                                            purpose="gold_eval", allow_paid=False)
            if vec is None:
                raise EvaluationError("a frozen H query vector is missing from the cache; rerun H")
            vectors[row["id"]] = vec
    if reranker is None:
        reranker, load_info = dense_mod.load_reranker(settings)
    load_info = load_info or getattr(reranker, "info", {})
    report: dict = {"h_run": h_run, "load": load_info, "depths": {}, "created_at": utcnow()}
    if reranker is None:
        report["gate"] = {"passed": False, "decision": "bypass", "reason": f"reranker unavailable: "
                                                                           f"{load_info.get('error')}"}
        run_id = f"HR-trial-{hashlib.sha256(dumps([h_run, depths, load_info]).encode()).hexdigest()[:10]}"
        _write_run(settings, run_id, {**h_config, "label": "HR", "mode": "hybrid_rerank"},
                   [], {"status": "blocked", "reason": report["gate"]["reason"], "trial": report})
        return {"run_id": run_id, **report}
    passage = [r for r in rows if is_passage_row(r)]
    # Warm once, then measure the reranking stage alone and under concurrent load.
    if passage:
        _execute(settings, index, analyzer, passage[:1], "hybrid_rerank", dense, vectors, reranker, max(depths))
    base_ndcg = h_scores["aggregate"]["ndcg@5"] or 0.0
    base_critical = set(h_scores["aggregate"]["critical_failures"])
    best = None
    for depth in depths:
        results = _execute(settings, index, analyzer, rows, "hybrid_rerank", dense, vectors, reranker, depth)
        for r in results:  # the pool must be exactly the frozen H candidates
            if r.get("metrics"):
                frozen = [c["chunk_id"] for c in h_traces[r["id"]]["candidates"] if c["channel"] == "rrf"][:depth]
                now = [c["chunk_id"] for c in r["candidates"] if c["channel"] == "rrf"][:depth]
                if frozen != now:
                    raise EvaluationError(f"H candidates changed for {r['id']}; rerun H before the trial")
        alone = [r["timings_ms"]["rerank"] for r in results if r.get("timings_ms")]

        def one(row):
            t0 = time.perf_counter()
            _execute(settings, index, analyzer, [row], "hybrid_rerank", dense, vectors, reranker, depth)
            return (time.perf_counter() - t0) * 1000

        def base(row):
            t0 = time.perf_counter()
            _execute(settings, index, analyzer, [row], "hybrid", dense, vectors)
            return (time.perf_counter() - t0) * 1000

        with ThreadPoolExecutor(max_workers=users) as pool:
            loaded = list(pool.map(one, passage * max(1, users // max(1, len(passage)))))
            unranked = list(pool.map(base, passage * max(1, users // max(1, len(passage)))))
        agg = aggregate(results, skipped)
        new_critical = sorted(set(agg["critical_failures"]) - base_critical)
        added_p95 = round((percentile(loaded, 0.95) or 0) - (percentile(unranked, 0.95) or 0), 1)
        gain = round((agg["ndcg@5"] or 0.0) - base_ndcg, 4)
        gate = {"ndcg@5_gain": gain, "required_gain": GATE_NDCG_GAIN, "new_critical_failures": new_critical,
                "added_p95_ms_under_load": added_p95, "allowed_added_p95_ms": GATE_ADDED_P95_MS, "users": users,
                "passed": gain >= GATE_NDCG_GAIN and not new_critical and added_p95 <= GATE_ADDED_P95_MS,
                "pilot_rows": agg["passage_rows"],
                "note": "pilot-sized sample: review a marginal gain on the larger dev set before final release"}
        config = {**h_config, "label": "HR", "mode": "hybrid_rerank", "h_run": h_run,
                  "reranker": {k: load_info.get(k) for k in ("model", "revision", "device", "max_length")},
                  "rerank_depth": depth}
        run_id = f"HR-{hashlib.sha256(dumps(config).encode()).hexdigest()[:10]}"
        scores = {"status": "complete", "aggregate": agg, "gate": gate, "created_at": utcnow(),
                  "latency_ms": {"rerank_alone_p50": percentile(alone, 0.5), "rerank_alone_p95": percentile(alone, 0.95),
                                 "hr_under_load_p95": percentile(loaded, 0.95),
                                 "h_under_load_p95": percentile(unranked, 0.95)},
                  "load": load_info}
        _write_run(settings, run_id, config, results, scores)
        report["depths"][depth] = {"run_id": run_id, **gate, "ndcg@5": agg["ndcg@5"]}
        if gate["passed"] and (best is None or gain > report["depths"][best]["ndcg@5_gain"]):
            best = depth
    report["gate"] = {"passed": best is not None, "decision": f"eligible at depth {best}" if best else "bypass",
                      "best_depth": best}
    return report


def activate_run(settings: Settings, run_id: str, decision_path: Path, actor: str = "owner-cli") -> dict:
    """Validates a reviewed selection and its ready artifacts, then switches the serving configuration in one
    transaction. The previous configuration is kept in the append-only activation history."""
    from . import dense as dense_mod
    from .retrieval import KeywordIndex, RetrievalError
    from .store import get_app_setting, set_app_setting, tx

    if not decision_path.is_absolute():
        raise EvaluationError("--decision-file must be an absolute path")
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    config, scores = load_run(settings, run_id)
    errors = []
    if decision.get("run_id") != run_id:
        errors.append("decision run_id does not match --run-id")
    if decision.get("mode") != config["mode"]:
        errors.append(f"decision mode must be the run's mode ({config['mode']})")
    for key in ("decided_by", "rationale"):
        if not str(decision.get(key, "")).strip():
            errors.append(f"decision requires {key!r}")
    if scores.get("status") != "complete":
        errors.append("the run is not complete")
    if config["mode"] == "hybrid_rerank" and not (scores.get("gate") or {}).get("passed"):
        errors.append("the reranker did not pass its promotion gate; keep the bypass")
    try:
        index = KeywordIndex.load(settings, config["index_version"])
        if index.manifest_hash != config["index_manifest_hash"]:
            errors.append("keyword index manifest changed since the run")
    except RetrievalError as exc:
        errors.append(f"keyword index not ready: {exc}")
        index = None
    if config.get("dense_version") and index is not None:
        try:
            dense_mod.DenseIndex.load(settings, config["dense_version"], base=index)
        except dense_mod.DenseError as exc:
            errors.append(f"dense matrix not ready: {exc}")
    if errors:
        raise EvaluationError("; ".join(errors))
    active = {"run_id": run_id, "label": config["label"], "mode": config["mode"],
              "index_version": config["index_version"], "dense_version": config.get("dense_version"),
              "reranker": {**config["reranker"], "depth": config["rerank_depth"]} if config.get("reranker") else None,
              "fallback_mode": "kiwi_bm25", "finalist_run_id": decision.get("finalist_run_id"),
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


def write_phase2_report(settings: Settings) -> Path:
    """`.runtime/releases/phase-2/report.md`: what is actually recorded in this data directory, nothing
    assumed. Missing steps appear as missing."""
    from .ingestion import identity_report, manifest_report, review_coverage
    from .store import get_app_setting

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
        from .budget import _purpose_used

        used = {k: _purpose_used(conn, k) for k in envelopes}
        activations = [dict(r) for r in conn.execute("SELECT run_id, config_json, decision_json, actor, created_at "
                                                     "FROM activations ORDER BY created_at")]
        active_run = get_app_setting(conn, "active_run")
        active_index = get_app_setting(conn, "active_index")
    runs = []
    base = settings.data_dir / "runs"
    for d in sorted(base.iterdir()) if base.exists() else []:
        try:
            config, scores = load_run(settings, d.name)
        except (EvaluationError, json.JSONDecodeError):
            continue
        runs.append({"run_id": d.name, "label": config.get("label"), "profile": config.get("profile"),
                     "index": config.get("index_version"), **_headline(scores),
                     "gate": (scores.get("gate") or {}).get("passed"), "load": scores.get("load")})
    checked = [c for c in coverage if c["review_status"] in ("sample_checked", "reviewed")]
    usd = lambda m: f"${m / 1_000_000:.6f}"  # noqa: E731
    L = ["# Phase 2 report — corpus coverage and retrieval selection", "",
         f"Generated {utcnow()} from `{settings.data_dir.name}/`. Every figure below is read from recorded state; "
         "steps that were not run are listed as missing.", "",
         "## Manifest and source review", "",
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
    fams = families["families"]
    L += ["", "## Evaluation families", ""]
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
    L += ["", "## Spend (tracked by this ledger)", "", "| Purpose | Stage | State | Attempts | Settled | Reserved |",
          "| --- | --- | --- | --- | --- | --- |"]
    L += [f"| {s['purpose']} | {s['stage']} | {s['state']} | {s['n']} | {usd(s['settled'])} | {usd(s['reserved'])} |"
          for s in spend] or ["| — | — | — | 0 | $0 | $0 |"]
    L += ["", "Envelopes (used incl. open reservations / envelope): " + ", ".join(
        f"{k} {usd(used[k])}/{usd(v)}" for k, v in envelopes.items()), "",
          "## Selection", ""]
    if active_run:
        a = json.loads(active_run)
        L += [f"- active: run `{a['run_id']}` ({a['label']}, {a['mode']}), keyword index `{a['index_version']}`, "
              f"dense `{a.get('dense_version')}`, reranker {a.get('reranker')}",
              f"- fallback: {a.get('fallback_mode')}; retrieval finalist for phase 4: {a.get('finalist_run_id')}"]
        for act in activations:
            L.append(f"  - {act['created_at'][:19]} {act['actor']}: {json.loads(act['decision_json']).get('rationale')}")
    else:
        L += [f"- no `activate-run` decision recorded: serving the keyword default (kiwi_bm25) on index "
              f"`{active_index}`; dense and reranker stay inactive"]
    L.append("")
    path = settings.data_dir / "releases" / "phase-2" / "report.md"
    write_text_atomic(path, "\n".join(L))
    return path
