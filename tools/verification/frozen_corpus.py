"""Offline preflight for a frozen preprocessing package; never imports into the application DB.

Run with --package, --source, --runtime, --baseline-index and a new --output directory.
Text projections exercise the existing chunker, but are not serving extractions or reviewed gold.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath, PureWindowsPath

from rfp_assistant.corpus.ingestion import nfc, read_manifest_csv, sha256_file
from rfp_assistant.retrieval import chunking
from rfp_assistant.settings import Settings
from rfp_assistant.storage.store import read_jsonl, utcnow, write_jsonl_atomic, write_text_atomic

TEXT_FIELDS = ("raw_text", "clean_text", "search_text")
PREVIEW_VERSION = "frozen-record-preview-1"


def relative_name(value: str) -> str:
    """Accept only a source filename, including decomposed Hangul from the producer's Mac."""
    if not isinstance(value, str) or not value or value in (".", "..") or "/" in value or "\\" in value or ":" in value:
        raise ValueError(f"unsafe source filename: {value!r}")
    return nfc(value)


def source_map(manifest: list[dict], originals: list[dict]) -> list[dict]:
    """Bind producer identities to all existing CSV associations by original bytes, never producer paths."""
    by_hash = defaultdict(list)
    for row in originals:
        by_hash[row["source_hash"]].append(row)
    mapped = []
    seen = set()
    for source in manifest:
        digest = source["sha256"]
        if digest in seen:
            raise ValueError(f"duplicate source hash: {digest}")
        seen.add(digest)
        aliases = {relative_name(p) for p in source["source_paths"]}
        rows = by_hash.get(digest, [])
        if not rows or aliases != {r["filename"] for r in rows}:
            raise ValueError(f"original aliases/hash mismatch: {digest}")
        if relative_name(source["representative_path"]) not in aliases:
            raise ValueError(f"representative is not a source alias: {digest}")
        mapped.append({
            "package_doc_id": source["doc_id"], "source_hash": digest,
            "associations": [{"doc_id": r["doc_id"], "filename": r["filename"],
                              "original_relative_to_source_dir": f"files/{r['filename']}"} for r in rows],
        })
    if seen != set(by_hash):
        raise ValueError("package and CSV cover different originals")
    return mapped


def external_paths(value, prefix="") -> Counter:
    """Count external provenance references; no referenced artifact is opened."""
    result = Counter()
    if isinstance(value, dict):
        for key, child in value.items():
            result.update(external_paths(child, f"{prefix}.{key}"))
    elif isinstance(value, list):
        for child in value:
            result.update(external_paths(child, prefix + "[]"))
    elif isinstance(value, str) and (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()):
        result[prefix] += 1
    return result


def validate_records(records: list[dict], manifest: list[dict]) -> None:
    sources = {s["doc_id"]: s["sha256"] for s in manifest}
    ids = set()
    for row in records:
        rid = row["record_id"]
        if not isinstance(rid, str) or not rid or rid in ids:
            raise ValueError(f"empty or duplicate record_id: {rid!r}")
        ids.add(rid)
        if sources.get(row["doc_id"]) != row["sha256"]:
            raise ValueError(f"record source mismatch: {rid}")
        if any(not isinstance(row[f], str) for f in TEXT_FIELDS):
            raise ValueError(f"invalid text field: {rid}")
        if row["element_type"] not in ("body", "page", "image_panel"):
            raise ValueError(f"unsupported element_type: {rid}")
        if not isinstance(row["heading_path"], list) or any(not isinstance(x, str) for x in row["heading_path"]):
            raise ValueError(f"invalid heading_path: {rid}")
        if type(row["index_eligible"]) is not bool:
            raise ValueError(f"invalid index_eligible: {rid}")
        start, end = row["raw_start"], row["raw_end"]
        if (start is None) != (end is None) or (start is not None and
                (type(start) is not int or type(end) is not int or not 0 <= start <= end)):
            raise ValueError(f"invalid raw coordinates: {rid}")
    if {r["doc_id"] for r in records} != set(sources):
        raise ValueError("manifest contains documents without records")


def project(records: list[dict], field: str, dataset_hash: str) -> tuple[str, list[dict]]:
    """Ephemeral chunker input. Its raw_text slot holds the named field, never original raw coordinates.

    No table cells are guessed from flattened text. Rich metadata remains in the immutable input.
    These views cannot be persisted as production elements without an evidence/structure adapter.
    """
    if field not in ("clean_text", "search_text"):
        raise ValueError("preview must explicitly choose clean_text or search_text")
    source_hash = records[0]["sha256"]
    extraction = hashlib.sha256(f"{PREVIEW_VERSION}:{dataset_hash}:{source_hash}:{field}".encode()).hexdigest()[:24]
    elements = []
    for order, row in enumerate(records):
        # An intentionally empty search value is not repaired with another text field.
        if not row["index_eligible"] or not row["search_text"].strip() or not row[field].strip():
            continue
        location = {"path": row["record_id"], "section_path": row["heading_path"],
                    "record_id": row["record_id"], "span_text_field": field,
                    "source_element_type": row["element_type"],
                    "pdf_page_number": row["pdf_page_number"], "hwp_element_index": row["hwp_element_index"]}
        if row["pdf_page_number"] is not None:
            location["page"] = row["pdf_page_number"]
        elements.append({"element_id": hashlib.sha256(row["record_id"].encode()).hexdigest()[:20],
                         "source_order": order, "kind": "paragraph", "parent_id": None,
                         "raw_text": row[field], "search_text": row["search_text"],
                         "location": location, "table": None})
    return extraction, elements


def preview(records: list[dict], field: str, dataset_hash: str) -> dict:
    extraction, elements = project(records, field, dataset_hash)
    try:
        chunks, inventory = chunking.build_chunks(elements, extraction)
    except (IndexError, ValueError) as error:
        return {"source_hash": records[0]["sha256"], "field": field, "records": len(records),
                "projected_elements": len(elements), "chunks": 0, "requirements": 0, "max_tokens": None,
                "errors": [{"kind": "chunker_exception", "exception": type(error).__name__, "detail": str(error)}],
                "oversized_headings": [{"record_id": e["location"]["record_id"], "tokens": tokens}
                                       for e in elements
                                       if (tokens := chunking.count_tokens(chunking._heading(
                                           e["location"]["section_path"]))) >= chunking.HARD_TOKENS - 8]}
    by_id = {e["element_id"]: e for e in elements}
    covered = defaultdict(list)
    errors = []
    for chunk in chunks:
        if chunk["token_count"] > chunking.HARD_TOKENS:
            errors.append({"kind": "token_limit", "chunk_id": chunk["chunk_id"], "tokens": chunk["token_count"]})
        for span in chunk["spans"]:
            el = by_id.get(span["element_id"])
            if el is None or not 0 <= span["start"] < span["end"] <= len(el["raw_text"]):
                errors.append({"kind": "invalid_preview_span", "chunk_id": chunk["chunk_id"]})
            else:
                covered[el["element_id"]].append((span["start"], span["end"]))
    for el in elements:
        end = 0
        for start, stop in sorted(covered[el["element_id"]]):
            if start > end and el["raw_text"][end:start].strip():
                errors.append({"kind": "uncovered_text", "record_id": el["location"]["record_id"]})
            end = max(end, stop)
        if el["raw_text"][end:].strip():
            errors.append({"kind": "uncovered_text", "record_id": el["location"]["record_id"]})
    return {"source_hash": records[0]["sha256"], "field": field, "records": len(records),
            "projected_elements": len(elements), "chunks": len(chunks), "requirements": len(inventory),
            "max_tokens": max((c["token_count"] for c in chunks), default=0), "errors": errors}


def normalized(text: str) -> str:
    return re.sub(r"\s+", "", nfc(text))


def audit_quotes(rows: list[dict], documents: dict[str, str]) -> dict:
    """Diagnostic quote availability, not span migration, gold approval or retrieval quality."""
    results = []
    for row in rows:
        missing = []
        matched = 0
        for group in row["evidence_groups"]:
            found = any(bool(normalized(a["quote"])) and
                        normalized(a["quote"]) in documents.get(a["source_hash"], "")
                        for a in group["alternatives"])
            matched += found
            if not found:
                missing.append({"group_id": group["group_id"],
                                "alternatives": [{"source_hash": a["source_hash"], "quote": a["quote"]}
                                                 for a in group["alternatives"]]})
        results.append({"question_id": row["question_id"], "groups": len(row["evidence_groups"]),
                        "groups_found": matched, "all_groups_found": bool(row["evidence_groups"]) and not missing,
                        "missing_groups": missing})
    return {"questions": len(results), "questions_with_all_groups": sum(r["all_groups_found"] for r in results),
            "groups": sum(r["groups"] for r in results), "groups_found": sum(r["groups_found"] for r in results),
            "rows": results}


def run(package: Path, source: Path, runtime: Path, baseline_index: str, output: Path) -> dict:
    package, source, runtime, output = [p.resolve() for p in (package, source, runtime, output)]
    if output.exists() or any(output.is_relative_to(p) for p in (package, source, runtime / "indexes", runtime / "datasets")):
        raise ValueError("output must be a new directory outside the input datasets and indexes")
    before = {p.relative_to(package).as_posix(): sha256_file(p) for p in package.rglob("*") if p.is_file()}
    for name in before:
        with (package / name).open("rb") as stream:
            if stream.read(3) == b"\xef\xbb\xbf":
                raise ValueError(f"input has a UTF-8 BOM: {name}")
    final = json.loads((package / "manifest/final_dataset_manifest.json").read_text(encoding="utf-8"))
    if not final.get("frozen") or before["data/rag_input_final.jsonl"] != final["SHA256"]:
        raise ValueError("frozen dataset hash mismatch")
    manifest = json.loads((package / "metadata/source_manifest.json").read_text(encoding="utf-8"))
    records = read_jsonl(package / "data/rag_input_final.jsonl")
    validate_records(records, manifest)
    if len(records) != final["record_count"] or len(manifest) != final["document_count"]:
        raise ValueError("manifest counts do not match the data")
    settings = Settings(source_dir=source, data_dir=runtime, hwp_converter=None)
    originals = read_manifest_csv(settings)
    for row in originals:
        row["source_hash"] = sha256_file(row["path"])
    mapping = source_map(manifest, originals)
    index_dir = runtime / "indexes" / relative_name(baseline_index)
    baseline = json.loads((index_dir / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in baseline["files"].items():
        if sha256_file(index_dir / relative_name(name)) != digest:
            raise ValueError(f"baseline index hash mismatch: {name}")
    baseline_ids = defaultdict(set)
    for chunk in read_jsonl(index_dir / "chunks.jsonl"):
        baseline_ids[chunk["extraction_id"]].update(s["element_id"] for s in chunk["spans"])
    baseline_text = {}
    for original in baseline["sources"]:
        # Historical artifact directories use parser fingerprints, not extraction IDs.
        # Resolve by the index's actual element IDs, including OCR IDs with no native element path.
        extraction = original["active_extraction_id"]
        source_root = runtime / "extracted" / relative_name(original["source_hash"])
        elements = None
        for artifact in source_root.glob("*/elements.jsonl"):
            candidate = read_jsonl(artifact)
            if baseline_ids[extraction] and baseline_ids[extraction] <= {e["element_id"] for e in candidate}:
                elements = candidate
                break
        if elements is None:
            raise ValueError(f"baseline extraction artifact not found: {extraction}")
        baseline_text[original["source_hash"]] = normalized("\n".join(e["raw_text"] for e in elements))
    by_source = defaultdict(list)
    paths = Counter()
    for row in records:
        by_source[row["sha256"]].append(row)
        paths.update(external_paths(row, "record"))
    for name in ("manifest/final_dataset_manifest.json", "metadata/provenance.json"):
        paths.update(external_paths(json.loads((package / name).read_text(encoding="utf-8")), name))
    print("Original hashes and aliases verified; testing both text projections.", flush=True)
    previews = [preview(rows, field, final["SHA256"]) for field in ("clean_text", "search_text")
                for rows in by_source.values()]
    quotes = {}
    for name in ("dev", "corpus"):
        rows = read_jsonl(runtime / "datasets" / f"{name}.jsonl")
        if not rows or any(r.get("split") != "dev" or r.get("review", {}).get("status") != "approved" for r in rows):
            raise ValueError(f"expected independently reviewed development rows: {name}")
        quotes[name] = {field: audit_quotes(rows, {h: normalized("\n".join(r[field] for r in rs))
                                                for h, rs in by_source.items()}) for field in TEXT_FIELDS}
        quotes[name]["baseline_raw_text"] = audit_quotes(rows, baseline_text)
    after = {p.relative_to(package).as_posix(): sha256_file(p) for p in package.rglob("*") if p.is_file()}
    if before != after:
        raise ValueError("input package changed during verification")
    report = {
        "created_at": utcnow(), "preflight_status": "failed" if any(p["errors"] for p in previews) else "passed",
        "serving_readiness": "blocked", "dataset_sha256": final["SHA256"], "package_hashes": before,
        "records": len(records), "documents": len(manifest), "csv_associations": len(originals),
        "empty_search_text": sum(not r["search_text"].strip() for r in records),
        "element_types": dict(Counter(r["element_type"] for r in records)),
        "text_changes": {"raw_to_clean": sum(r["raw_text"] != r["clean_text"] for r in records),
                         "clean_to_search": sum(r["clean_text"] != r["search_text"] for r in records)},
        "portability": {"originals_matched_by_sha256": len(mapping), "portable_source_mapping": True,
                        "gcp_execution": "not_run", "external_provenance_paths": dict(paths),
                        "external_provenance_policy": "retain as history; never require missing producer artifacts"},
        "baseline": {"index_version": baseline["index_version"], "chunks": baseline["chunk_count"],
                     "requirements": len(read_jsonl(index_dir / "requirements.jsonl"))},
        "chunk_preview": {"version": PREVIEW_VERSION, "profile": "structural",
                          "chunker_fingerprint": chunking.chunker_fingerprint(), "rows": previews,
                          "span_basis": "offsets within the selected text field; not original raw offsets",
                          "structure": "paragraph projection only; table/requirement metadata not adapted"},
        "quote_availability": quotes,
        "blockers": ["gold evidence needs reviewed repinning to new record coordinates",
                     "table cells and structured requirement inventory need a preserving adapter",
                     "search-only descriptions need separate citation provenance"],
        "not_run": ["application database import", "keyword/dense index activation", "embedding inference",
                    "paid answer evaluation", "sealed test", "GCP deployment"],
    }
    output.mkdir(parents=True)
    write_jsonl_atomic(output / "source-map.jsonl", mapping)
    write_text_atomic(output / "report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    summary = {k: report[k] for k in ("preflight_status", "serving_readiness", "records", "documents", "empty_search_text")}
    summary["preview"] = {field: {"chunks": sum(p["chunks"] for p in previews if p["field"] == field),
                                  "errors": sum(len(p["errors"]) for p in previews if p["field"] == field)}
                          for field in ("clean_text", "search_text")}
    summary["quote_availability"] = {name: {field: {k: v for k, v in result.items() if k != "rows"}
                                            for field, result in fields.items()} for name, fields in quotes.items()}
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("package", "source", "runtime", "output"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    parser.add_argument("--baseline-index", required=True)
    args = parser.parse_args()
    report = run(args.package, args.source, args.runtime, args.baseline_index, args.output)
    return 1 if report["preflight_status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
