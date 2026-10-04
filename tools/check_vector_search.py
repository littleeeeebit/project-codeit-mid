"""HNSW against exact pgvector search on the fixed text-embedding-3-large/1536 serving set.

Builds the HNSW index (idempotent), then for every frozen pilot (`dev`) and whole-corpus (`corpus`) passage question,
on its own document scope and on all documents, compares the HNSW top 20 at several `hnsw.ef_search` values with the
exact top 20. HNSW may serve only when every group's mean recall@20 is at least 0.99; otherwise exact search stays.
Also records EXPLAIN ANALYZE plans and warm p50/p95 latency for one and six concurrent clients. Cached query vectors
only: nothing is paid.

python tools/check_vector_search.py --out <absolute dir under RFP_DATA_DIR>
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rfp_assistant import dense, evaluation, store, vector_store
from rfp_assistant.retrieval import KeywordIndex, corpus_scope, scope_rows
from rfp_assistant.settings import load_settings

K = 20
EF_VALUES = (40, 100, 200, 400)
RECALL_FLOOR = 0.99
DATASETS = ("dev", "corpus")


def queries(settings, index):
    """(group, row id, vector, allowed rows) for every frozen passage question, scoped and over all documents."""
    everything = scope_rows(index, corpus_scope(settings, index))
    out = []
    for name in DATASETS:
        frozen = evaluation.frozen_dataset(settings, name)
        if not frozen or not frozen["current"]:
            raise ValueError(f"freeze the {name} dataset first")
        rows, _, _ = evaluation.load_eval_rows(settings, name)
        for row in rows:
            if not evaluation.is_passage_row(row):
                continue
            vector, info = dense.query_vector(settings, None, row["question"], request_id=None, member_id="hnsw-check",
                                              purpose="gold_eval", allow_paid=False)
            if vector is None:
                raise ValueError(f"query vector not cached for {evaluation.row_id(row)}; run the fusion gate first")
            out.append((f"{name}/scoped", evaluation.row_id(row), vector, scope_rows(index, evaluation.row_scope(row))))
            out.append((f"{name}/unscoped", evaluation.row_id(row), vector, everything))
    return out


def recall(settings, candidate, qs):
    """Tie-aware recall@20: an HNSW row counts when its exact cosine reaches the exact 20th score (chunks sharing one
    payload tie, and exact search orders ties by chunk ID)."""
    exact = {(g, rid): candidate.search(v, allowed, K, settings) for g, rid, v, allowed in qs}
    result = {}
    for ef in EF_VALUES:
        s = settings.with_(dense_search="hnsw", hnsw_ef_search=ef)
        groups: dict[str, list[float]] = {}
        for g, rid, v, allowed in qs:
            truth = exact[(g, rid)]
            got = candidate.search(v, allowed, K, s)
            floor = truth[-1][1] - 1e-6 if truth else 0
            groups.setdefault(g, []).append(min(1.0, sum(sc >= floor for _, sc in got) / len(truth)) if truth else 1.0)
        result[ef] = {g: {"n": len(x), "mean_recall@20": round(sum(x) / len(x), 4), "min": round(min(x), 4),
                          "full": sum(r == 1.0 for r in x)} for g, x in sorted(groups.items())}
        result[ef]["passes"] = all(v["mean_recall@20"] >= RECALL_FLOOR for v in result[ef].values())
    return result


def explain(settings, candidate, qs):
    plans = {}
    for g, _, v, allowed in (next(q for q in qs if q[0] == "dev/scoped"), next(q for q in qs if q[0] == "dev/unscoped")):
        vector = vector_store.verified(v, candidate.dims)
        with store.open_db(settings.db_path) as conn:
            exact = conn.execute("EXPLAIN (ANALYZE, BUFFERS) " + vector_store.EXACT_SQL,
                                 (vector, candidate.version, allowed, vector, K)).fetchall()
            with store.tx(conn):
                conn.execute(vector_store.HNSW_SESSION, ("100",))
                hnsw = conn.execute("EXPLAIN (ANALYZE, BUFFERS) " + vector_store.HNSW_SQL.format(d=candidate.dims),
                                    (vector, candidate.version, allowed, vector, K)).fetchall()
        plans[g] = {"exact": [r[0] for r in exact], "hnsw_ef100": [r[0] for r in hnsw]}
    return plans


def latency(settings, candidate, qs, clients):
    def one(q):
        t = time.perf_counter()
        candidate.search(q[2], q[3], K, settings)
        return (time.perf_counter() - t) * 1000

    for q in qs[:10]:  # warm the buffer cache and pool
        one(q)
    out = {}
    for group in sorted({q[0] for q in qs}):
        work = [q for q in qs if q[0] == group]
        with ThreadPoolExecutor(clients) as pool:
            ms = list(pool.map(one, work * 2))
        out[group] = {"n": len(ms), "p50_ms": dense.percentile(ms, 0.5), "p95_ms": dense.percentile(ms, 0.95)}
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    settings = load_settings(provider="fake")
    if not args.out.is_absolute() or not args.out.resolve().is_relative_to(settings.data_dir.resolve()):
        parser.error("--out must be an absolute private directory inside RFP_DATA_DIR")
    with store.database_lifecycle(settings.db_path):
        index = KeywordIndex.load(settings)
        version = evaluation._ready_dense_for(settings, index.version)
        if not version:
            raise SystemExit("complete fixed-dimension corpus is required")
        candidate = dense.DenseIndex.load(settings, version, base=index)
        built = vector_store.build_hnsw(settings)
        qs = queries(settings, index)
        measured = recall(settings, candidate, qs)
        passing = [ef for ef in EF_VALUES if measured[ef]["passes"]]
        chosen = min(passing) if passing else None
        serving = settings.with_(dense_search="hnsw", hnsw_ef_search=chosen) if chosen else settings
        report = {"version": "vector-search-check-1", "checked_at": store.utcnow(), "dense_version": version,
                  "index_version": index.version, "k": K, "recall_floor": RECALL_FLOOR, "hnsw": built,
                  "recall": measured, "selected": {"dense_search": "hnsw", "hnsw_ef_search": chosen} if chosen
                  else {"dense_search": "exact", "reason": "no ef_search value reached mean recall@20 >= 0.99 "
                                                           "in every group"},
                  "explain_analyze": explain(settings, candidate, qs),
                  "latency_ms": {mode: {f"{c}_clients": latency(s, candidate, qs, c) for c in (1, 6)}
                                 for mode, s in (("exact", settings.with_(dense_search="exact")),
                                                 ("hnsw", serving if chosen else settings.with_(dense_search="hnsw")))}}
    args.out.mkdir(parents=True, exist_ok=True)
    store.write_text_atomic(args.out / "vector-search.json", store.dumps(report))
    print(json.dumps({"selected": report["selected"], "recall": measured, "latency_ms": report["latency_ms"],
                      "hnsw": built}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
