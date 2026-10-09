"""Comparison runner: one declared axis matrix, every row measured, one table the person picks from.

A matrix declares axes (chunk profile, analyzer, retrieval mode, embedding model, fusion, reranker and rerank mode,
evidence units, depth); every axis it does not name stays at the serving run's value, so one row differs from the
serving configuration in what the matrix varies and nothing else. Each row is measured on three populations: the
development set on its own document scopes (`dev`), the needle set over all documents (`needles`) and the
development questions over all documents plus the needles (`whole`). The development measurement is also written
as an ordinary retrieval run, so any row the person picks can be activated (`activate-run`, or the 실험 비교 view).

Nothing here changes what serves. Indexes, vectors and reranker scores are cached and reused. A paid step (OpenAI
or Gemini embeddings) runs only under an estimate the person approved; without one the row reports the estimate.
A model that fails to load, runs out of memory or crashes is a row with its reason.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..retrieval import dense as dense_mod
from . import evaluation as ev
from . import judge_set
from ..retrieval.models import EMBEDDINGS, GEMINI_PROVIDER, RERANKERS, ModelError, embedding_spec, free_gpu, model_size_bytes, reranker_spec
from ..settings import Settings
from ..storage.store import dumps, open_db, tx, utcnow, write_text_atomic

COMPARE_VERSION = "compare-1"
MEMBER = "owner-compare"
PROTECTED_HEAD = 6  # below-the-head rerank mode keeps the BM25 head the serving fusion protects
LOAD_USERS = ev.LOAD_USERS

SERVING_FUSION = "keyword_first:60:1.0:6"
MATRICES: dict[str, dict] = {
    "lexical": {"title": "K0 / K1", "axes": {"analyzer": ["whitespace", "kiwi"]},
                "fixed": {"retrieval": "keyword"}},
    "chunking": {"title": "청킹", "axes": {"profile": ["structural", "fixed-256-32", "fixed-512-64", "fixed-800-96"]},
                 "fixed": {"retrieval": "keyword", "analyzer": "kiwi"}},
    "embedding": {"title": "임베딩", "axes": {"embedding": list(EMBEDDINGS), "retrieval": ["dense", "hybrid"]},
                  "fixed": {"fusion": SERVING_FUSION, "depth": 50, "units": 10}},
    # Rerankers score the serving hybrid's frozen pool: its embedding, fusion, depth and units come from the
    # activated run (`serving_base`), never from a fixed model, and `Runner.run` records them as the fixed values.
    "reranker": {"title": "리랭커", "axes": {"reranker": list(RERANKERS), "rerank_mode": ["whole", "below_head"]},
                 "fixed": {"retrieval": "hybrid_rerank"}},
}
SERVING_HYBRID = ("embedding", "fusion", "depth", "units")
# The limits each axis derives. A row keeps the activated run's recorded value of every limit whose axis it leaves at
# the serving value, so varying one axis never moves another's limits; the search limits belong to no axis and always
# come from the serving run.
AXIS_LIMITS = {"fusion": ("fusion", "rrf_k", "dense_weight", "keyword_head"),
               "depth": ("channel_top_k", "fused_top_k"),
               "units": ("evidence_max_units", "evidence_target_tokens", "evidence_max_tokens")}
UNTIED_LIMITS = ("dense_search", "hnsw_ef_search")
SERVING_LIMITS = tuple(k for keys in AXIS_LIMITS.values() for k in keys) + UNTIED_LIMITS
AXES = ("profile", "analyzer", "retrieval", "embedding", "fusion", "reranker", "rerank_mode", "units", "depth")
COLUMNS = {  # (key, label, better: "high" | "low" | None) in display order
    "lexical": [("dev.ndcg", "nDCG@5 (dev)", "high"), ("dev.support", "complete support (dev)", "high"),
                ("whole.ndcg", "nDCG@5 (whole)", "high"), ("whole.support", "complete support (whole)", "high"),
                ("needle.top5", "needle top-5", "high"), ("dev.critical", "critical (dev)", "low"),
                ("whole.critical", "critical (whole)", "low"), ("dev.qualifier_losses", "qualifier losses", "low"),
                ("dev.p95_ms", "latency p95 ms", "low")],
    "chunking": [("chunks", "chunks", None), ("duplicated_tokens", "duplicated tokens", "low"),
                 ("dev.ndcg", "nDCG@5", "high"), ("dev.support", "complete support", "high"),
                 ("dev.qualifier_losses", "qualifier losses", "low"), ("dev.critical", "critical", "low"),
                 ("dev.p95_ms", "latency p95 ms", "low"), ("whole.ndcg", "nDCG@5 (whole)", "high"),
                 ("needle.top5", "needle top-5", "high")],
    "embedding": [("dev.ndcg", "nDCG@5", "high"), ("dev.support", "complete support", "high"),
                  ("needle.top5", "needle top-5", "high"), ("whole.ndcg", "nDCG@5 (whole)", "high"),
                  ("new_critical_vs_k1", "critical vs K1", "low"), ("truncated_chunks", "truncated chunks", "low"),
                  ("query_p50_ms", "query p50 ms", "low"), ("query_p95_ms", "query p95 ms", "low"),
                  ("corpus_seconds", "corpus embed s", "low"), ("storage_mb", "storage MB", "low"),
                  ("cost_usd", "cost USD", "low"), ("licence", "licence", None)],
    "reranker": [("dev.ndcg", "nDCG@5", "high"), ("dev.support", "complete support", "high"),
                 ("recall20_before", "recall@20 before", "high"), ("dev.recall20", "recall@20 after", "high"),
                 ("new_critical_vs_h", "critical vs hybrid", "low"), ("new_critical_vs_k1", "critical vs K1", "low"),
                 ("truncated_pairs", "truncated pairs", "low"), ("p95_alone_ms", "warm p95 alone ms", "low"),
                 ("p95_loaded_ms", f"warm p95 {LOAD_USERS} users ms", "low"), ("gate", "gate", None),
                 ("needle.top5", "needle top-5", "high"), ("whole.ndcg", "nDCG@5 (whole)", "high")],
}


COLUMNS["regression"] = COLUMNS["lexical"] + [("new_critical_vs_k1", "critical vs K1", "low")]


class CompareError(RuntimeError):
    pass


def compare_dir(settings: Settings) -> Path:
    return settings.data_dir / "compare"


# ---------------------------------------------------------------- matrix and rows


def load_matrix(name_or_path: str) -> tuple[str, dict]:
    """A built-in matrix by name, or a JSON file {"name", "title", "axes": {axis: [values]}, "fixed": {...}}."""
    if name_or_path in MATRICES:
        return name_or_path, MATRICES[name_or_path]
    path = Path(name_or_path)
    if not path.is_absolute() or not path.exists():
        raise CompareError(f"unknown matrix {name_or_path!r}: use {sorted(MATRICES)} or an absolute JSON path")
    spec = json.loads(path.read_text(encoding="utf-8"))
    unknown = (set(spec.get("axes") or {}) | set(spec.get("fixed") or {})) - set(AXES)
    if unknown or not spec.get("axes"):
        raise CompareError(f"a matrix declares axes among {AXES}; unknown: {sorted(unknown)}")
    return spec.get("name") or path.stem, spec


def serving_base(settings: Settings) -> dict:
    """The serving run's value of every axis: what a row keeps unless its matrix varies it."""
    from ..service.service import active_serving

    cfg = active_serving(settings)
    limits = cfg.get("limits") or {}
    fusion = (f"{limits.get('fusion', settings.fusion)}:{limits.get('rrf_k', settings.rrf_k)}:"
              f"{float(limits.get('dense_weight', settings.dense_weight))}:{limits.get('keyword_head', settings.keyword_head)}")
    rr = cfg.get("reranker") or {}
    profile = "structural"
    if cfg.get("index_version"):
        with open_db(settings.db_path) as conn:
            row = conn.execute("SELECT config_json FROM indexes WHERE index_version = ?",
                               (cfg["index_version"],)).fetchone()
        if row is not None:
            profile = json.loads(row[0]).get("profile") or profile
    base = {"profile": profile, "analyzer": "kiwi", "retrieval": {"kiwi_bm25": "keyword", "dense": "dense",
            "hybrid": "hybrid", "hybrid_rerank": "hybrid_rerank"}.get(cfg["mode"], "keyword"),
            "embedding": (cfg.get("embedding") or {}).get("model") or "text-embedding-3-large",
            "fusion": fusion, "reranker": rr.get("model"), "rerank_mode": "below_head" if rr.get("protect") else "whole",
            "units": limits.get("evidence_max_units", settings.evidence_max_units),
            "depth": limits.get("fused_top_k", settings.fused_top_k)}
    # Not an axis: the serving run's own keyword index and recorded limits. A row on the serving profile, fusion,
    # depth and units keeps them (`Runner.index`, `row_settings`), so the serving hybrid's pool is the one served.
    base["serving"] = {"index_version": cfg.get("index_version"), "profile": profile,
                       **{a: base[a] for a in ("fusion", "depth", "units")},
                       "limits": {k: limits[k] for k in SERVING_LIMITS if k in limits}}
    return base


def matrix_rows(spec: dict, base: dict) -> list[dict]:
    axes = spec["axes"]
    rows = []
    for values in itertools.product(*axes.values()):
        row = {**base, **(spec.get("fixed") or {}), **dict(zip(axes, values))}
        if row["retrieval"] != "hybrid_rerank":
            row["reranker"] = row["rerank_mode"] = None
        if row["retrieval"] == "keyword":
            row["embedding"] = None
        rows.append(row)
    return rows


def row_label(row: dict) -> str:
    if row["retrieval"] == "keyword":
        return "K0" if row["analyzer"] == "whitespace" else "K1"
    return {"dense": "D", "hybrid": "H", "hybrid_rerank": "HR"}[row["retrieval"]]


def row_name(row: dict, matrix: dict) -> str:
    varied = list(matrix["axes"])
    return " · ".join(str(row[a]) for a in varied)


def row_settings(settings: Settings, row: dict) -> Settings:
    fusion, rrf_k, weight, head = row["fusion"].split(":")
    units = int(row["units"])
    changes = {"fusion": fusion, "rrf_k": int(rrf_k), "dense_weight": float(weight), "keyword_head": int(head),
               "channel_top_k": int(row["depth"]), "fused_top_k": int(row["depth"]), "evidence_max_units": units,
               "evidence_target_tokens": 400 * units, "evidence_max_tokens": 400 * units + 800}
    if row.get("embedding"):
        spec = embedding_spec(row["embedding"])
        changes.update(embedding_model=spec.key, embedding_dimensions=spec.dims)
    if row.get("reranker"):
        r = reranker_spec(row["reranker"])
        changes.update(reranker_model=r.key, reranker_revision=r.revision, reranker_max_length=r.max_length,
                       reranker_precision=r.precision)
    served = row.get("serving") or {}
    recorded = served.get("limits") or {}
    keep = list(UNTIED_LIMITS) + [k for axis, keys in AXIS_LIMITS.items() if row[axis] == served.get(axis)
                                  for k in keys]
    changes.update({k: recorded[k] for k in keep if k in recorded})
    return settings.with_(**changes)


# ---------------------------------------------------------------- populations and artifacts


def populations(settings: Settings, index=None) -> dict:
    """dev (scoped), needles (unscoped) and their identities. `whole` is dev unscoped plus the needles. With an
    index, the questions are those pinned to the extractions it holds (`load_eval_rows`)."""
    out = {}
    for name in ("dev", "corpus"):
        rows, skipped, sha = ev.load_eval_rows(settings, name,
                                               extractions=index.source_extraction if index is not None else None)
        out[name] = {"rows": rows, "skipped": skipped, "dataset_sha256": sha,
                     "population_sha256": ev.population_identity(rows, skipped)}
    return out


def keyword_index(settings: Settings, analyzer, profile: str):
    """The profile's keyword index over the active index's source policy, built once and never activated."""
    from ..retrieval.retrieval import KeywordIndex, build_keyword_index
    from ..storage.store import get_app_setting

    with open_db(settings.db_path) as conn:
        active = get_app_setting(conn, "active_index")
    current = KeywordIndex.load(settings, active)
    if current.profile == profile:
        return current
    built = build_keyword_index(settings, analyzer, include_unreviewed=current.review_scope != "reviewed_only",
                                profile=profile, activate=False)
    return KeywordIndex.load(settings, built["index_version"])


def _build_record(settings: Settings, version: str) -> Path:
    return compare_dir(settings) / "builds" / f"{version}.json"


def ensure_dense(settings: Settings, s: Settings, index, approvals: dict, transport) -> tuple[object, dict]:
    """The row model's ready set over this index: reused, built locally (free), or built under an approved
    estimate (OpenAI ledger or Gemini cap). Returns (index or None, build facts or the estimate to approve)."""
    version = ev._ready_dense_for(s, index.version)
    backend = embedding_spec(s.embedding_model).backend
    if version is None:
        if backend == "local":
            facts = dense_mod.build_local_dense(s, index.version)
            write_text_atomic(_build_record(settings, facts["dense_version"]), dumps(facts))
            version = facts["dense_version"]
        else:
            estimate = paid_estimate(settings, s, index)
            approved = approvals.get(estimate["estimate_id"])
            if not approved:
                return None, {"needs_approval": estimate}
            facts = _build_paid(settings, s, index, estimate, transport)
            if facts.get("status") != "ready":
                return None, {"error": f"paid build stopped: {facts.get('reason')}", "estimate": estimate}
            write_text_atomic(_build_record(settings, facts["dense_version"]), dumps(facts))
            version = facts["dense_version"]
    record = _build_record(settings, version)
    facts = json.loads(record.read_text(encoding="utf-8")) if record.exists() else {
        "dense_version": version, "note": "existing vectors; built before the comparison runner"}
    return dense_mod.DenseIndex.load(s, version, base=index), facts


# ---------------------------------------------------------------- paid estimates


def _estimates_dir(settings: Settings) -> Path:
    return compare_dir(settings) / "estimates"


def _missing_queries(s: Settings, pops: dict) -> list[str]:
    from ..retrieval.vector_store import cached_hashes

    questions = sorted({r["question"] for p in pops.values() for r in p["rows"] if ev.is_passage_row(r)})
    hashes = {dense_mod.payload_hash(dense_mod.normalize_payload(q), s.embedding_model, s.embedding_dimensions,
                                     "query"): q for q in questions}
    have = cached_hashes(s, hashes)
    return [q for h, q in hashes.items() if h not in have]


def paid_estimate(settings: Settings, s: Settings, index, pops: dict | None = None) -> dict:
    """The priced plan of one paid model over this index: corpus payloads and comparison queries not yet cached.
    Its ID binds the model, index manifest and missing payload set; any change makes a new estimate."""
    from ..gateway import budget

    pops = pops or populations(settings, index)
    queries = _missing_queries(s, pops)
    backend = embedding_spec(s.embedding_model).backend
    if backend == "openai":
        corpus = dense_mod.plan_embeddings(s, index.version)
        ledger = dense_mod._ledger_view(s, "gold_eval")
        q_tokens = sum(dense_mod.count_embedding_tokens(q, s.embedding_model) + dense_mod.QUERY_MARGIN_TOKENS
                       for q in queries)
        q_cost = budget.max_cost(ledger["rates"], q_tokens, 0) if ledger["rates"] else None
        plan = {"backend": "openai", "ledger": "shared OpenAI allowance (budget-report)",
                "corpus_tokens": corpus["tokens_to_embed"], "corpus_micro_usd": corpus["max_cost_micro_usd"],
                "corpus_estimate_id": corpus["estimate_id"], "fits_envelope": corpus["fits_envelope"],
                "query_tokens": q_tokens, "query_micro_usd": q_cost, "payloads_to_embed": corpus["payloads_to_embed"],
                "fingerprint": corpus["fingerprint"]}
    else:
        from ..retrieval.models import external_status, gemini_cost_micro

        corpus = dense_mod.plan_gemini(s, index.version)
        client = dense_mod._gemini_client(s)
        try:
            q_each = [client.count_tokens([q]) for q in queries]  # one call per query, each rounded up
        finally:
            client.close()
        q_tokens = sum(q_each)
        plan = {"backend": "gemini", "ledger": "Gemini cap set by the person (compare-cap)",
                "corpus_tokens": corpus["tokens_to_embed"], "corpus_micro_usd": corpus["max_cost_micro_usd"],
                "query_tokens": q_tokens, "query_micro_usd": sum(map(gemini_cost_micro, q_each)),
                "payloads_to_embed": corpus["payloads_to_embed"], "fingerprint": corpus["fingerprint"],
                "cap": external_status(s.db_path)}
    plan.update(model=s.embedding_model, dims=s.embedding_dimensions, index_version=index.version,
                queries_to_embed=len(queries), queries=[dense_mod.normalize_payload(q) for q in queries],
                total_micro_usd=(plan["corpus_micro_usd"] or 0) + (plan["query_micro_usd"] or 0))
    plan["estimate_id"] = "est-" + hashlib.sha256(dumps([plan["model"], plan["index_version"], plan["fingerprint"],
                                                          sorted(queries)]).encode()).hexdigest()[:12]
    path = _estimates_dir(settings) / f"{plan['estimate_id']}.json"
    if path.exists():  # keep the approval of an unchanged estimate
        stored = json.loads(path.read_text(encoding="utf-8"))
        plan["approved_by"], plan["approved_at"] = stored.get("approved_by"), stored.get("approved_at")
    plan["created_at"] = utcnow()
    write_text_atomic(path, dumps(plan))
    return plan


def approve_estimate(settings: Settings, estimate_id: str, approved_by: str) -> dict:
    path = _estimates_dir(settings) / f"{estimate_id}.json"
    if not path.exists():
        raise CompareError(f"no estimate {estimate_id}; run the matrix first to price it")
    if not approved_by.strip():
        raise CompareError("approval needs the person's name")
    plan = json.loads(path.read_text(encoding="utf-8"))
    plan.update(approved_by=approved_by, approved_at=utcnow())
    write_text_atomic(path, dumps(plan))
    from .evaluation import record_audit

    record_audit(settings, approved_by, "approve-comparison-estimate", estimate_id,
                 f"{plan['model']}: up to {plan['total_micro_usd'] / 1e6:.4f} USD", plan)
    return plan


def approvals(settings: Settings) -> dict:
    d = _estimates_dir(settings)
    out = {}
    for p in d.glob("est-*.json") if d.exists() else []:
        plan = json.loads(p.read_text(encoding="utf-8"))
        if plan.get("approved_by"):
            out[plan["estimate_id"]] = plan
    return out


def _build_paid(settings: Settings, s: Settings, index, estimate: dict, transport) -> dict:
    if estimate["backend"] == "openai":
        facts = dense_mod.build_dense(s, transport, index.version, estimate["corpus_estimate_id"], MEMBER)
        if facts.get("status") == "ready":
            facts.update(openai_corpus_seconds(s, estimate["corpus_estimate_id"]))
        return facts
    facts = dense_mod.build_gemini_dense(s, index.version, estimate["corpus_micro_usd"])
    if facts.get("status") == "ready":
        facts.update(gemini_corpus_totals(s))
    return facts


def gemini_corpus_totals(settings: Settings) -> dict:
    """A Gemini build resumed after a stop reports only its last part; its ledger, used only by this comparison,
    holds every corpus batch: the settled cost and the summed batch time.
    ponytail: one Gemini model and one index; key the sum by dense version if a second one is compared."""
    with open_db(settings.db_path) as conn:
        rows = conn.execute("SELECT settled_micro_usd, created_at, finished_at FROM external_attempts "
                            "WHERE provider = 'gemini' AND purpose = 'embedding' AND state = 'settled'").fetchall()
    seconds = sum((datetime.fromisoformat(f) - datetime.fromisoformat(c)).total_seconds() for _, c, f in rows if f)
    return {"cost_micro_usd": sum(int(r[0] or 0) for r in rows), "embed_seconds": round(seconds, 1),
            "corpus_batches": len(rows)}


def openai_corpus_seconds(settings: Settings, corpus_estimate_id: str) -> dict:
    """The OpenAI build does not time itself; its ledger keeps every batch call of the build's job request."""
    with open_db(settings.db_path) as conn:
        rows = conn.execute("SELECT a.dispatched_at, a.finished_at FROM attempts a JOIN requests r USING (request_id) "
                            "WHERE r.idempotency_key = ? AND a.state = 'settled' AND a.dispatched_at IS NOT NULL "
                            "AND a.finished_at IS NOT NULL", (f"build-dense:{corpus_estimate_id}",)).fetchall()
    seconds = sum((datetime.fromisoformat(f) - datetime.fromisoformat(d)).total_seconds() for d, f in rows)
    return {"embed_seconds": round(seconds, 1)}


# ---------------------------------------------------------------- reranker score cache


class CachedReranker:
    """Scores of (question, passage) pairs are kept per reranker identity, so both rerank modes and every rerun
    reuse them. Latency is never measured through this wrapper."""

    def __init__(self, settings: Settings, model) -> None:
        self.settings, self.model, self.info = settings, model, model.info
        self.key = f"{model.spec.key}@{model.spec.revision}:{model.spec.max_length}:{model.spec.precision}"

    @staticmethod
    def pair_hash(question: str, payload: str) -> str:
        return hashlib.sha256(dumps([question, payload]).encode()).hexdigest()

    def rerank(self, question: str, chunks: list[dict]):
        hashes = [self.pair_hash(question, c["payload"]) for c in chunks]
        with open_db(self.settings.db_path) as conn:
            known = {r[0]: (r[1], r[2]) for r in conn.execute(
                "SELECT pair_hash, score, truncated FROM rerank_scores WHERE model_key = ? AND pair_hash = ANY(?)",
                (self.key, hashes))}
        todo = [i for i, h in enumerate(hashes) if h not in known]
        if todo:
            scores, flags = self.model.score([(question, chunks[i]["payload"]) for i in todo])
            with open_db(self.settings.db_path) as conn, tx(conn):
                conn.executemany("INSERT INTO rerank_scores VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                                 [(self.key, hashes[i], s, bool(f)) for i, s, f in zip(todo, scores, flags)])
            known.update({hashes[i]: (s, bool(f)) for i, s, f in zip(todo, scores, flags)})
        scores = [known[h][0] for h in hashes]
        order = sorted(range(len(scores)), key=lambda i: (-scores[i], chunks[i]["chunk_id"]))
        return [(i, scores[i]) for i in order], {"truncated": sum(known[h][1] for h in hashes)}


# ---------------------------------------------------------------- one row


def _metric_view(agg: dict) -> dict:
    pc = agg["packed_complete"]
    passage = agg["passage_rows"]
    return {"ndcg": agg["ndcg@5"], "support": pc["rate"], "support_n": f"{pc['numerator']}/{pc['denominator']}",
            "critical": len(agg["critical_failures"]), "critical_ids": agg["critical_failures"],
            "qualifier_losses": agg["qualifier_losses"], "p50_ms": agg["latency_ms"]["p50"],
            "p95_ms": agg["latency_ms"]["p95"], "passage_rows": passage, "fallbacks": agg["fallbacks"], "unlabelled_top5": (agg.get("ndcg_pool") or {}).get(
                "unlabelled_top5_passages")}


def _recall20(results: list[dict]) -> float | None:
    vals = [r["metrics"]["recall@20"] for r in results if r.get("metrics")]
    return round(sum(vals) / len(vals), 4) if vals else None


def _questions(results: list[dict], population: str) -> list[dict]:
    """Per-question outcome, kept small: the view lists the failures of a row from this."""
    out = []
    for r in results:
        m = r.get("metrics")
        if not m:
            continue
        out.append({"population": population, "id": r["id"], "question": r.get("question"), "type": r.get("type"),
                    "ndcg": m.get("ndcg@5"), "complete": bool(m["packed_complete"]), "hit5": m.get("hit@5"),
                    "qualifier_loss": m["qualifier_loss"], "missing": m["missing_units"],
                    "critical": r["id"] in ev.critical_failures([r]), "fallback": r.get("fallback")})
    return out


def measure(settings: Settings, s: Settings, label: str, index, analyzer, pops: dict, dense=None, vectors=None,
            reranker=None, protect: int = 0) -> dict:
    from ..retrieval.retrieval import RUN_MODES

    mode = RUN_MODES[label]
    kw = dict(dense=dense, vectors=vectors, reranker=reranker, rerank_depth=s.fused_top_k if reranker else None,
              rerank_protect=protect)
    dev = ev._execute(s, index, analyzer, pops["dev"]["rows"], mode, **kw)
    dev_unscoped = ev._execute(s, index, analyzer, pops["dev"]["rows"], mode, unscoped=True, **kw)
    needles = ev._execute(s, index, analyzer, pops["corpus"]["rows"], mode, unscoped=True, **kw)
    whole = dev_unscoped + needles
    agg = {"dev": ev.aggregate(dev, pops["dev"]["skipped"]), "whole": ev.aggregate(whole, []),
           "needles": ev.aggregate(needles, pops["corpus"]["skipped"])}
    rows = [t for t in needles if t.get("metrics")]
    n5 = sum(t["metrics"]["hit@5"] or 0 for t in rows)
    return {"results": {"dev": dev, "whole": whole, "needles": needles}, "aggregate": agg,
            "needle": {"top5": round(n5 / len(rows), 4) if rows else None, "top5_n": f"{n5}/{len(rows)}",
                       "missed_top5": [t["id"] for t in rows if not t["metrics"]["hit@5"]]}}


def _summary(measured: dict) -> dict:
    out = {"needle": measured["needle"]}
    for pop in ("dev", "whole"):
        view = _metric_view(measured["aggregate"][pop])
        view["recall20"] = _recall20(measured["results"][pop])
        out[pop] = view
    return out


def write_dev_run(settings: Settings, s: Settings, label: str, index, dense, analyzer, pops: dict,
                  measured: dict, extra: dict | None = None) -> str:
    """The development measurement as an ordinary run: what `activate-run` activates."""
    extra = dict(extra or {})
    gate = extra.pop("gate", None)
    rows = pops["dev"]
    config = ev._frozen_config(s, label, "dev", rows["dataset_sha256"], index, dense, analyzer,
                               population=(rows["population_sha256"], len(rows["rows"])),
                               extra={"ranking_policy": ev.GOLD_RANKING_POLICY, **extra}
                               if any(ev.is_gold_row(r) for r in rows["rows"]) else extra or None)
    run_id = f"{label}-{hashlib.sha256(dumps(config).encode()).hexdigest()[:10]}"
    scores = {"status": "complete", "aggregate": measured["aggregate"]["dev"], "created_at": utcnow(),
              "compared_by": COMPARE_VERSION, "provenance": {"code": ev.code_fingerprint(), "hardware": ev.hardware(),
                                                             "metric_code_sha256": ev.metric_code_sha256()}}
    if gate is not None:
        scores["gate"] = gate
    ev._write_run(settings, run_id, config, measured["results"]["dev"], scores)
    return run_id


def query_vectors(settings: Settings, s: Settings, pops: dict, transport,
                  approved_queries: set[str]) -> tuple[dict, dict]:
    """Every passage question's vector (cached, local, or paid only for a question the approved estimate priced)
    and, for a local model, the uncached embedding latency of each development question."""
    request_id = None
    if approved_queries:
        request_id = dense_mod.ensure_job_request(settings, MEMBER, f"compare:{s.embedding_model}:{utcnow()[:10]}",
                                                  {"job": "compare", "model": s.embedding_model})
    vectors, info = {}, {"embed_ms": [], "unavailable": []}
    for name, p in pops.items():
        for row in p["rows"]:
            if not ev.is_passage_row(row):
                continue
            vec, qi = dense_mod.query_vector(s, transport, row["question"], request_id=request_id, member_id=MEMBER,
                                             purpose="gold_eval",
                                             allow_paid=dense_mod.normalize_payload(row["question"]) in approved_queries)
            if vec is None:
                info["unavailable"].append(qi.get("reason"))
                continue
            vectors[ev.row_id(row)] = vec
            if qi.get("embed_ms") is not None and name == "dev":
                info["embed_ms"].append(qi["embed_ms"])
    if embedding_spec(s.embedding_model).backend == "local":
        from ..retrieval.models import local_embedder

        model = local_embedder(s.embedding_model)
        info["embed_ms"] = []
        for row in pops["dev"]["rows"]:
            if ev.is_passage_row(row):
                t0 = time.perf_counter()
                model.embed([dense_mod.normalize_payload(row["question"])], "query")
                info["embed_ms"].append(round((time.perf_counter() - t0) * 1000, 1))
    else:
        # An API model is timed only when a question is not cached yet, so every paid question call it ever made is
        # read back from its ledger instead.
        info["embed_ms"] = ledger_query_ms(s)
    return vectors, info


def ledger_query_ms(s: Settings) -> list[float]:
    """Round trip of every settled paid question embedding for this API model, from the ledger timestamps."""
    with open_db(s.db_path) as conn:
        if embedding_spec(s.embedding_model).backend == "openai":
            rows = conn.execute("SELECT dispatched_at, finished_at FROM attempts WHERE model = ? AND purpose = 'gold_eval' "
                                "AND state = 'settled' AND dispatched_at IS NOT NULL AND finished_at IS NOT NULL",
                                (s.embedding_model,)).fetchall()
        else:
            rows = conn.execute("SELECT created_at, finished_at FROM external_attempts WHERE provider = ? "
                                "AND purpose = 'gold_eval' AND state = 'settled' AND finished_at IS NOT NULL",
                                (GEMINI_PROVIDER,)).fetchall()
    return [round((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() * 1000, 1) for a, b in rows]


def _storage_mb(s: Settings) -> float | None:
    with open_db(s.db_path) as conn:
        size = conn.execute("SELECT COALESCE(SUM(pg_column_size(embedding)), 0) FROM embedding_payloads "
                            "WHERE model = ? AND dimensions = ? AND policy = ?",
                            (s.embedding_model, s.embedding_dimensions,
                             dense_mod.embed_policy(s.embedding_model))).fetchone()[0]
    return round(size / 2 ** 20, 1)


def failure_reason(exc: BaseException) -> str:
    """The reason a failed row shows: what to do for a gated model, otherwise the error's first line."""
    name = type(exc).__name__
    if name == "GatedRepoError":
        return ("gated model: accept its licence on its Hugging Face page and set HF_TOKEN in .env, then rerun "
                "the matrix")
    if name == "OutOfMemoryError":
        return "out of GPU memory"
    return f"{name}: {(str(exc).splitlines() or [''])[0]}"[:300]


def limits_identity(s: Settings) -> dict:
    return {k: getattr(s, k) for k in SERVING_LIMITS}


def _cell_key(row: dict, index, pops: dict, extra: dict) -> str:
    row = {a: row.get(a) for a in AXES}  # what the serving base adds besides axes enters through index and extra
    return hashlib.sha256(dumps({"v": COMPARE_VERSION, "eval": ev.EVAL_VERSION, "row": row,
                                 "index": index.manifest_hash,
                                 "pops": {k: p["population_sha256"] for k, p in pops.items()}, **extra}
                                ).encode()).hexdigest()[:20]


# ---------------------------------------------------------------- the runner


class Runner:
    def __init__(self, settings: Settings, analyzer, transport=None) -> None:
        self.settings, self.analyzer, self.transport = settings, analyzer, transport
        self.approved = approvals(settings)
        self._indexes: dict[str, object] = {}
        self._baselines: dict[str, dict] = {}
        self._pops: dict[str, dict] = {}

    def pops_for(self, index) -> dict:
        """The populations a row on this index is graded on: the questions pinned to the extractions it holds. After
        a re-parse the served index keeps its questions, and an index over the new extraction lists them as
        skipped (`evidence_revision_not_active`) until their evidence is reviewed again."""
        if index.version not in self._pops:
            self._pops[index.version] = populations(self.settings, index)
        return self._pops[index.version]

    def index(self, profile: str, serving: dict | None = None):
        """The profile's keyword index; on the serving profile, the activated run's own index."""
        version = (serving or {}).get("index_version")
        if version and (serving or {}).get("profile") == profile:
            if version not in self._indexes:
                from ..retrieval.retrieval import KeywordIndex

                self._indexes[version] = KeywordIndex.load(self.settings, version)
            return self._indexes[version]
        if profile not in self._indexes:
            self._indexes[profile] = keyword_index(self.settings, self.analyzer, profile)
        return self._indexes[profile]

    def _cell_path(self, key: str) -> Path:
        return compare_dir(self.settings) / "cells" / f"{key}.json"

    def cached(self, key: str) -> dict | None:
        path = self._cell_path(key)
        if not path.exists():
            return None
        cell = json.loads(path.read_text(encoding="utf-8"))
        if cell.get("status") == "failed":
            return None  # a failure is retried on the next run: its cause (access, a timeout) may be fixed
        run_id = cell.get("run_id")
        if run_id and not (ev._run_dir(self.settings, run_id) / "scores.json").exists():
            return None  # the activatable run was removed: measure again
        return cell

    def store(self, key: str, cell: dict) -> dict:
        cell = {**cell, "cell": key, "measured_at": utcnow()}
        write_text_atomic(self._cell_path(key), dumps(cell))
        return cell

    # -- baselines every table compares against
    def baseline(self, which: str, base: dict) -> dict:
        if which not in self._baselines:
            row = {**base, "retrieval": "keyword", "analyzer": "kiwi", "embedding": None, "reranker": None,
                   "rerank_mode": None} if which == "K1" else {**base, "retrieval": "hybrid", "reranker": None,
                                                               "rerank_mode": None}  # the serving hybrid
            self._baselines[which] = self.run_row(row, base)
        return self._baselines[which]

    def row_key(self, row: dict) -> tuple[str, Settings, object]:
        s = row_settings(self.settings, row)
        index = self.index(row["profile"], row.get("serving"))
        # Every limit the row executes with is part of its identity, whatever the process defaults are.
        extra_key = {"limits": limits_identity(s)}
        extra_key |= {"embedding": ev.embedding_identity(s) if row.get("embedding") else None,
                     "reranker": [s.reranker_model, s.reranker_revision, s.reranker_max_length, s.reranker_precision]
                     if row.get("reranker") else None}
        return _cell_key(row, index, self.pops_for(index), extra_key), s, index

    def recorded(self, row: dict) -> dict:
        """What the last run recorded for a row this run skips (`--only`), failures included, so the table keeps
        every row of its matrix."""
        try:
            key, _, _ = self.row_key(row)
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "reason": failure_reason(exc)}
        cell, path = self.cached(key), self._cell_path(key)
        if cell is None and path.exists():  # cached() hides failures so a run retries them; the table shows them
            cell = json.loads(path.read_text(encoding="utf-8"))
        return cell or {"status": "not_run", "reason": "not measured yet: run this matrix without --only"}

    def run_row(self, row: dict, base: dict) -> dict:
        label = row_label(row)
        try:
            key, s, index = self.row_key(row)
        except Exception as exc:  # noqa: BLE001 - a row that cannot be set up is a row with its reason
            return {"status": "failed", "reason": failure_reason(exc)}
        hit = self.cached(key)
        if hit is not None:
            return hit
        if label == "HR":
            return self._rerank_row(key, row, base, s, index)
        pops = self.pops_for(index)
        try:
            dense = facts = None
            vectors, qinfo = None, {"embed_ms": []}
            if label in ("D", "H"):
                dense, facts = ensure_dense(self.settings, s, index, self.approved, self.transport)
                if dense is None:
                    if facts.get("needs_approval"):
                        return {"status": "needs_approval", "estimate": facts["needs_approval"],
                                "reason": "paid embedding waits for the person's approval of this estimate"}
                    return self.store(key, {"status": "failed", "reason": facts.get("error")})
                paid = embedding_spec(s.embedding_model).backend != "local"
                approved_queries: set[str] = set()
                if paid and _missing_queries(s, pops):
                    # Only an approval of the estimate that priced exactly these missing questions lets them be
                    # sent; an earlier approval for the same model covered other payloads and does not carry over.
                    estimate = paid_estimate(self.settings, s, index, pops)
                    if not estimate.get("approved_by"):
                        return {"status": "needs_approval", "estimate": estimate,
                                "reason": f"{estimate['queries_to_embed']} query vectors need a paid call"}
                    approved_queries = set(estimate["queries"])
                vectors, qinfo = query_vectors(self.settings, s, pops, self.transport, approved_queries)
                if qinfo["unavailable"]:
                    reasons = sorted({r for r in qinfo["unavailable"] if r})
                    return self.store(key, {"status": "failed", "reason": f"query vectors unavailable: {reasons}"})
            measured = measure(self.settings, s, label, index, self.analyzer, pops, dense, vectors)
            run_id = write_dev_run(self.settings, s, label, index, dense, self.analyzer, pops, measured)
            cell = {"status": "complete", "run_id": run_id, "label": label, **_summary(measured),
                    "questions": sum((_questions(measured["results"][p], p) for p in ("dev", "whole")), [])}
            if label in ("K0", "K1"):
                stats = ev.profile_stats(index)
                cell.update(chunks=stats["chunks"], duplicated_tokens=_duplicated_tokens(index, stats),
                            index_version=index.version, profile=index.profile)
            if label in ("D", "H"):
                spec = embedding_spec(s.embedding_model)
                ms = qinfo["embed_ms"]
                cell.update(dense_version=dense.version, build=facts, licence=spec.licence, revision=spec.revision,
                            dims=spec.dims, context=spec.context, backend=spec.backend,
                            size_bytes=model_size_bytes(spec.key, spec.revision) if spec.backend == "local" else None,
                            peak_vram_mb=facts.get("peak_vram_mb"), truncated_chunks=facts.get("truncated_chunks"),
                            corpus_seconds=facts.get("embed_seconds"), storage_mb=_storage_mb(s),
                            cost_usd=round((facts.get("cost_micro_usd") or facts.get("settled_micro_usd") or 0) / 1e6, 4),
                            query_p50_ms=dense_mod.percentile(ms, 0.5), query_p95_ms=dense_mod.percentile(ms, 0.95))
            return self.store(key, cell)
        except Exception as exc:  # noqa: BLE001 - load failure, out of memory or a crash is a row with its reason
            free_gpu()
            return self.store(key, {"status": "failed", "label": label,
                                    "reason": failure_reason(exc)})

    def _rerank_row(self, key: str, row: dict, base: dict, s: Settings, index) -> dict:
        from ..retrieval.models import LocalRerankerModel

        h = self.baseline("H", base)
        if h.get("status") != "complete":
            return {"status": "failed", "reason": f"the serving hybrid could not be measured: {h.get('reason')}"}
        protect = PROTECTED_HEAD if row["rerank_mode"] == "below_head" else 0
        spec = reranker_spec(row["reranker"])
        model, pops = None, self.pops_for(index)
        try:
            hs = row_settings(self.settings, {**row, "retrieval": "hybrid", "reranker": None})
            dense, _ = ensure_dense(self.settings, hs, index, self.approved, self.transport)
            vectors, _ = query_vectors(self.settings, hs, pops, self.transport, set())
            model = LocalRerankerModel(spec)
            load = {**model.info, "license": spec.licence, "size_bytes": model_size_bytes(spec.key, spec.revision)}
            cached = CachedReranker(self.settings, model)
            measured = measure(self.settings, s, "HR", index, self.analyzer, pops, dense, vectors, cached,
                               protect)
            _raise_on_bypass(r for rs in measured["results"].values() for r in rs)  # every population
            latency = self._latency(s, index, dense, vectors, model, protect)
            summary = _summary(measured)
            gain = round((summary["dev"]["ndcg"] or 0) - (h["dev"]["ndcg"] or 0), 4)
            new_vs_h = sorted(set(summary["dev"]["critical_ids"]) - set(h["dev"]["critical_ids"]))
            gate = {"ndcg@5_gain": gain, "required_gain": ev.GATE_NDCG_GAIN, "new_critical_failures": new_vs_h,
                    "added_p95_ms_under_load": latency["added_p95_ms"], "allowed_added_p95_ms": ev.GATE_ADDED_P95_MS,
                    "users": LOAD_USERS, "passed": gain >= ev.GATE_NDCG_GAIN and not new_vs_h
                    and latency["added_p95_ms"] <= ev.GATE_ADDED_P95_MS}
            run_id = write_dev_run(self.settings, s, "HR", index, dense, self.analyzer, pops, measured, extra={
                "h_run": h["run_id"], "rerank_depth": s.fused_top_k, "rerank_protect": protect, "gate": gate,
                "reranker": {k: load.get(k) for k in ("model", "revision", "device", "max_length", "max_concurrency",
                                                      "precision", "backend", "layer")}})
            truncated = sum(int(next((x.split("=")[1] for x in r.get("limitations", []) if x.startswith(
                "rerank:truncated=")), 0)) for r in measured["results"]["dev"])
            cell = {"status": "complete", "run_id": run_id, "label": "HR", **summary, "gate": gate,
                    "gate_passed": gate["passed"], "recall20_before": h["dev"]["recall20"],
                    "new_critical_vs_h": len(new_vs_h), "truncated_pairs": truncated, "load": load,
                    "licence": spec.licence, "revision": spec.revision, "precision": spec.precision,
                    "max_length": spec.max_length, "layer": spec.layer, "cold_load_seconds": load["cold_load_seconds"],
                    "peak_vram_mb": model.peak_vram_mb(), "batch": model.batch, "size_bytes": load["size_bytes"], **latency,
                    "questions": sum((_questions(measured["results"][p], p) for p in ("dev", "whole")), [])}
            return self.store(key, cell)
        except Exception as exc:  # noqa: BLE001 - load failure, out of memory or a crash is a row with its reason
            return self.store(key, {"status": "failed", "label": "HR", "licence": spec.licence,
                                    "revision": spec.revision, "reason": failure_reason(exc)})
        finally:
            del model
            free_gpu()

    def _latency(self, s: Settings, index, dense, vectors, model, protect: int) -> dict:
        """Warm reranking latency without the score cache: once alone (sequential) and with LOAD_USERS concurrent
        questions, against the hybrid order without reranking under the same load."""
        rows = [r for r in self.pops_for(index)["dev"]["rows"] if ev.is_passage_row(r)]
        if not rows:
            return {"p95_alone_ms": None, "p95_loaded_ms": None, "added_p95_ms": 0}
        _raise_on_bypass(ev._execute(s, index, self.analyzer, rows[:1], "hybrid_rerank", dense, vectors, model,
                                     s.fused_top_k, rerank_protect=protect))  # warm
        trial = ev._execute(s, index, self.analyzer, rows, "hybrid_rerank", dense, vectors, model, s.fused_top_k,
                            rerank_protect=protect)
        _raise_on_bypass(trial)
        alone = [r["timings_ms"]["rerank"] for r in trial if r.get("timings_ms")]

        def one(row, rerank=True):
            t0 = time.perf_counter()
            out = ev._execute(s, index, self.analyzer, [row], "hybrid_rerank" if rerank else "hybrid", dense,
                              vectors, model if rerank else None, s.fused_top_k if rerank else None,
                              rerank_protect=protect)
            ms = (time.perf_counter() - t0) * 1000
            if rerank:
                _raise_on_bypass(out)  # a request that fell back under load timed the bypass, not the reranker
            return ms

        with ThreadPoolExecutor(max_workers=LOAD_USERS) as pool:
            loaded = list(pool.map(one, rows))
            plain = list(pool.map(lambda r: one(r, False), rows))
        p = dense_mod.percentile
        return {"p50_alone_ms": p(alone, 0.5), "p95_alone_ms": p(alone, 0.95), "p95_loaded_ms": p(loaded, 0.95),
                "h_p95_loaded_ms": p(plain, 0.95),
                "added_p95_ms": round((p(loaded, 0.95) or 0) - (p(plain, 0.95) or 0), 1)}

    def run(self, name: str, matrix: dict, only: list[str] | None = None) -> dict:
        base = serving_base(self.settings)
        k1 = self.baseline("K1", base)
        if matrix.get("axes", {}).get("reranker"):
            matrix = {**matrix, "fixed": {**(matrix.get("fixed") or {}), **{a: base[a] for a in SERVING_HYBRID}}}
        rows = matrix_rows(matrix, base)
        if matrix.get("axes", {}).get("reranker"):  # one model loaded at a time, both modes back to back
            rows.sort(key=lambda r: list(matrix["axes"]["reranker"]).index(r["reranker"]))
        out = []
        for row in rows:
            if only and not any(o == row.get("embedding") or o == row.get("reranker") or o == row.get("profile")
                                for o in only):
                cell = self.recorded(row)
            else:
                from ..retrieval.models import unload_embedders

                unload_embedders(keep=row.get("embedding"))  # one local model on the GPU at a time
                cell = self.run_row(row, base)
            if cell.get("status") == "complete" and k1.get("status") == "complete":
                cell = {**cell, "new_critical_vs_k1": len(set(cell["dev"]["critical_ids"])
                                                         - set(k1["dev"]["critical_ids"]))}
            out.append({"name": row_name(row, matrix), "axes": {a: row[a] for a in AXES}, **cell})
        return write_table(self.settings, name, matrix, out, base, k1)


def paid_models(settings: Settings, spec: dict) -> set[str]:
    """API embedding models any row of this matrix uses, whether an axis varies them, its fixed values hold them,
    or the serving base supplies them: the command opens the paid gateway for these."""
    return {r["embedding"] for r in matrix_rows(spec, serving_base(settings))
            if r.get("embedding") and embedding_spec(r["embedding"]).backend != "local"}


def _raise_on_bypass(results) -> None:
    """`retrieve` answers a reranker crash with the hybrid order; a comparison row must fail with that reason instead
    of publishing the bypass's numbers as the reranker's."""
    for r in results:
        if (r.get("fallback") or "").startswith("hybrid_rerank->"):
            raise ModelError(f"inference failed: {r['fallback']}")


# ---------------------------------------------------------------- tables


def _duplicated_tokens(index, stats: dict) -> int:
    """Tokens repeated by overlapping chunks: total chunk tokens times the overlapping share of span characters."""
    total = sum(c["token_count"] for c in index.chunks)
    spans = sum(s["end"] - s["start"] for c in index.chunks for s in c["spans"] if "start" in s)
    return round(total * stats["duplicated_span_chars"] / spans) if spans else 0


def value(row: dict, key: str):
    cur = row
    for part in key.split("."):
        cur = (cur or {}).get(part) if isinstance(cur, dict) else None
    if key == "gate":
        return None if row.get("gate_passed") is None else ("pass" if row["gate_passed"] else "fail")
    return cur


def write_table(settings: Settings, name: str, matrix: dict, rows: list[dict], base: dict, k1: dict,
                needs_evidence_review: list[str] | None = None) -> dict:
    columns = [{"key": k, "label": label, "better": better} for k, label, better in COLUMNS.get(name, COLUMNS["lexical"])]
    table = {"version": COMPARE_VERSION, "matrix": name, "title": matrix.get("title", name), "axes": matrix["axes"],
             "fixed": matrix.get("fixed") or {}, "base": base, "columns": columns, "created_at": utcnow(),
             "baseline_k1_run": k1.get("run_id"),
             "populations": {"dev": "development rows on their own documents",
                             "whole": "development questions over all documents plus the needles",
                             "needles": "needle questions over all documents"},
             "needs_evidence_review": needs_evidence_review or [], "rows": rows}
    d = compare_dir(settings) / "tables"
    write_text_atomic(d / f"{name}.json", dumps(table))
    write_text_atomic(d / f"{name}.md", table_md(table))
    return table


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.4f}" if abs(v) < 10 else f"{v:,.1f}"
    return str(v)


def table_md(table: dict) -> str:
    cols = table["columns"]
    lines = [f"# {table['title']} ({table['matrix']})", "", f"Generated {table['created_at']} by `compare --matrix "
             f"{table['matrix']}`. Fixed: `{json.dumps(table['fixed'], ensure_ascii=False)}`.", "",
             "| Row | status | " + " | ".join(c["label"] for c in cols) + " | run |",
             "| --- | --- | " + " | ".join("---" for _ in cols) + " | --- |"]
    for r in table["rows"]:
        status = r["status"] if r["status"] == "complete" else f"{r['status']}: {r.get('reason', '')}"[:120]
        lines.append(f"| {r['name']} | {status} | " + " | ".join(_fmt(value(r, c["key"])) for c in cols)
                     + f" | `{r.get('run_id') or '—'}` |")
    lines += ["", "Populations: " + "; ".join(f"{k}: {v}" for k, v in table["populations"].items()) + "."]
    if table.get("needs_evidence_review"):
        lost = table["needs_evidence_review"]
        lines += ["", f"Not verified: {len(lost)} question(s) the served rows are graded on are left out of the "
                  f"rebuilt rows, because a re-parse replaced the extraction their evidence is pinned to. Review their "
                  f"evidence again before the rebuilt rows count: {', '.join(f'`{q}`' for q in lost)}."]
    if str(table["fixed"].get("fusion", "")).startswith("keyword_first"):
        lines += ["", "Fusion `keyword_first` keeps the leading BM25 results (its last field, 6) in place, so nDCG@5 "
                  "equals K1's on every hybrid and below-the-head row; the varied axis moves the evidence after them, "
                  "which complete support and the critical counts show."]
    return "\n".join(lines) + "\n"


def load_tables(settings: Settings) -> list[dict]:
    d = compare_dir(settings) / "tables"
    order = list(MATRICES)
    found = [json.loads(p.read_text(encoding="utf-8")) for p in d.glob("*.json")] if d.exists() else []
    return sorted(found, key=lambda t: (order.index(t["matrix"]) if t["matrix"] in order else len(order), t["matrix"]))


# ---------------------------------------------------------------- golden counts


def golden_counts(settings: Settings) -> dict:
    """Rows of every gold set by status, from the databases, with the development-count discrepancy explained."""
    import os

    import psycopg

    with open_db(settings.db_path) as conn:
        live = [dict(r) for r in conn.execute("SELECT dataset, status, COUNT(*) AS n, COUNT(DISTINCT question_key) "
                                              "AS questions FROM gold_candidates GROUP BY dataset, status "
                                              "ORDER BY dataset, status")]
        dev_batches = [dict(r) for r in conn.execute(
            "SELECT split_part(candidate_id, '-', 1) AS family, decided_by, COUNT(*) AS n FROM gold_candidates "
            "WHERE dataset = 'dev' AND status = 'approved' GROUP BY 1, 2 ORDER BY 1, 2")]
    archive, archive_error = [], None
    dsn = os.environ.get(settings.database_dsn_env, "")
    if dsn:
        try:
            archive_dsn = dsn.rsplit("/", 1)[0] + "/bidmate_pilot_archive"
            with psycopg.connect(archive_dsn, connect_timeout=5) as conn:
                archive = [{"dataset": d, "status": s, "n": n, "questions": q} for d, s, n, q in conn.execute(
                    "SELECT dataset, status, COUNT(*), COUNT(DISTINCT question_key) FROM gold_candidates "
                    "GROUP BY 1, 2 ORDER BY 1, 2")]
        except Exception as exc:  # noqa: BLE001 - reported, never guessed
            archive_error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
    manifest = settings.data_dir / "judges" / "reference" / "manifest.json"
    judge = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
    live_dev = sum(r["n"] for r in live if r["dataset"] == "dev" and r["status"] == "approved")
    pilot = next((r for r in archive if r["dataset"] == "dev" and r["status"] == "approved"), {"n": 0, "questions": 0})
    pilot_dev, pilot_questions = pilot["n"], pilot["questions"]
    sealed = sum(r["n"] for r in live if r["dataset"] == "test" and r["status"] == "approved")
    report = {
        "created_at": utcnow(),
        "live": live, "live_dev_by_family": dev_batches, "pilot_archive": archive, "pilot_archive_error": archive_error,
        "judge_reference": {"items": judge.get("items"), "run_id": judge.get("run_id"),
                            "path": str(manifest.parent) if judge else None},
        "judge_set": judge_set.counts(settings),
        "development_count": {"live_approved": live_dev, "release_report": 50, "pilot_archive_approved": pilot_dev,
                              "pilot_archive_questions": pilot_questions, "live_sealed_approved": sealed},
        "explanation": (
            f"The release report counts 50 development questions: the refresh50 batch. The Phase 4 pilot "
            f"(`bidmate_pilot_archive`) holds {pilot_dev} approved dev rows for {pilot_questions} distinct questions, "
            "because some questions were approved again as a corrected revision. The live set has "
            f"{live_dev}: the refresh50 questions once each plus the `cutover-late-*` rows drafted and approved during "
            "the PostgreSQL cutover, after the release report was written. Every number is correct for its source; "
            f"every current comparison uses the live set. The live sealed set has {sealed} approved rows: the pilot's "
            "sealed rows stay in the archive and are not reused."),
    }
    d = compare_dir(settings)
    write_text_atomic(d / "golden-counts.json", dumps(report))
    write_text_atomic(d / "golden-counts.md", golden_counts_md(report))
    return report


def golden_counts_md(r: dict) -> str:
    def table(rows):
        out = ["| set | status | rows | distinct questions |", "| --- | --- | --- | --- |"]
        out += [f"| {x['dataset']} | {x['status']} | {x['n']} | {x.get('questions', '-')} |" for x in rows]
        return out

    name = {"dev": "development", "test": "sealed", "corpus": "needle", "dev-pilot": "candidate (dev-pilot)"}
    live = [{**x, "dataset": f"{name.get(x['dataset'], x['dataset'])} (`{x['dataset']}`)"} for x in r["live"]]
    if not any(x["dataset"] == "test" for x in r["live"]):
        live.append({"dataset": "sealed (`test`)", "status": "any", "n": 0, "questions": 0})
    lines = ["# Golden-set counts", "", f"Generated {r['created_at']} from `bidmate_app` and `bidmate_pilot_archive`.",
             "", "## Live (`bidmate_app.gold_candidates`)", "", *table(live), "",
             "Approved development rows by batch family and reviewer:", "",
             "| family | reviewer | rows |", "| --- | --- | --- |",
             *[f"| {x['family']} | {x['decided_by']} | {x['n']} |" for x in r["live_dev_by_family"]], "",
             "## Phase 4 pilot (`bidmate_pilot_archive`, read-only)", ""]
    lines += table(r["pilot_archive"]) if r["pilot_archive"] else [f"Unavailable: {r['pilot_archive_error']}"]
    j = r["judge_reference"]
    lines += ["", "## Judge reference", "", f"{j['items']} reviewed reference verdicts (run `{j['run_id']}`), files "
              f"under `{j['path']}`." if j["items"] else "No judge reference set.", ""]
    s = r.get("judge_set")
    lines += ["## Judge set (apart from the development set)", ""]
    lines += [f"{s['negatives']} mutated negatives and {s['positives']} unmutated positives ({s['items']} items, "
              f"`{s['version']}`), under `judges/judge-set/`; none of them is a development row.", "",
              "| mutation | negatives |", "| --- | --- |",
              *[f"| {k} | {n} |" for k, n in s["by_type"].items()], ""] if s else ["No judge set yet: `judge-set`.", ""]
    lines += [f"## 50 versus {r['development_count']['live_approved']} development rows", "", r["explanation"], ""]
    return "\n".join(lines)


REGRESSION = {"title": "회귀 (유지보수)", "axes": {"index": ["served", "rebuilt"]}, "fixed": {}}


def regression(settings: Settings, analyzer, transport, rebuilt: str) -> dict:
    """The maintenance regression table: K1 and the serving configuration on the served keyword index and on the
    index maintenance just rebuilt, so drift after new or changed originals shows as rows of one table. Cached
    cells are reused; when the rebuilt index is the served one, only the served rows exist. Activates nothing."""
    runner = Runner(settings, analyzer, transport)
    base = serving_base(settings)
    k1 = runner.baseline("K1", base)
    served = base["serving"]["index_version"]
    rows = []
    for where, version in [("served", served)] + ([("rebuilt", rebuilt)] if rebuilt != served else []):
        at = {**base, "serving": {**base["serving"], "index_version": version}}
        keyword = {**at, "retrieval": "keyword", "analyzer": "kiwi", "embedding": None, "reranker": None,
                   "rerank_mode": None}
        for row in [keyword] + ([at] if at["retrieval"] != "keyword" else []):
            cell = runner.run_row(row, base)
            if cell.get("status") == "complete" and k1.get("status") == "complete":
                cell = {**cell, "new_critical_vs_k1": len(set(cell["dev"]["critical_ids"])
                                                         - set(k1["dev"]["critical_ids"]))}
            rows.append({"name": f"{where} · {row_label(row)} · {version[:8]}",
                         "axes": {**{a: row[a] for a in AXES}, "index": where}, "index_version": version, **cell})
    return write_table(settings, "regression", REGRESSION, rows, base, k1,
                       needs_evidence_review=lost_to_reparse(runner, served, rebuilt))


def lost_to_reparse(runner: "Runner", served: str, rebuilt: str) -> list[str]:
    """Questions the served index is graded on that the rebuilt one cannot be: their evidence is pinned to an
    extraction a re-parse replaced. The rebuilt rows leave them out until a person reviews their evidence again,
    so a comparison that lost any is not a verified one."""
    from ..retrieval.retrieval import KeywordIndex

    if rebuilt == served:
        return []
    before, after = (runner.pops_for(KeywordIndex.load(runner.settings, v)) for v in (served, rebuilt))
    return sorted({str(s["id"]) for name in ("dev", "corpus") for s in after[name]["skipped"]
                   if s["reason"] == "evidence_revision_not_active"}
                  & {str(ev.row_id(r)) for name in ("dev", "corpus") for r in before[name]["rows"]})
