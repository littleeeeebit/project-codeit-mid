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
`ocr` envelope). The approved maximum holds across every pass: the ledger, not this pass, counts what a candidate
has already spent or left unknown, and each reservation is refused above what is left (the read or answer it was for
is unresolved, with the refusal). Each paid OCR read is kept on disk as it settles, and the ledger names every region
and answer already billed, so a pass that dies midway or loses its output never pays for one again.
"""

from __future__ import annotations

import contextlib
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


def committed(conn, key_prefix: str) -> int:
    """What the ledger holds against one candidate's approval across every pass: settled attempts at their cost, every
    other attempt but a released one at its reservation (an unknown one may yet be billed in full)."""
    return conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN a.state = 'settled' THEN a.settled_micro_usd WHEN a.state = 'released' THEN 0 "
        "ELSE a.reserved_micro_usd END), 0) FROM requests r JOIN attempts a ON a.request_id = r.request_id "
        "WHERE r.idempotency_key LIKE ?", (key_prefix + "%",)).fetchone()[0]


def paid_items(conn, key_prefix: str) -> set[str]:
    """What the ledger will never pay for again under one candidate's keys, whatever its output holds: an OCR region
    (attempt stage `ocr:<region>`) or a question (key `<prefix><question>:<n>`) with an attempt that is not released
    (it was or may have been billed), and a question whose request stored a finished answer (it replays for free)."""
    items = set()
    for key, result, stage, state in conn.execute(
            "SELECT r.idempotency_key, r.result_json, a.stage, a.state FROM requests r LEFT JOIN attempts a "
            "ON a.request_id = r.request_id WHERE r.idempotency_key LIKE ?", (key_prefix + "%",)).fetchall():
        billed = state is not None and state != "released"
        if key_prefix.startswith("review:"):  # the OCR job: one request, an attempt per region
            if billed and (stage or "").startswith("ocr:"):
                items.add(stage[4:])
            continue
        stored = json.loads(result)["status"] if result else None
        if billed or stored not in (None, "budget_blocked", "technical_error"):
            items.add(key[len(key_prefix):].rsplit(":", 1)[0])
    return items


@contextlib.contextmanager
def capped_reservations(config: dict):
    """While open, every reservation is admitted only up to what the candidate's approval has left: the approved
    maximum less what the ledger holds against it. The gateway refuses above that in its own admission, so it binds
    whatever the candidate's code reserves at whatever the rates are now."""
    from rfp_assistant.gateway import budget
    from rfp_assistant.storage import store

    reserve = budget.reserve

    def capped(db, **kw):
        # ponytail: read before the admission transaction; one worker per candidate makes it the only writer
        with store.open_db(db) as conn:
            left = config["cap_micro_usd"] - committed(conn, config["ledger_key"])
        ceiling = kw.get("ceiling_micro_usd")
        return reserve(db, **{**kw, "ceiling_micro_usd": left if ceiling is None else min(ceiling, left)})

    budget.reserve = capped
    try:
        yield
    finally:
        budget.reserve = reserve


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
        from rfp_assistant.gateway import budget
        from rfp_assistant.storage import store

        pinned = answers.PinnedResources(s, None, serving)
        try:
            prices = [{"question_id": r["question_id"], **answers.price_row(pinned, r)} for r in rows]
            for p in prices:  # the call's token bound, so a re-price applies the rates of its day; the rest is the query
                p["extra_micro_usd"] = p["max_micro_usd"] and p["max_micro_usd"] - budget.estimate(
                    s.db_path, s.generation_model, p["input_tokens"], s.generation_max_output_tokens)
            with store.open_db(s.db_path) as conn:
                version = conn.execute("SELECT rate_version FROM budget_settings WHERE id = 1").fetchone()[0]
            return {"prices": prices, "model": s.generation_model, "rate_version": version,
                    "max_output_tokens": s.generation_max_output_tokens}
        finally:
            pinned.close()
    owner = service.Resources(s)  # the gateway owner of the shared ledger, the key's transport, tracing if set
    pinned = None
    try:
        if owner.transport is None:
            raise RuntimeError(owner.provider_note or "no provider transport")
        pinned = answers.PinnedResources(s, owner.transport, serving, owner)
        pinned.tracing = owner.tracing  # a borrowed transport is not traced on its own
        out, stop = [], None
        # A resumed candidate keeps the answers it finished, with their measured latency, instead of replaying them;
        # an answer whose output was lost replays from its request without a call (`_answer_row`), so nothing is
        # checked before it: only a reservation can pass the approval, and the gateway refuses that one.
        earlier = json.loads(Path(config["previous"]).read_text(encoding="utf-8")).get("rows") or [] \
            if config.get("previous") else []
        finished = {r["question_id"]: r for r in earlier if r.get("status") == "done"}
        with capped_reservations(config):
            for row in rows:
                if row["question_id"] in finished:
                    out.append(finished[row["question_id"]])
                    continue
                record = answers._answer_row(s, pinned, config["run_key"], config["candidate"], row, None)
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
        return {"rows": out, **({"status": "stopped", "reason": stop} if stop else {})}
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
        # The read's token bound beside its price, so a re-price applies the rates of its day.
        return {"images": reads, "per_read_micro_usd": per_read, "model": ocr.REMOTE_MODEL,
                "input_tokens": ocr.remote_input_tokens(ledger), "max_output_tokens": ocr.REMOTE_MAX_OUTPUT}
    local_reads = json.loads(Path(config["local_reads"]).read_text(encoding="utf-8"))["images"]
    previous = {r["n"]: r for r in paid_reads(config.get("previous"), config["progress"])}
    by_n = {i["n"]: i for i in images}
    out, stop, request_id = [], None, None
    with store.database_lifecycle(ledger.db_path), capped_reservations(config), ocr.RemoteReader(
            ledger, job=f"review:{config['run_key']}:{config['candidate']}") as reader:
        request_id = reader.request_id
        with store.open_db(ledger.db_path) as conn:  # after the reader's recovery: an orphaned call is unknown now
            billed = paid_items(conn, config["ledger_key"])
        for read in local_reads:
            row = {**read, "local_text": read["text"]}
            region = by_n[read["n"]].get("digest") or str(read["n"])
            if read["reasons"] and read["n"] in previous:
                row = previous[read["n"]]
            elif read["reasons"] and region in billed:
                row.update(status="unresolved", error="paid in the ledger, but its read was lost; not paid again")
            elif read["reasons"] and stop is None:
                png, _ = _image(by_n[read["n"]], inputs)
                try:
                    text, micro = reader.read(png, region)  # refused above what the approval has left
                    row.update(status="remote", text=text, micro_usd=micro)
                    with open(config["progress"], "a", encoding="utf-8") as f:  # kept even if this process dies
                        f.write(json.dumps(row, ensure_ascii=False) + "\n")
                except ocr.OcrStop as exc:
                    row.update(status="unresolved", error=str(exc)[:300])
                    stop = str(exc)
                except ocr.OcrError as exc:
                    row.update(status="unresolved", error=str(exc)[:300])
                except OSError as exc:  # the read is paid and in hand; the ledger keeps it from being paid twice
                    stop = f"could not record a paid read: {exc}"[:300]
            elif read["reasons"]:
                row.update(status="unresolved", error=f"not read: {stop}"[:300])
            out.append(row)
    return {"images": out, "request_id": request_id, **({"status": "stopped", "reason": stop} if stop else {})}


def paid_reads(previous: str | None, progress: str | None) -> list[dict]:
    """Every gpt-5-mini read a candidate already paid for: those its last output holds and those it recorded one by
    one before it could write an output (a worker that died midway)."""
    reads = []
    if previous and Path(previous).exists():
        reads += json.loads(Path(previous).read_text(encoding="utf-8")).get("images") or []
    if progress and Path(progress).exists():
        reads += [json.loads(line) for line in Path(progress).read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in reads if r.get("status") == "remote"]


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
