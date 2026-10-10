"""One candidate of a stage review, run in a fresh process inside the candidate's own worktree.

Usage: python -I -B stage_review_worker.py <worktree> <stage> <inputs dir> <config.json> <output.json> [mode]

The runner copies this file into the run folder and runs the same copy for every candidate, so only the candidate's
package differs. It imports `rfp_assistant` from `<worktree>/src`, reads the frozen inputs and writes raw output
(rankings, chunks, answers or reads); scoring happens once, in the runner.

Retriever and chunking (mode `run`): the database session is read-only (the runner sets PGOPTIONS), a query vector
is read from the cache or embedded by a local model without being stored, and an API model is never called.
Generation and OCR price first (mode `estimate`, no key, no paid call) and pay only in mode `paid`, which the runner
starts with an approved estimate: answers go through the candidate's own answer path and budget gateway (charged to
gold_eval, idempotency keys `<run>:<candidate>:<question>:<n>`), OCR re-reads through its own RemoteReader (the
`ocr` envelope). Each candidate stops before a call that could take it past its approved maximum.
"""

from __future__ import annotations

import gzip
import io
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


def _generation_settings(config: dict):
    from rfp_assistant.settings import load_settings
    from rfp_assistant.storage import store

    s = load_settings(generation_model=config["model"], generation_reasoning_effort=config["reasoning_effort"],
                      generation_max_output_tokens=config["max_output_tokens"])
    with store.database_lifecycle(s.db_path), store.open_db(s.db_path) as conn:
        current = store.schema_version(conn)
    if current != store.SCHEMA_VERSION:  # its init_schema would migrate the shared database
        raise RuntimeError(f"this candidate's schema version {store.SCHEMA_VERSION} is not the database's {current}")
    return s


def generation(inputs: Path, config: dict, mode: str) -> dict:
    from rfp_assistant.service import answers, service

    rows = json.loads((inputs / "rows.json").read_text(encoding="utf-8"))
    serving = json.loads((inputs / "activation.json").read_text(encoding="utf-8"))
    s = _generation_settings(config)
    if mode == "estimate":
        pinned = answers.PinnedResources(s, None, serving)
        try:
            return {"prices": [{"question_id": r["question_id"], **answers.price_row(pinned, r)} for r in rows]}
        finally:
            pinned.close()
    owner = service.Resources(s)  # the gateway owner of the shared ledger, the key's transport, tracing if set
    pinned = None
    try:
        if owner.transport is None:
            raise RuntimeError(owner.provider_note or "no provider transport")
        pinned = answers.PinnedResources(s, owner.transport, serving, owner)
        pinned.tracing = owner.tracing  # a borrowed transport is not traced on its own
        out, spent, stop = [], 0, None
        for row in rows:
            if spent + config["prices"].get(row["question_id"], 0) > config["cap_micro_usd"]:
                stop = "the next answer could pass the approved maximum"
                break
            record = answers._answer_row(s, pinned, config["run_key"], config["candidate"], row, None)
            spent += record.get("settled_micro_usd") or 0
            out.append(record)
            if record["status"] in ("blocked", "unknown_billing"):
                stop = f"{record['status']}: {record.get('reason') or record.get('outcome') or ''}".strip()
                break
        if owner.tracing is not None:
            client = owner.tracing.client
            for record in out:
                try:
                    record["trace_url"] = client.get_trace_url(trace_id=client.create_trace_id(
                        seed=record["request_id"]))
                except Exception:  # noqa: BLE001 - a missing link never changes the answer
                    pass
        return {"rows": out, "spent_micro_usd": spent, **({"status": "stopped", "reason": stop} if stop else {})}
    finally:
        if pinned is not None:
            pinned.tracing = None  # the owner flushes and closes it
            pinned.close()
        owner.close()


def _image(item: dict, inputs: Path):
    """The frozen bytes as this candidate decodes them, or None when it cannot."""
    from PIL import Image
    from rfp_assistant.corpus import ocr

    raw = inputs / "images" / f"{item['n']}.bin"
    if not raw.exists():
        return None, None
    try:
        data = raw.read_bytes()
        png = ocr.to_png(data) if item["kind"] == "hwp" else data
        return png, Image.open(io.BytesIO(png)).convert("RGB")
    except Exception:  # noqa: BLE001 - an image this candidate cannot decode is not OCR'd
        return None, None


def ocr_stage(inputs: Path, config: dict, mode: str) -> dict:
    from rfp_assistant.corpus import ocr
    from rfp_assistant.gateway import budget
    from rfp_assistant.settings import load_settings
    from rfp_assistant.storage import store

    images = json.loads((inputs / "images.json").read_text(encoding="utf-8"))["images"]
    ledger = load_settings(provider="fake", database_dsn_env=config.get("ledger_env") or "RFP_DATABASE_DSN")
    if mode == "estimate":
        with store.database_lifecycle(ledger.db_path):
            per_read = budget.estimate(ledger.db_path, ocr.REMOTE_MODEL, ocr.remote_input_tokens(ledger),
                                       ocr.REMOTE_MAX_OUTPUT)
        reads = []
        with ocr.LocalOCR() as local:
            for item in images:
                _, image = _image(item, inputs)
                if image is None:
                    reads.append({"n": item["n"], "status": "unreadable", "text": "", "reasons": []})
                    continue
                started = time.perf_counter()
                first = {**local.read(image), "expected": round(ocr.expected_chars(image), 1)}
                reasons = ocr.fallback_reasons(first["text"], first["mean_prob"], first["looped"], first["expected"],
                                               first["truncated"])
                reads.append({"n": item["n"], "status": "local", "text": first["text"], "reasons": reasons,
                              **{k: first[k] for k in ("mean_prob", "looped", "truncated", "expected")},
                              "seconds": round(time.perf_counter() - started, 2)})
        return {"images": reads, "per_read_micro_usd": per_read, "model": ocr.REMOTE_MODEL}
    local_reads = json.loads(Path(config["local_reads"]).read_text(encoding="utf-8"))["images"]
    previous = {}
    if config.get("previous") and Path(config["previous"]).exists():
        previous = {r["n"]: r for r in json.loads(Path(config["previous"]).read_text(encoding="utf-8")).get("images")
                    or [] if r["status"] == "remote"}
    by_n = {i["n"]: i for i in images}
    out, spent, stop, request_id = [], 0, None, None
    with store.database_lifecycle(ledger.db_path), ocr.RemoteReader(
            ledger, job=f"review:{config['run_key']}:{config['candidate']}") as reader:
        request_id = reader.request_id
        for read in local_reads:
            row = {**read, "local_text": read["text"]}
            if read["reasons"] and read["n"] in previous:
                row = previous[read["n"]]
            elif read["reasons"] and stop is None:
                if spent + config["per_read_micro_usd"] > config["cap_micro_usd"]:
                    stop = "the next read could pass the approved maximum"
                else:
                    png, _ = _image(by_n[read["n"]], inputs)
                    try:
                        text, micro = reader.read(png, by_n[read["n"]].get("digest") or str(read["n"]))
                        row.update(status="remote", text=text, micro_usd=micro)
                        spent += micro
                    except ocr.OcrStop as exc:
                        row.update(status="unresolved", error=str(exc)[:300])
                        stop = str(exc)
                    except ocr.OcrError as exc:
                        row.update(status="unresolved", error=str(exc)[:300])
            elif read["reasons"]:
                row["status"] = "unresolved"
            out.append(row)
    return {"images": out, "request_id": request_id, "spent_micro_usd": spent,
            **({"status": "stopped", "reason": stop} if stop else {})}


def main() -> None:
    worktree, stage, inputs, config_path, output, *rest = sys.argv[1:]
    mode = rest[0] if rest else "run"
    started = time.perf_counter()
    result: dict = {"host": host()}
    try:
        sys.path.insert(0, str(Path(worktree) / "src"))
        import rfp_assistant

        if not Path(rfp_assistant.__file__).resolve().is_relative_to(Path(worktree).resolve()):
            raise RuntimeError("the worker imported rfp_assistant from outside its worktree")
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        if stage in ("generation", "ocr"):
            result.update({"generation": generation, "ocr": ocr_stage}[stage](Path(inputs), config, mode))
        else:
            result.update({"retriever": retriever, "chunking": chunking}[stage](Path(inputs), config))
        result.setdefault("status", "complete")
    except BaseException as exc:  # noqa: BLE001 - every failure is a visible candidate result
        result.update(status="failed", reason=f"{type(exc).__name__}: {exc}"[:2000])
    result["elapsed_s"] = round(time.perf_counter() - started, 2)
    result["packages"] = sorted(f"{d.metadata['Name']}=={d.version}" for d in metadata.distributions()
                                if d.metadata["Name"])
    Path(output).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
