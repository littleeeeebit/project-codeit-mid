"""Plan/run an offline native-large reference against the fixed 1536 candidate.

Only the frozen development scopes are embedded at 3072. No reference set is
published for serving. All paid work uses the configured authoritative ledger.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rfp_assistant import budget, dense, evaluation, service, store
from rfp_assistant.retrieval import KeywordIndex, RUN_MODES, index_compatibility
from rfp_assistant.settings import DEFAULT_RATES, load_settings


def compare(candidate, reference, baseline):
    """Require usable denominators and all unchanged acceptance thresholds."""
    def loss(key, nested=False):
        a, b = candidate[key], reference[key]
        if nested:
            if a["denominator"] <= 0 or a["denominator"] != b["denominator"]:
                return None
            a, b = a["rate"], b["rate"]
        if a is None or b is None:
            return None
        return float(Decimal(str(b)) - Decimal(str(a)))
    ndcg = loss("ndcg@5")
    packed = loss("packed_complete", True)
    critical = set(candidate["critical_failures"])
    checks = {
        "same_nonzero_ndcg_population": candidate["ndcg_eligible_rows"] > 0 and
            candidate["ndcg_eligible_rows"] == reference["ndcg_eligible_rows"],
        "ndcg_loss_at_most_0.02": ndcg is not None and ndcg <= .02,
        "packed_support_loss_at_most_0.02": packed is not None and packed <= .02,
        "no_new_reference_critical_failures": not critical - set(reference["critical_failures"]),
        "no_new_baseline_critical_failures": not critical - set(baseline["critical_failures"]),
        "no_wrong_scope": candidate["wrong_scope_candidates"] == 0,
        "no_fallback": not candidate["fallbacks"],
    }
    return {"passed": all(checks.values()), "checks": checks, "ndcg_loss": ndcg,
            "packed_support_loss": packed,
            "new_reference_critical": sorted(critical - set(reference["critical_failures"])),
            "new_baseline_critical": sorted(critical - set(baseline["critical_failures"]))}


class BoundedReference:
    """An offline reference refuses any row outside its declared development scopes."""
    def __init__(self, index, vectors, model, dims):
        self.version = f"offline-{model}-{dims}"
        self.base_index_version = index.version
        self.model, self.dims = model, dims
        self.chunk_ids = [c["chunk_id"] for c in index.chunks]
        self.vectors = vectors

    def search(self, query, allowed, k):
        if any(i not in self.vectors for i in allowed):
            raise ValueError("reference search exceeds the frozen development coverage")
        scores = [(i, float(self.vectors[i] @ query)) for i in allowed]
        return sorted(scores, key=lambda r: (-r[1], self.chunk_ids[r[0]]))[:k]


def reference_rows(index, covered, by_payload):
    # Deduplicate provider payloads, never the association-specific chunk rows.
    return {index.row_of[p["chunk_id"]]: by_payload[p["payload_hash"]] for p in covered}


def candidate_vectors(settings, candidate, index, payloads):
    """Candidate vectors from the verified ready set; the mutable cache is not part of its identity."""
    if settings.database_backend == "sqlite":
        return {p["payload_hash"]: candidate.matrix[index.row_of[p["chunk_id"]]] for p in payloads}
    from rfp_assistant.vector_store import verified
    with store.open_db(settings.db_path) as conn:
        rows = conn.execute("SELECT r.chunk_id,p.embedding,p.vector_checksum FROM embedding_set_rows r "
                            "JOIN embedding_payloads p USING(payload_hash) WHERE r.set_version=? AND r.chunk_id=ANY(?)",
                            (candidate.version, [p["chunk_id"] for p in payloads])).fetchall()
    by_chunk = {r["chunk_id"]: verified(r["embedding"], candidate.dims, r["vector_checksum"]) for r in rows}
    return {p["payload_hash"]: by_chunk[p["chunk_id"]] for p in payloads}


def job_cost(conn, request_id):
    return conn.execute("SELECT COALESCE(SUM(CASE WHEN state='settled' THEN settled_micro_usd "
                        "WHEN state IN ('reserved','dispatching','unknown') THEN reserved_micro_usd "
                        "ELSE 0 END),0) FROM attempts WHERE request_id=?", (request_id,)).fetchone()[0]


def ceiling_guard(conn, request_id, previous, maximum):
    # Invoked inside durable dispatch admission; include concurrent reservations,
    # and use actual snapshotted prices rather than trusting the planning rate.
    if job_cost(conn, request_id) - previous > maximum:
        return "comparison_invocation_cost_ceiling"
    return None


def prepare(settings, analyzer=None, index_version=None):
    if (settings.embedding_model, settings.embedding_dimensions) != ("text-embedding-3-large", 1536):
        raise ValueError("serving candidate is fixed at text-embedding-3-large/1536")
    frozen = evaluation.frozen_dataset(settings, "dev")
    if not frozen or not frozen["current"]:
        raise ValueError("freeze the independently reviewed development population first")
    validation = evaluation.validate_gold(settings, "dev")
    if not validation["ok"]:
        raise ValueError("frozen development source/review validation failed")
    rows, skipped, dataset_sha = evaluation.load_eval_rows(settings, "dev")
    if skipped or len(rows) != frozen["rows"]:
        raise ValueError("every frozen development row must remain eligible")
    index = KeywordIndex.load(settings, index_version)  # default: the active pointer
    outdated = index_compatibility(index, analyzer)
    if outdated:
        raise ValueError(outdated)
    passages = [r for r in rows if evaluation.is_passage_row(r)]
    # Every frozen scope must be indexed: an absent one ranks empty for all three systems and passes unmeasured.
    unindexed = [evaluation.row_id(r) for r in passages if not evaluation.scope_indexed(index, r)
                 or not all(index.rows_by_extraction.get(x) for _, x in evaluation.row_scope(r))]
    if unindexed:
        raise ValueError(f"frozen passage scopes are absent from the active keyword index: {unindexed[:5]}")
    version = evaluation._ready_dense_for(settings, index.version)
    if not version:
        raise ValueError("complete fixed-dimension corpus is required")
    candidate = dense.DenseIndex.load(settings, version, base=index)
    scopes = {x for r in passages for _, x in evaluation.row_scope(r)}
    _, payloads = dense.index_payloads(settings, index.version)
    covered = [p for p in payloads if p["extraction_id"] in scopes]
    wanted = {p["payload_hash"]: p for p in covered}
    reference = settings.with_(embedding_dimensions=3072)
    missing = []
    for p in wanted.values():
        key = dense.payload_hash(p["text"], reference.embedding_model, reference.embedding_dimensions)
        if dense.cache_get(reference, key) is None:
            missing.append({**p, "reference_hash": key,
                            "tokens": dense.count_embedding_tokens(p["text"], reference.embedding_model)})
    batches = dense.plan_batches(reference, missing)
    rates = DEFAULT_RATES[settings.embedding_model]
    corpus_max = sum(budget.max_cost(rates, sum(p["tokens"] for p in b), 0) for b in batches)
    query_max = 0
    for s in (settings, reference):
        for r in rows:
            if not evaluation.is_passage_row(r):
                continue
            key = dense.payload_hash(dense.normalize_payload(r["question"]), s.embedding_model, s.embedding_dimensions)
            if dense.cache_get(s, key) is None:
                query_max += budget.max_cost(rates, dense.count_embedding_tokens(r["question"], s.embedding_model) + dense.QUERY_MARGIN_TOKENS, 0)
    envelope = dense._ledger_view(settings, "embedding")
    gold = dense._ledger_view(settings, "gold_eval")
    plan = {"version": "large-quality-plan-1", "candidate_model": settings.embedding_model,
            "candidate_dimensions": 1536, "reference_dimensions": 3072,
            "reference_serving": False, "dataset_sha256": dataset_sha,
            "population_sha256": evaluation.population_identity(rows, skipped), "population_rows": len(rows),
            "frozen": frozen, "index_version": index.version, "index_manifest_hash": index.manifest_hash,
            "candidate_version": candidate.version, "scopes": len(scopes), "reference_payloads": len(wanted),
            "reference_rows": len(covered),
            "limits": {k: getattr(settings, k) for k in ("channel_top_k", "fused_top_k", "rrf_k",
                "evidence_target_tokens", "evidence_max_tokens", "evidence_max_units")},
            "reference_misses": len(missing), "reference_tokens": sum(p["tokens"] for p in missing),
            "reference_batches": len(batches), "corpus_max_micro_usd": corpus_max,
            "query_max_micro_usd": query_max,
            "fits_envelopes": corpus_max <= envelope["envelope_remaining_micro_usd"] and
                query_max <= gold["envelope_remaining_micro_usd"] and
                corpus_max + query_max <= envelope["available_micro_usd"]}
    return plan, rows, skipped, index, candidate, reference, covered, batches


def run(settings, resources, out, max_cost_micro, index_version=None):
    plan, rows, skipped, index, candidate, ref_settings, covered, batches = prepare(settings, resources.analyzer,
                                                                                   index_version)
    probe = candidate_vectors(settings, candidate, index, list({p["payload_hash"]: p for p in covered}.values())[:8])
    if not plan["fits_envelopes"] or plan["corpus_max_micro_usd"] + plan["query_max_micro_usd"] > max_cost_micro:
        raise ValueError("reference/query estimate exceeds the existing envelopes or explicit ceiling")
    for purpose in ("embedding", "gold_eval"):
        if dense.unresolved_attempts(settings, purpose):
            raise ValueError("unknown embedding billing blocks all comparison dispatch")
    store.write_text_atomic(out / "plan.json", store.dumps(plan))
    job = dense.ensure_job_request(settings, "codex-cutover", "large-reference:" + plan["population_sha256"], plan)
    with store.open_db(settings.db_path) as conn:
        previous = job_cost(conn, job)
    def guard(conn):
        return ceiling_guard(conn, job, previous, max_cost_micro)
    for number, batch in enumerate(batches, 1):
        result = dense.metered_embed(ref_settings, resources.transport, [p["text"] for p in batch],
                                    sum(p["tokens"] for p in batch), request_id=job,
                                    member_id="codex-cutover", purpose="embedding", guard=guard)
        if result["status"] != "ok" or result["billing"] != "settled":
            raise RuntimeError("reference batch not settled; stop without retry")
        for p, vector in zip(batch, result["vectors"]):
            dense.cache_put(ref_settings, p["reference_hash"], vector, {
                "kind": "offline-development-reference", "attempt_id": result["attempt_id"],
                "normalized_payload_sha256": hashlib.sha256(p["text"].encode()).hexdigest(),
                "construction": "provider-explicit-dimensions:l2-float32"})
        print(json.dumps({"event": "reference_batch_settled", "batch": number, "batches": len(batches)}), flush=True)
    by_payload, paired = {}, []
    wanted = {p["payload_hash"]: p for p in covered}
    for p in wanted.values():
        key = dense.payload_hash(p["text"], ref_settings.embedding_model, 3072)
        vector = dense.cache_get(ref_settings, key)
        if vector is None:
            raise RuntimeError("incomplete reference cache")
        by_payload[p["payload_hash"]] = vector
        if p["payload_hash"] in probe:
            derived, provenance = dense.shorten_large_reference(vector, 1536, model=settings.embedding_model)
            api = probe[p["payload_hash"]]
            paired.append({"payload_hash": p["payload_hash"], "cosine": float(derived @ api),
                           "max_abs_difference": float(np.max(np.abs(derived-api))), "provenance": provenance})
    reference = BoundedReference(index, reference_rows(index, covered, by_payload), settings.embedding_model, 3072)
    queries = {}
    for s in (settings, ref_settings):
        qs = {}
        for row in rows:
            if evaluation.is_passage_row(row):
                vector, info = dense.query_vector(s, resources.transport, row["question"], request_id=job,
                    member_id="codex-cutover", purpose="gold_eval", allow_paid=True, guard=guard)
                if vector is None or info.get("billing") == "unknown":
                    raise RuntimeError("query vector unavailable or billing unknown; stop without retry")
                qs[evaluation.row_id(row)] = vector
        queries[s.embedding_dimensions] = qs
    results = {}
    for name, s, indexer, dims, labels in (("baseline", settings, None, None, ["K1"]),
            ("candidate", settings, candidate, 1536, ["D", "H"]),
            ("reference", ref_settings, reference, 3072, ["D", "H"])):
        for label in labels:
            traces = evaluation._execute(s, index, resources.analyzer, rows, RUN_MODES[label],
                                         dense=indexer, vectors=queries.get(dims))
            store.write_jsonl_atomic(out / f"{name}-{label}.jsonl", traces)
            results[f"{name}-{label}"] = evaluation.aggregate(traces, skipped)
    gates = {label: compare(results[f"candidate-{label}"], results[f"reference-{label}"], results["baseline-K1"])
             for label in ("D", "H")}
    current = evaluation.frozen_dataset(settings, "dev")
    if not current or not current["current"] or current["dataset_sha256"] != plan["dataset_sha256"]:
        raise ValueError("development population changed during comparison; acceptance refused")
    report = {"version": "large-quality-check-1", "passed": all(g["passed"] for g in gates.values()),
              "checked_at": store.utcnow(), "plan": plan, "aggregates": results, "gates": gates,
              "paired_shortening": paired, "production_vectors": "API dimensions=1536 only; no derived mixing",
              "eval_version": evaluation.EVAL_VERSION, "ranking_policy": evaluation.GOLD_RANKING_POLICY,
              "trace_hashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in out.glob("*.jsonl")}}
    with store.open_db(settings.db_path) as conn:
        attempts = conn.execute("SELECT state,COUNT(*),COALESCE(SUM(settled_micro_usd),0) FROM attempts "
                                "WHERE request_id=? GROUP BY state", (job,)).fetchall()
        original_plan = json.loads(conn.execute("SELECT trace_json FROM requests WHERE request_id=?", (job,)).fetchone()[0])
    report["billing"] = {"request_id": job, "attempts": {r[0]: {"count": r[1], "micro_usd": r[2]} for r in attempts},
                         "first_estimate_micro_usd": original_plan["corpus_max_micro_usd"] + original_plan["query_max_micro_usd"]}
    report["candidate_content_sha256"] = hashlib.sha256(candidate.matrix.astype("<f4").tobytes()).hexdigest() \
        if settings.database_backend == "sqlite" else None
    store.write_text_atomic(out / "quality.json", store.dumps(report))
    dense.finish_job_request(settings, job, "completed", {"quality_passed": report["passed"], "report": str(out / "quality.json")})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--max-cost-usd", default="0")
    parser.add_argument("--index-version", help="keyword index to compare on (default: the active one); "
                        "lets a rebuilt, unactivated index be measured without moving the serving pointer")
    args = parser.parse_args()
    if not args.out.is_absolute():
        parser.error("--out must be an absolute private directory")
    settings = load_settings() if args.run else load_settings(provider="fake")
    repo = Path(__file__).resolve().parents[1]
    if args.out.is_relative_to(repo) and not args.out.is_relative_to(settings.data_dir):
        parser.error("private comparison artifacts in this checkout must stay inside RFP_DATA_DIR")
    if args.run:
        with closing(service.Resources(settings, recover=True)) as resources:
            report = run(settings, resources, args.out, int(Decimal(args.max_cost_usd) * 1_000_000), args.index_version)
            print(json.dumps({"passed": report["passed"], "gates": report["gates"], "report": str(args.out / "quality.json")}))
            return 0 if report["passed"] else 1
    with store.database_lifecycle(settings.db_path):
        plan = prepare(settings, index_version=args.index_version)[0]
        store.write_text_atomic(args.out / "plan.json", store.dumps(plan))
        print(json.dumps({k: v for k, v in plan.items() if k != "frozen"}))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
