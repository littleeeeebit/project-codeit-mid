"""One candidate of a stage review, run in a fresh process inside the candidate's own worktree.

Usage: python -I -B stage_review_worker.py <worktree> <stage> <inputs dir> <config.json> <output.json>

The runner copies this file into the run folder and runs the same copy for every candidate, so only the candidate's
package differs. It imports `rfp_assistant` from `<worktree>/src`, reads the frozen inputs and writes raw output
(rankings or chunks); scoring happens once, in the runner. The database session is read-only (the runner sets
PGOPTIONS), a query vector is read from the cache or embedded by a local model without being stored, and an API
model is never called.
"""

from __future__ import annotations

import gzip
import json
import platform
import sys
import time
from importlib import metadata
from pathlib import Path


def host() -> dict:
    info = {"platform": platform.platform(), "machine": platform.machine(), "processor": platform.processor(),
            "python": platform.python_version(), "cuda": False, "gpu": None}
    try:
        import torch

        if torch.cuda.is_available():
            info.update(cuda=True, gpu=torch.cuda.get_device_name(0))
    except Exception:  # noqa: BLE001 - no torch means no GPU
        pass
    return info


def retriever(inputs: Path, config: dict) -> dict:
    from rfp_assistant.contracts import DocRef
    from rfp_assistant.retrieval import dense, retrieval
    from rfp_assistant.settings import Settings, load_settings
    from rfp_assistant.storage import store

    frozen = json.loads((inputs / "questions.json").read_text(encoding="utf-8"))
    base = load_settings(provider="fake")
    with store.database_lifecycle(base.db_path):
        known = set(Settings.__dataclass_fields__)
        changes = {k: v for k, v in (config.get("limits") or {}).items() if k in known}
        if config.get("embedding"):
            changes.update(embedding_model=config["embedding"]["model"],
                           embedding_dimensions=config["embedding"]["dims"])
        rr = config.get("reranker")
        if rr:
            changes.update(reranker_model=rr["model"], reranker_revision=rr["revision"],
                           reranker_max_length=rr["max_length"], reranker_precision=rr["precision"])
        s = base.with_(**changes)
        index = retrieval.KeywordIndex.load(base, config["index_version"])
        analyzer = retrieval.Analyzer()
        mode = config["mode"]
        dense_index = reranker = None
        if mode in retrieval.DENSE_MODES:
            dense_index = dense.DenseIndex.load(s, config["dense_version"], base=index)
        if mode == "hybrid_rerank":
            reranker, info = dense.load_reranker(s)
            if reranker is None:
                raise RuntimeError(f"reranker did not load: {info.get('error')}")
        corpus = [(DocRef(d, h), x) for d, h, x in frozen["corpus_scope"]]
        embedder = None
        out = []
        for q in frozen["questions"]:
            vector = None
            if dense_index is not None:
                text = dense.normalize_payload(q["question"])
                vector = dense.cache_get(s, dense.payload_hash(text, s.embedding_model, s.embedding_dimensions,
                                                               "query"))
                if vector is None:
                    if config["embedding"]["backend"] != "local":
                        raise RuntimeError(f"query vector for {q['key']} is not cached and {s.embedding_model} is an "
                                           "API model; a stage review never pays for one")
                    if embedder is None:
                        from rfp_assistant.retrieval.models import local_embedder

                        embedder = local_embedder(s.embedding_model)
                    vector = embedder.embed([text], "query")[0]  # never written back: the session is read-only
            sides = []
            for side in q["sides"]:
                scope = corpus if side == "corpus" else [(DocRef(d, h), x) for d, h, x in side]
                started = time.perf_counter()
                r = retrieval.retrieve(s, index, analyzer, q["question"], scope, mode=mode, dense=dense_index,
                                       query_vector=vector, reranker=reranker,
                                       rerank_depth=(rr or {}).get("depth"), rerank_protect=(rr or {}).get("protect") or 0)
                sides.append({"ranking": list(r.ranking[:config["rank_depth"]]),
                              "packed": [e.chunk_id for e in r.evidence], "fallback": r.fallback,
                              "limitations": list(r.limitations),
                              "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)})
            out.append({"key": q["key"], "sides": sides})
    return {"questions": out}


def chunking(inputs: Path, config: dict) -> dict:
    from rfp_assistant.retrieval import chunking as chunker

    with gzip.open(inputs / "documents.json.gz", "rt", encoding="utf-8") as f:
        documents = json.load(f)["documents"]
    profile = config.get("profile") or "structural"
    out = []
    for doc in documents:
        if profile == "structural":
            chunks, _ = chunker.build_chunks(doc["elements"], doc["extraction_id"])
        else:
            chunks, _ = chunker.build_profile(doc["elements"], doc["extraction_id"], profile)
        out.append({"extraction_id": doc["extraction_id"],
                    "chunks": [{k: c.get(k) for k in ("chunk_id", "spans", "payload", "token_count", "chunk_type")}
                               for c in chunks]})
    return {"documents": out}


def main() -> None:
    worktree, stage, inputs, config_path, output = sys.argv[1:]
    started = time.perf_counter()
    result: dict = {"host": host()}
    try:
        sys.path.insert(0, str(Path(worktree) / "src"))
        import rfp_assistant

        if not Path(rfp_assistant.__file__).resolve().is_relative_to(Path(worktree).resolve()):
            raise RuntimeError("the worker imported rfp_assistant from outside its worktree")
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        result.update({"retriever": retriever, "chunking": chunking}[stage](Path(inputs), config))
        result["status"] = "complete"
    except BaseException as exc:  # noqa: BLE001 - every failure is a visible candidate result
        result.update(status="failed", reason=f"{type(exc).__name__}: {exc}"[:2000])
    result["elapsed_s"] = round(time.perf_counter() - started, 2)
    result["packages"] = sorted(f"{d.metadata['Name']}=={d.version}" for d in metadata.distributions()
                                if d.metadata["Name"])
    Path(output).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
