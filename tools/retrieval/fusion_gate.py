"""Fusion gate: keyword-only K1 against hybrid fusion settings on the fixed text-embedding-3-large/1536 set.

Populations: the frozen pilot (`dev`) on its own document scopes, the same questions over all documents, and the
frozen whole-corpus needle set (`corpus`) over all documents. A setting passes when, on the scoped pilot and on the
whole-corpus set (unscoped pilot plus needles), it has no critical failure K1 does not have and its nDCG@5 and
packed complete support are no lower than K1's. Query embeddings are the only paid work and go through the
authoritative ledger under an explicit ceiling.

python tools/retrieval/fusion_gate.py --out <absolute dir under RFP_DATA_DIR> [--run --max-cost-usd 0.05]
    [--variant rrf:60:1.0 --variant keyword_first:60:1.0 ...]   # fusion:rrf_k:dense_weight
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from contextlib import closing
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from rfp_assistant.evaluation import evaluation
from rfp_assistant.gateway import budget
from rfp_assistant.retrieval import dense
from rfp_assistant.retrieval.retrieval import KeywordIndex, index_compatibility
from rfp_assistant.service import service
from rfp_assistant.settings import load_settings
from rfp_assistant.storage import store

MEMBER = "claude-cutover"
DEFAULT_VARIANTS = ("rrf:60:1.0", "rrf:60:0.5", "rrf:60:0.25", "keyword_first:60:1.0")
DATASETS = ("dev", "corpus")


def parse_variant(text):
    fusion, rrf_k, weight, *head = text.split(":")
    return {"fusion": fusion, "rrf_k": int(rrf_k), "dense_weight": float(weight),
            "keyword_head": int(head[0]) if head else 20}


def variant_name(v):
    return f"{v['fusion']}:{v['rrf_k']}:{v['dense_weight']}" + (f":{v['keyword_head']}"
                                                                if v["fusion"] == "keyword_first" else "")


def evidence_limits(units, depth=None):
    """Evidence units with a token budget that scales with them (chunks average 266 tokens, at most 797)."""
    limits = {"evidence_max_units": units, "evidence_target_tokens": 400 * units,
              "evidence_max_tokens": 400 * units + 800}
    if depth:
        limits.update(channel_top_k=depth, fused_top_k=depth)
    return limits


def gate(candidate, baseline):
    """Zero new critical failures, nDCG@5 and packed complete support no lower than K1, with equal denominators."""
    new = sorted(set(candidate["critical_failures"]) - set(baseline["critical_failures"]), key=str)
    a, b = candidate["packed_complete"], baseline["packed_complete"]
    checks = {
        "no_new_critical_failures": not new,
        "ndcg_at_least_k1": candidate["ndcg@5"] is not None and baseline["ndcg@5"] is not None
        and candidate["ndcg_eligible_rows"] == baseline["ndcg_eligible_rows"] > 0
        and candidate["ndcg@5"] >= baseline["ndcg@5"],
        "complete_support_at_least_k1": a["denominator"] == b["denominator"] > 0 and a["numerator"] >= b["numerator"],
        "no_wrong_scope": candidate["wrong_scope_candidates"] == 0,
        "no_fallback": not candidate["fallbacks"],
    }
    return {"passed": all(checks.values()), "checks": checks, "new_critical_failures": new,
            "ndcg@5": [candidate["ndcg@5"], baseline["ndcg@5"]],
            "packed_complete": [f"{a['numerator']}/{a['denominator']}", f"{b['numerator']}/{b['denominator']}"]}


def needle_hits(traces):
    """Target passage within the top 5 and top 10 of the single whole-corpus ranking, with Wilson 95% intervals."""
    rows = [t for t in traces if t.get("metrics")]
    out = {"denominator": len(rows)}
    for k in (5, 10):
        n = sum(t["metrics"][f"hit@{k}"] for t in rows)
        out[f"top{k}"] = {"numerator": n, "rate": round(n / len(rows), 4) if rows else None,
                          "wilson95": evaluation.wilson(n, len(rows))}
    out["missed_top10"] = [t["id"] for t in rows if not t["metrics"]["hit@10"]]
    return out


def job_cost(conn, request_id):
    return conn.execute("SELECT COALESCE(SUM(CASE WHEN state='settled' THEN settled_micro_usd "
                        "WHEN state IN ('reserved','dispatching','unknown') THEN reserved_micro_usd "
                        "ELSE 0 END),0) FROM attempts WHERE request_id=?", (request_id,)).fetchone()[0]


def prepare(settings, analyzer=None, datasets=DATASETS):
    if (settings.embedding_model, settings.embedding_dimensions) != ("text-embedding-3-large", 1536):
        raise ValueError("serving candidate is fixed at text-embedding-3-large/1536")
    populations, identities = {}, {}
    for name in datasets:
        frozen = evaluation.frozen_dataset(settings, name)
        if not frozen or not frozen["current"]:
            raise ValueError(f"freeze the independently reviewed {name} dataset first")
        if not evaluation.validate_gold(settings, name)["ok"]:
            raise ValueError(f"frozen {name} source/review validation failed")
        rows, skipped, sha = evaluation.load_eval_rows(settings, name)
        if skipped or len(rows) != frozen["rows"]:
            raise ValueError(f"every frozen {name} row must remain eligible")
        populations[name] = rows
        identities[name] = {"dataset_sha256": sha, "population_sha256": evaluation.population_identity(rows, skipped),
                            "rows": len(rows), "passage_rows": sum(map(evaluation.is_passage_row, rows))}
    index = KeywordIndex.load(settings)  # the active pointer
    outdated = index_compatibility(index, analyzer)
    if outdated:
        raise ValueError(outdated)
    # Every frozen scope must be indexed: an absent one ranks empty for every system and passes unmeasured.
    unindexed = [evaluation.row_id(r) for rows in populations.values() for r in rows
                 if evaluation.is_passage_row(r) and not all(index.rows_by_extraction.get(x)
                                                             for _, x in evaluation.row_scope(r))]
    if unindexed:
        raise ValueError(f"frozen passage scopes are absent from the active keyword index: {unindexed[:5]}")
    version = evaluation._ready_dense_for(settings, index.version)
    if not version:
        raise ValueError("complete fixed-dimension corpus is required")
    candidate = dense.DenseIndex.load(settings, version, base=index)
    # Price with the ledger snapshot that reservations enforce, never the code default.
    ledger = dense._ledger_view(settings, "gold_eval")
    if ledger["rates"] is None:
        raise ValueError(f"no configured ledger rate for {settings.embedding_model}")
    questions = {r["question"] for rows in populations.values() for r in rows if evaluation.is_passage_row(r)}
    missing = [q for q in sorted(questions) if dense.cache_get(settings, dense.payload_hash(
        dense.normalize_payload(q), settings.embedding_model, settings.embedding_dimensions)) is None]
    query_max = sum(budget.max_cost(ledger["rates"], dense.count_embedding_tokens(q, settings.embedding_model)
                                    + dense.QUERY_MARGIN_TOKENS, 0) for q in missing)
    plan = {"version": "fusion-gate-plan-1", "model": settings.embedding_model, "dimensions": 1536,
            "datasets": identities, "index_version": index.version, "index_manifest_hash": index.manifest_hash,
            "dense_version": candidate.version, "dense_search": settings.dense_search,
            "limits": {k: getattr(settings, k) for k in ("channel_top_k", "fused_top_k", "evidence_target_tokens",
                                                         "evidence_max_tokens", "evidence_max_units")},
            "query_misses": len(missing), "query_max_micro_usd": query_max, "rate_version": ledger["rate_version"],
            "fits_envelope": query_max <= min(ledger["envelope_remaining_micro_usd"], ledger["available_micro_usd"])}
    return plan, populations, index, candidate


def systems_for(settings, variants, units=None, depth=None, reranker=None):
    """(label, mode, settings, baseline label). Each hybrid system is gated against K1 at the same depth and
    evidence limits; without `units` this is the single comparison at the configured limits."""
    out = []
    for u in units or [None]:
        s = settings.with_(**evidence_limits(u, depth)) if u else settings
        tag = f"@{u}" if u else ""
        out.append((f"K1{tag}", "kiwi_bm25", s, None))
        for v in variants:
            out.append((variant_name(v) + tag, "hybrid", s.with_(**v), f"K1{tag}"))
            if reranker is not None:
                out.append((variant_name(v) + "+rerank" + tag, "hybrid_rerank", s.with_(**v), f"K1{tag}"))
    return out


def _query_vectors(settings, transport, populations, job, guard):
    """One paid query vector per passage row, through the ledger; any unavailable or unknown-billed one stops."""
    vectors = {}
    for rows in populations.values():
        for row in rows:
            if evaluation.is_passage_row(row):
                vector, info = dense.query_vector(settings, transport, row["question"], request_id=job,
                                                  member_id=MEMBER, purpose="gold_eval", allow_paid=True, guard=guard)
                if vector is None or info.get("billing") == "unknown":
                    raise RuntimeError(f"query vector unavailable or billing unknown ({info}); stop without retry")
                vectors[evaluation.row_id(row)] = vector
    return vectors


def _measure(systems, runs, index, analyzer, vectors, candidate, reranker, out):
    """Every system over every population, each trace written; the aggregates per system and population."""
    traces, aggregates = {}, {}
    for label, mode, s, _ in systems:
        for population, rows, unscoped in runs:
            traces[population] = evaluation._execute(
                s, index, analyzer, rows, mode, vectors=vectors,
                dense=candidate if mode != "kiwi_bm25" else None, unscoped=unscoped,
                reranker=reranker if mode == "hybrid_rerank" else None,
                rerank_depth=s.fused_top_k if mode == "hybrid_rerank" else None)
            store.write_jsonl_atomic(out / f"{label.replace(':', '_')}-{population}.jsonl", traces[population])
            aggregates[f"{label}/{population}"] = evaluation.aggregate(traces[population], [])
        whole = traces["pilot-unscoped"] + traces.get("needles", [])
        aggregates[f"{label}/whole-corpus"] = evaluation.aggregate(whole, [])
        if "needles" in traces:
            aggregates[f"{label}/needle-hits"] = needle_hits(traces["needles"])
    return aggregates


def _gates(systems, aggregates):
    """Each non-baseline system against its K1 baseline, on the scoped pilot and the whole corpus."""
    gates = {}
    for label, _, _, base in systems:
        if base is None:
            continue
        gates[label] = {p: gate(aggregates[f"{label}/{p}"], aggregates[f"{base}/{p}"])
                        for p in ("pilot-scoped", "whole-corpus")}
        gates[label]["passed"] = all(g["passed"] for g in gates[label].values())
    return gates


def run(settings, resources, out, max_cost_micro, variants, datasets=DATASETS, units=None, depth=None,
        reranker=None):
    plan, populations, index, candidate = prepare(settings, resources.analyzer, datasets)
    if not plan["fits_envelope"] or plan["query_max_micro_usd"] > max_cost_micro:
        raise ValueError("query estimate exceeds the existing envelope or the explicit ceiling")
    if dense.unresolved_attempts(settings, "gold_eval"):
        raise ValueError("unknown embedding billing blocks comparison dispatch")
    store.write_text_atomic(out / "plan.json", store.dumps(plan))
    key = "fusion-gate:" + hashlib.sha256(store.dumps(plan["datasets"]).encode()).hexdigest()[:16]
    job = dense.ensure_job_request(settings, MEMBER, key, plan)
    with store.open_db(settings.db_path) as conn:
        previous = job_cost(conn, job)

    def guard(conn):  # inside durable dispatch: concurrent reservations and snapshotted prices count
        return "comparison_invocation_cost_ceiling" if job_cost(conn, job) - previous > max_cost_micro else None

    vectors = _query_vectors(settings, resources.transport, populations, job, guard)
    runs = [("pilot-scoped", populations["dev"], False), ("pilot-unscoped", populations["dev"], True)]
    if "corpus" in populations:
        runs.append(("needles", populations["corpus"], True))
    systems = systems_for(settings, variants, units, depth, reranker)
    aggregates = _measure(systems, runs, index, resources.analyzer, vectors, candidate, reranker, out)
    gates = _gates(systems, aggregates)
    complete = tuple(datasets) == DATASETS
    passing = [label for label in gates if gates[label]["passed"]]
    limits = {label: s for label, _, s, _ in systems}

    def merit(label):  # most complete support, then ranking quality, then the smaller (cheaper) evidence set
        support = lambda p: aggregates[f"{label}/{p}"]["packed_complete"]["numerator"]  # noqa: E731
        return (support("whole-corpus"), support("pilot-scoped"), aggregates[f"{label}/whole-corpus"]["ndcg@5"],
                aggregates[f"{label}/pilot-scoped"]["ndcg@5"], -limits[label].evidence_max_units)

    best = max(passing, key=merit, default=None)
    for name in datasets:
        if evaluation.frozen_dataset(settings, name)["dataset_sha256"] != plan["datasets"][name]["dataset_sha256"]:
            raise ValueError(f"{name} changed during comparison; acceptance refused")
    with store.open_db(settings.db_path) as conn:
        attempts = conn.execute("SELECT state,COUNT(*),COALESCE(SUM(settled_micro_usd),0) FROM attempts "
                                "WHERE request_id=? GROUP BY state", (job,)).fetchall()
    report = {"version": "fusion-gate-1", "complete": complete, "passed": complete and best is not None,
              "selected": best if complete else None, "best_measured": best, "checked_at": store.utcnow(),
              "plan": plan, "variants": [variant_name(v) for v in variants], "gates": gates,
              "systems": {label: {"mode": mode, "baseline": base, "rerank": mode == "hybrid_rerank",
                                  **{k: getattr(s, k) for k in ("channel_top_k", "fused_top_k", "evidence_max_units",
                                                                "evidence_target_tokens", "evidence_max_tokens")}}
                          for label, mode, s, base in systems},
              "reranker": getattr(reranker, "info", None),
              "aggregates": aggregates, "eval_version": evaluation.EVAL_VERSION,
              "billing": {"request_id": job, "attempts": {r[0]: {"count": r[1], "micro_usd": r[2]} for r in attempts}},
              "trace_hashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.glob("*.jsonl"))}}
    store.write_text_atomic(out / "quality.json", store.dumps(report))
    dense.finish_job_request(settings, job, "completed", {"passed": report["passed"], "report": str(out / "quality.json")})
    return report


def summary(report):
    lines = []
    for key, agg in report["aggregates"].items():
        if key.endswith("needle-hits"):
            lines.append(f"{key}: top5 {agg['top5']['numerator']}/{agg['denominator']} {agg['top5']['wilson95']}, "
                         f"top10 {agg['top10']['numerator']}/{agg['denominator']} {agg['top10']['wilson95']}")
        else:
            pc = agg["packed_complete"]
            lines.append(f"{key}: nDCG@5 {agg['ndcg@5']} (n={agg['ndcg_eligible_rows']}), packed complete "
                         f"{pc['numerator']}/{pc['denominator']}, critical {agg['critical_failures']}")
    for label, g in report["gates"].items():
        lines.append(f"gate {label}: {'PASS' if g['passed'] else 'FAIL'} " + "; ".join(
            f"{p} new critical {g[p]['new_critical_failures']} nDCG {g[p]['ndcg@5']} support {g[p]['packed_complete']}"
            for p in ("pilot-scoped", "whole-corpus")))
    lines.append(f"complete: {report['complete']}, selected: {report['selected']}, best measured: {report['best_measured']}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--max-cost-usd", default="0")
    parser.add_argument("--variant", action="append", help="fusion:rrf_k:dense_weight (repeatable)")
    parser.add_argument("--pilot-only", action="store_true",
                        help="exploration before the needle set is frozen; never passes the gate")
    parser.add_argument("--units", help="comma-separated evidence unit counts to measure, e.g. 10,15,20,25,30")
    parser.add_argument("--depth", type=int, help="candidates per channel and after fusion, e.g. 50")
    parser.add_argument("--rerank-revision", help="also measure each hybrid variant with the pinned local reranker")
    args = parser.parse_args()
    if not args.out.is_absolute():
        parser.error("--out must be an absolute private directory")
    settings = load_settings() if args.run else load_settings(provider="fake")
    repo = Path(__file__).resolve().parents[2]
    if args.out.is_relative_to(repo) and not args.out.is_relative_to(settings.data_dir):
        parser.error("private comparison artifacts in this checkout must stay inside RFP_DATA_DIR")
    variants = [parse_variant(v) for v in (args.variant or DEFAULT_VARIANTS)]
    datasets = ("dev",) if args.pilot_only else DATASETS
    units = [int(u) for u in args.units.split(",")] if args.units else None
    if args.run:
        reranker = None
        if args.rerank_revision:
            reranker, info = dense.load_reranker(settings.with_(reranker_revision=args.rerank_revision))
            if reranker is None:
                raise SystemExit(f"reranker unavailable: {info}")
        with closing(service.Resources(settings, recover=True)) as resources:
            report = run(settings, resources, args.out, int(Decimal(args.max_cost_usd) * 1_000_000), variants,
                         datasets, units, args.depth, reranker)
            print(summary(report))
            print(json.dumps({"passed": report["passed"], "selected": report["selected"],
                              "report": str(args.out / "quality.json")}))
            return 0 if report["passed"] else 1
    with store.database_lifecycle(settings.db_path):
        plan = prepare(settings, datasets=datasets)[0]
        store.write_text_atomic(args.out / "plan.json", store.dumps(plan))
        print(json.dumps(plan))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
