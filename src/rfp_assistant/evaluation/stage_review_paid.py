"""The paid stages of a stage review: generation (gpt-5-mini answers the frozen development set) and OCR (one fixed
image set, PaddleOCR-VL with gpt-5-mini re-reading what the candidate's own test flags).

A paid run stops at a priced estimate: `stage_review.run` freezes the inputs and runs every candidate's worker in
its free estimate mode (generation prices each answer exactly as the answer path will; OCR reads every image locally
and prices a gpt-5-mini read of each image the candidate flags). The person approves the estimate (`approve`), and
only `stage_review.resume` pays. The API key reaches a worker in one place, `approved_env`, which refuses an estimate
that is not approved, has expired or is no longer the one on disk. Paid calls go through each candidate's own budget
gateway into the shared ledger: generation's database is the runner's (point RFP_DATABASE_DSN at the shared one,
e.g. codeit's through an SSH tunnel), OCR's ledger is the database named by `--ledger-dsn-env` (default the same).

Files beside the common run folder: `estimate.json`, `candidates/<id>.estimate.json` (prices or local reads) and,
for OCR, `images/<n>.png` (what the app shows; the inputs keep the embedded bytes each candidate decodes itself).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import random
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from ..settings import TRACING_ENV, Settings, read_api_key, tracing_credentials
from ..storage.store import dumps, open_db, utcnow, write_text_atomic
from . import evaluation as ev
from . import stage_review as sr

STAGES = ("generation", "ocr")
MODEL = "gpt-5-mini"
PURPOSE = {"generation": "gold_eval", "ocr": "ocr"}
ESTIMATE_TTL_HOURS = 24
OCR_SAMPLE, OCR_SEED = 30, 20261010  # unflagged images read beside every flagged one, drawn once per seed


def ledger_settings(settings: Settings, record: dict) -> Settings:
    env = record.get("ledger_env")
    return settings.with_(database_dsn_env=env) if env else settings


def _titles(settings: Settings) -> dict[str, str]:
    with open_db(settings.db_path) as conn:
        return {r["doc_id"]: json.loads(r["normalized_metadata_json"]).get("title") or r["doc_id"]
                for r in conn.execute("SELECT doc_id, normalized_metadata_json FROM documents")}


# ---------------------------------------------------------------- frozen inputs


def freeze(settings: Settings, stage: str, inputs: Path) -> None:
    if stage == "generation":
        rows, skipped, sha = ev.load_eval_rows(settings, "dev")
        if not rows:
            raise sr.ReviewError("no independently reviewed dev rows to answer")
        write_text_atomic(inputs / "rows.json", json.dumps(rows, ensure_ascii=False))
        write_text_atomic(inputs / "dataset.json", json.dumps(
            {"dataset": "dev", "dataset_sha256": sha, "rows": len(rows), "skipped": skipped,
             "population_sha256": ev.population_identity(rows, skipped)}, ensure_ascii=False))
        write_text_atomic(inputs / "documents.json", json.dumps(_titles(settings), ensure_ascii=False))
    else:
        freeze_ocr(settings, inputs)


def freeze_ocr(settings: Settings, inputs: Path) -> None:
    """Every image the current OCR cache flagged or could not decode, plus OCR_SAMPLE images whose local read passed:
    each distinct image once, as the bytes a candidate decodes itself (an HWP picture's embedded file, a PDF region's
    PNG). An image whose bytes are gone (a linked file) stays in the set as unreadable for every candidate."""
    from ..corpus import ocr
    from ..storage.postgres import host_path

    with open_db(settings.db_path) as conn:
        sources = [dict(r) for r in conn.execute(
            "SELECT s.source_hash, s.format, s.original_path, MIN(d.doc_id) AS doc_id FROM sources s "
            "JOIN documents d ON d.active_source_hash = s.source_hash WHERE s.parse_status = 'parsed' "
            "GROUP BY s.source_hash, s.format, s.original_path ORDER BY s.source_hash")]
    titles = _titles(settings)
    picked, passing, seen = [], [], set()
    for src in sources:
        for row in ocr.load(settings, src["source_hash"]):
            key = row.get("digest") or (src["source_hash"], row.get("bindata"))
            if key in seen:
                continue
            seen.add(key)
            # flagged as the current fallback test judges the cached local read (thresholds may have moved since)
            item = {"source_hash": src["source_hash"], "doc_id": src["doc_id"], "title": titles.get(src["doc_id"]),
                    "format": src["format"], "original": src["original_path"], **ocr._place(row),
                    "digest": row.get("digest"), "cached": row["status"],
                    "reasons": ocr._reasons(row["local"]) if row.get("local") else []}
            if row["status"] == "unavailable":
                picked.append({**item, "reasons": [f"unavailable:{row.get('reason')}"]})
            elif item["reasons"]:
                picked.append(item)
            else:
                passing.append(item)
    flagged = len(picked)
    picked += random.Random(OCR_SEED).sample(passing, min(OCR_SAMPLE, len(passing)))
    if not picked:
        raise sr.ReviewError("the OCR cache holds no images; run `ocr` first")
    (inputs / "images").mkdir()
    pdf_pngs: dict[str, dict] = {}
    images = []
    for n, item in enumerate(picked):
        original = host_path(item.pop("original"))
        data = None
        try:
            if "bindata" in item:
                data = _bindata(original, item["bindata"])
            else:
                if item["source_hash"] not in pdf_pngs:
                    pdf_pngs[item["source_hash"]] = {r["digest"]: r["png"] for r in ocr.regions(original)}
                data = pdf_pngs[item["source_hash"]].get(item["digest"])
        except Exception:  # noqa: BLE001 - an original that cannot be opened leaves this image unreadable
            data = None
        if data is not None:
            (inputs / "images" / f"{n}.bin").write_bytes(data)
        images.append({"n": n, **item, "kind": "hwp" if "bindata" in item else "png", "bytes": data is not None,
                       "sample": n >= flagged})
    write_text_atomic(inputs / "images.json", json.dumps({"ocr_version": ocr.OCR_VERSION, "flagged": flagged,
                                                          "sample": len(picked) - flagged, "seed": OCR_SEED,
                                                          "images": images}, ensure_ascii=False))


def _bindata(original: Path, name: str) -> bytes | None:
    from hwp5.xmlmodel import Hwp5File

    f = Hwp5File(str(original))
    streams = {n.lower(): n for n in f["BinData"]} if "BinData" in f else {}
    stream = streams.get((name or "").lower())
    return f["BinData"][stream].open().read() if stream else None


def render_images(folder: Path) -> None:
    """What the app shows beside the reads: the runner's own render of each image (a candidate decodes the bytes
    itself, so an image it cannot decode still shows here when the runner can)."""
    from ..corpus import ocr

    out = folder / "images"
    out.mkdir(exist_ok=True)
    for item in json.loads((folder / "inputs" / "images.json").read_text(encoding="utf-8"))["images"]:
        raw = folder / "inputs" / "images" / f"{item['n']}.bin"
        if raw.exists():
            try:
                data = raw.read_bytes()
                (out / f"{item['n']}.png").write_bytes(ocr.to_png(data) if item["kind"] == "hwp" else data)
            except Exception:  # noqa: BLE001 - no picture to show; the reads still are
                pass


# ---------------------------------------------------------------- candidate configuration


def candidate_config(settings: Settings, stage: str, inputs: Path, own: dict) -> dict:
    from ..retrieval.retrieval import DENSE_MODES

    unknown = set(own) - set(sr.VARIANT_TYPES[stage])
    if unknown:
        raise sr.ReviewError(f"{sr.VARIANT_FILE}: unknown {stage} keys {sorted(unknown)}")
    if stage == "ocr":
        return {"gpu": ["local OCR model PaddleOCR-VL"]}
    activation = json.loads((inputs / "activation.json").read_text(encoding="utf-8"))
    local = activation.get("mode") in DENSE_MODES and (activation.get("embedding") or {}).get("backend") == "local"
    return {"model": MODEL, "reasoning_effort": own.get("reasoning_effort", settings.generation_reasoning_effort),
            "max_output_tokens": own.get("max_output_tokens", settings.generation_max_output_tokens),
            "gpu": [why for why, needed in (
                (f"local query embedding model {(activation.get('embedding') or {}).get('model')}", local),
                (f"local reranker {(activation.get('reranker') or {}).get('model')}",
                 activation.get("mode") == "hybrid_rerank")) if needed]}


# ---------------------------------------------------------------- the estimate and its approval


def _target(s: Settings) -> str:
    """The ledger database as the person can recognise it: env variable, host, port and name, never the password."""
    import os

    url = urlsplit(os.environ.get(s.database_dsn_env, ""))
    return f"{s.database_dsn_env} ({url.hostname}:{url.port}{url.path})"


def _ledger_view(settings: Settings, record: dict) -> dict:
    from ..service import answers

    ls = ledger_settings(settings, record)
    view = answers._ledger(ls, PURPOSE[record["stage"]])
    with open_db(ls.db_path) as conn:
        avg, n = conn.execute("SELECT AVG(settled_micro_usd), COUNT(*) FROM attempts WHERE purpose = ? AND model = ? "
                              "AND state = 'settled'", (PURPOSE[record["stage"]], MODEL)).fetchone()
    return {**view, "ledger": _target(ls), "settled_average_micro_usd": round(avg) if n else None,
            "settled_average_n": n}


def _estimate_output(folder: Path, cand: dict) -> dict | None:
    path = folder / "candidates" / f"{cand['id']}.estimate.json"
    if cand.get("status") != "complete" or not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def estimate(settings: Settings, folder: Path, record: dict) -> dict:
    """Prices every paid call each candidate may make, writes estimate.json and returns it. The fingerprint binds the
    inputs, worker, candidates' commits and configuration, every priced item and the ledger's rates."""
    stage = record["stage"]
    ledger = _ledger_view(settings, record)
    cands = {}
    for c in record["candidates"]:
        out = _estimate_output(folder, c)
        if out is None:
            continue
        if stage == "generation":
            prices = {p["question_id"]: p["max_micro_usd"] for p in out["prices"]}
            cands[c["id"]] = {"max_micro_usd": sum(prices.values()), "calls": sum(1 for v in prices.values() if v),
                              "rows": len(prices), "prices": prices,
                              "notes": sorted({p["note"] for p in out["prices"] if p.get("note")})}
        else:
            flagged = [i["n"] for i in out["images"] if i.get("reasons")]
            per = out["per_read_micro_usd"]
            cands[c["id"]] = {"max_micro_usd": per * len(flagged), "calls": len(flagged), "flagged": flagged,
                              "images": len(out["images"]), "per_read_micro_usd": per,
                              "unreadable": sum(i["status"] == "unreadable" for i in out["images"])}
        if ledger["settled_average_micro_usd"] is not None:
            cands[c["id"]]["typical_micro_usd"] = ledger["settled_average_micro_usd"] * cands[c["id"]]["calls"]
    total = sum(c["max_micro_usd"] for c in cands.values())
    fingerprint = hashlib.sha256(dumps([record["input_sha256"], record["worker_sha256"], ledger["rate_version"],
                                        [[c["id"], c["commit"], c.get("changed_sha256"), c.get("config")]
                                         for c in record["candidates"]], cands]).encode()).hexdigest()
    now = datetime.now(timezone.utc)
    est = {"estimate_id": uuid.uuid4().hex[:12], "run_id": record["run_id"], "stage": stage, "model": MODEL,
           "purpose": PURPOSE[stage], **ledger, "candidates": cands, "max_micro_usd": total,
           "fits": ledger["paid_enabled"] and total <= min(ledger["envelope_remaining_micro_usd"],
                                                           ledger["available_micro_usd"]),
           "fingerprint": fingerprint, "created_at": now.isoformat(),
           "expires_at": (now + timedelta(hours=ESTIMATE_TTL_HOURS)).isoformat(), "approved_by": None}
    write_text_atomic(folder / "estimate.json", json.dumps(est, ensure_ascii=False, indent=1))
    return est


def load_estimate(folder: Path) -> dict:
    path = folder / "estimate.json"
    if not path.exists():
        raise sr.ReviewError(f"{folder.name} has no estimate; only generation and ocr runs are paid")
    return json.loads(path.read_text(encoding="utf-8"))


def approve(settings: Settings, run_id: str, approved_by: str) -> dict:
    folder = sr.run_folder(settings, run_id)
    est = load_estimate(folder)
    if not approved_by.strip():
        raise sr.ReviewError("approval needs the person's name (--approved-by)")
    if datetime.fromisoformat(est["expires_at"]) < datetime.now(timezone.utc):
        raise sr.ReviewError("the estimate expired; run the comparison again for a new one")
    if not est["fits"]:
        raise sr.ReviewError(f"the maximum {est['max_micro_usd'] / 1e6:.4f} USD does not fit the {est['purpose']} "
                             "envelope, the cap or paid admission of the ledger; nothing was approved")
    est.update(approved_by=approved_by.strip(), approved_at=utcnow())
    write_text_atomic(folder / "estimate.json", json.dumps(est, ensure_ascii=False, indent=1))
    ev.record_audit(settings, approved_by, "approve-stage-review-estimate", run_id,
                    f"{est['stage']}: up to {est['max_micro_usd'] / 1e6:.4f} USD", est)
    return est


def require_approved(est: dict) -> None:
    if not est.get("approved_by") or not est.get("approved_at"):
        raise sr.ReviewError(f"estimate {est.get('estimate_id')} of {est.get('run_id')} is not approved: read it, then "
                             f"`stage-review approve --run {est.get('run_id')} --approved-by <name>`")
    if datetime.fromisoformat(est["expires_at"]) < datetime.now(timezone.utc):
        raise sr.ReviewError("the approved estimate expired; run the comparison again for a new one")


def approved_env(folder: Path, est: dict) -> dict[str, str]:
    """The only way a key reaches a worker: an approved, unexpired estimate that is still this run's on disk."""
    require_approved(est)
    on_disk = load_estimate(folder)
    if on_disk.get("fingerprint") != est.get("fingerprint") or on_disk.get("approved_at") != est.get("approved_at"):
        raise sr.ReviewError("the estimate on disk is no longer the approved one")
    key = read_api_key("OPENAI_API_KEY")
    if not key:
        raise sr.ReviewError("OPENAI_API_KEY is not configured for the runner")
    env = {"OPENAI_API_KEY": key}
    creds = tracing_credentials() if est["stage"] == "generation" else None
    if creds:  # the answers are traced, and the view links to them, only when the runner has Langfuse configured
        env.update(zip(TRACING_ENV, creds))
    return env


def recheck(settings: Settings, folder: Path, record: dict, est: dict) -> None:
    """Before paying: the inputs and worker are the estimated ones, the rates unchanged, and the remaining maximum
    still fits the ledger."""
    if sr.input_hashes(folder / "inputs") != record["input_sha256"]:
        raise sr.ReviewError("the frozen inputs changed since the estimate")
    if sr._sha((folder / "worker.py").read_bytes()) != record["worker_sha256"]:
        raise sr.ReviewError("the worker changed since the estimate")
    now = _ledger_view(settings, record)
    if now["rate_version"] != est["rate_version"]:
        raise sr.ReviewError("the ledger's rates changed since the estimate; run the comparison again")
    left = sum(c["max_micro_usd"] for cid, c in est["candidates"].items() if not _finished(folder, cid))
    if not now["paid_enabled"] or left > min(now["envelope_remaining_micro_usd"], now["available_micro_usd"]):
        raise sr.ReviewError(f"the remaining maximum {left / 1e6:.4f} USD no longer fits the {est['purpose']} envelope "
                             "or the cap, or paid admission is off; nothing was dispatched")


def _finished(folder: Path, cid: str) -> bool:
    path = folder / "candidates" / f"{cid}.json"
    return path.exists() and json.loads(path.read_text(encoding="utf-8")).get("status") == "complete"


def paid_config(folder: Path, record: dict, est: dict, cand: dict) -> dict:
    mine = est["candidates"][cand["id"]]
    config = {**(cand.get("config") or {}), "run_key": record["run_id"], "candidate": cand["id"],
              "cap_micro_usd": mine["max_micro_usd"]}
    previous = folder / "candidates" / f"{cand['id']}.json"
    if record["stage"] == "generation":
        return {**config, "prices": mine["prices"], "previous": str(previous) if previous.exists() else None}
    return {**config, "ledger_env": record.get("ledger_env"), "per_read_micro_usd": mine["per_read_micro_usd"],
            "local_reads": str(folder / "candidates" / f"{cand['id']}.estimate.json"),
            "previous": str(previous) if previous.exists() else None}


def ledger_spend(settings: Settings, folder: Path, record: dict) -> None:
    """Each candidate's spend as the shared ledger records it, beside what its worker counted."""
    ls = ledger_settings(settings, record)
    with open_db(ls.db_path) as conn:
        for c in record["candidates"]:
            out = sr._output(folder, c) or {}
            if record["stage"] == "generation":
                where, arg = "r.idempotency_key LIKE ?", f"{record['run_id']}:{c['id']}:%"
            elif out.get("request_id"):
                where, arg = "r.request_id = ?", out["request_id"]
            else:
                continue
            row = conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN a.state = 'settled' THEN a.settled_micro_usd ELSE 0 END), 0), "
                "COUNT(a.attempt_id), COALESCE(SUM(CASE WHEN a.state IN ('reserved', 'dispatching', 'unknown') "
                f"THEN 1 ELSE 0 END), 0) FROM requests r JOIN attempts a ON a.request_id = r.request_id WHERE {where}",
                (arg,)).fetchone()
            c["ledger"] = {"settled_micro_usd": row[0], "attempts": row[1], "open": row[2], "target": _target(ls)}
            if record["stage"] == "generation":
                c["replayed"] = _replayed(conn, out.get("rows") or [])


def _replayed(conn, rows: list[dict]) -> list[str]:
    """Rows answered from a stored request rather than a call (their earlier output was lost): the request began
    long before the row's measured call could have, so the latency measured is the replay's, not the answer's."""
    replayed = []
    for r in rows:
        if not (r.get("request_id") and r.get("answered_at") and r.get("latency_ms") is not None):
            continue
        hit = conn.execute("SELECT created_at FROM requests WHERE request_id = ?", (r["request_id"],)).fetchone()
        created = hit and (hit[0] if isinstance(hit[0], datetime) else datetime.fromisoformat(hit[0]))
        answered = datetime.fromisoformat(r["answered_at"])
        if created and (answered - created).total_seconds() > r["latency_ms"] / 1000 + 120:
            replayed.append(r["question_id"])
    return replayed


# ---------------------------------------------------------------- generation view


def _percentile(values: list[float], q: float) -> float | None:
    from ..retrieval.dense import percentile

    return percentile(values, q)


def score_generation(settings: Settings, folder: Path, record: dict) -> dict:
    from ..retrieval.retrieval import KeywordIndex

    inputs = folder / "inputs"
    rows = json.loads((inputs / "rows.json").read_text(encoding="utf-8"))
    dataset = json.loads((inputs / "dataset.json").read_text(encoding="utf-8"))
    titles = json.loads((inputs / "documents.json").read_text(encoding="utf-8"))
    activation = json.loads((inputs / "activation.json").read_text(encoding="utf-8"))
    index = KeywordIndex.load(settings, activation["index_version"])
    chunks = {c["chunk_id"]: c for c in index.chunks}
    answered = {c["id"]: {r["question_id"]: r for r in out["rows"]}
                for c in record["candidates"] for out in [sr._output(folder, c) or _partial(folder, c)] if out}
    replayed = {c["id"]: set(c.get("replayed") or ()) for c in record["candidates"]}
    questions = []
    for row in rows:
        per = {cid: _answer(row, by_q.get(row["question_id"]), index, chunks, titles,
                            row["question_id"] in replayed.get(cid, ())) for cid, by_q in answered.items()}
        done = [p for p in per.values() if "error" not in p]
        outcomes = {json.dumps([p["outcome"], p["passed"], [r["verdict"] for r in p["required"]]]) for p in done}
        questions.append({"key": row["question_id"], "id": row["question_id"], "question": row["question"],
                          "type": row["question_type"], "mode": row.get("mode"),
                          "expected_status": row["expected_status"], "answerability": row["answerability"],
                          "docs": [titles.get(s["doc_id"], s["doc_id"]) for s in row.get("scope") or []],
                          "required": [{"claim_id": c["claim_id"], "text": _claim_text(c),
                                        "critical": c.get("criticality") == "critical"}
                                       for c in row.get("required_claims") or []],
                          "candidates": per, "differs": len(outcomes) > 1 or len(done) < len(per)})
    summary = {cid: _generation_summary([q["candidates"][cid] for q in questions]) for cid in answered}
    for c in record["candidates"]:
        if c["id"] in summary:
            summary[c["id"]]["ledger"] = c.get("ledger")
    return {"schema": sr.SCHEMA, "stage": "generation", "run_id": record["run_id"],
            "candidates": [sr._summary(c) for c in record["candidates"]], "summary": summary,
            "dataset": {k: dataset[k] for k in ("dataset", "rows")} | {"skipped": len(dataset["skipped"])},
            "activation": {k: activation.get(k) for k in ("run_id", "mode", "index_version", "dense_version")},
            "traced": any(p.get("trace_url") for q in questions for p in q["candidates"].values()),
            "questions": questions}


def _claim_text(claim: dict) -> str:
    """A required claim as the gold states it: its text, or a number with its unit and qualifiers."""
    match = claim.get("match") or {}
    if match.get("type") != "number":
        return (match.get("patterns") or [""])[0]
    qualifiers = " · ".join(q[0] for q in claim.get("qualifiers") or [] if q)
    number = f"{match.get('value')}{match.get('unit') or ''}"
    return f"{number} ({qualifiers})" if qualifiers else number


def _partial(folder: Path, cand: dict) -> dict | None:
    """A stopped candidate's answers so far: shown, never ranked as complete."""
    path = folder / "candidates" / f"{cand['id']}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() and cand.get("status") == "stopped" else None


VALIDATION = ("technical_error",)
NOT_DONE = {"blocked": "예산 게이트가 거절했습니다", "unknown_billing": "과금 결과를 알 수 없어 멈췄습니다",
            "technical": "제공자 호출 전에 실패했습니다"}


def _answer(row: dict, rec: dict | None, index, chunks: dict, titles: dict, replayed: bool = False) -> dict:
    from ..service import answers

    if rec is None:
        return {"error": "이 질문 전에 실행이 멈췄습니다"}
    base = {"cost_micro_usd": rec.get("settled_micro_usd") or 0,
            "latency_ms": None if replayed else rec.get("latency_ms"), "replayed": replayed,
            "trace_url": rec.get("trace_url"), "request_id": rec.get("request_id")}
    if rec.get("status") != "done":
        return {**base, "error": NOT_DONE.get(rec.get("status"), rec.get("status")), "detail": rec.get("reason")}
    s = answers.score_record(row, rec, index, {})
    answer = rec.get("answer") or {}
    links: dict[int, list] = {}
    for link in s["links"]:
        links.setdefault(link["claim_index"], []).append(
            {k: link[k] for k in ("evidence_id", "valid", "verbatim", "grade")})
    claims = [{"text": c.get("text", ""), "kind": c.get("kind"), "doc": titles.get(c.get("doc_id"), c.get("doc_id")),
               "evidence_ids": c.get("evidence_ids") or [], "supported": s["answer_claims"][i]["supported"],
               "links": links.get(i, [])} for i, c in enumerate(answer.get("claims") or [])]
    cited = {e for c in claims for e in c["evidence_ids"]} | {
        e for c in answer.get("conflicts") or [] for a in c.get("alternatives") or [] for e in a.get("evidence_ids") or []}
    evidence = {}
    for eid in sorted(cited):
        e = (rec.get("evidence") or {}).get(eid) or {}
        chunk = chunks.get(e.get("chunk_id")) or {}
        evidence[eid] = {"doc": titles.get(e.get("doc_id"), e.get("doc_id")), "quote": e.get("quote"),
                         "section": " > ".join(chunk.get("section_path") or []),
                         "text": chunk.get("body") or chunk.get("payload")}
    error = answer.get("error") if s["outcome"] in VALIDATION else None
    code = error.split(":", 1)[0] if error else None
    # The validator names its rule in snake_case (claim_without_evidence); anything else, such as a provider 503
    # (InternalServerError), failed the request without a verdict on the answer.
    validation = code if code and re.fullmatch(r"[a-z_]+", code) else None
    return {**base, "outcome": s["outcome"], "passed": s.get("passed"), "status_ok": s["status_ok"],
            "summary": answer.get("summary") or "", "claims": claims, "evidence": evidence,
            "conflicts": answer.get("conflicts") or [], "missing": answer.get("missing_fields") or [],
            "validation": validation, "validation_detail": error if validation else None,
            "failure": error if error and not validation else None,
            "required": [{"claim_id": c["claim_id"], "verdict": c["verdict"]} for c in s["claims"]],
            "groups": s.get("groups"), "scope_leaks": s.get("scope_leaks", 0)}


def _generation_summary(per: list[dict]) -> dict:
    done = [p for p in per if "outcome" in p]
    required = [r for p in done for r in p["required"]]
    claims = [c for p in done for c in p["claims"]]
    links = [link for c in claims for link in c["links"]]
    failures: dict[str, int] = {}
    for p in done:
        if p["validation"]:
            failures[p["validation"]] = failures.get(p["validation"], 0) + 1
    paid = [p["cost_micro_usd"] for p in per if p.get("cost_micro_usd")]
    latency = [p["latency_ms"] for p in done if p.get("latency_ms") is not None]
    return {"rows": len(per), "answered": len(done), "passed": sum(bool(p.get("passed")) for p in done),
            "required_correct": sum(r["verdict"] == "correct" for r in required), "required": len(required),
            "claims_supported": sum(c["supported"] is True for c in claims),
            "claims_rejected": sum(c["supported"] is False for c in claims), "claims": len(claims),
            "links_invalid": sum(not link["valid"] for link in links), "links": len(links),
            "validation_failures": failures, "validation_failed": sum(failures.values()),
            "technical_failures": sum(bool(p.get("failure")) for p in done),
            "cost_micro_usd": sum(paid), "paid_answers": len(paid),
            "latency_ms": {"p50": _percentile(latency, 0.5), "p95": _percentile(latency, 0.95), "n": len(latency)},
            "not_done": len(per) - len(done)}


# ---------------------------------------------------------------- OCR view


def char_diff(base: str, other: str) -> list[list[str]]:
    """Character-level edit of `base` into `other`: [tag, base text, other text] runs, equal runs included."""
    m = difflib.SequenceMatcher(None, base, other, autojunk=False)
    return [[tag, base[i1:i2], other[j1:j2]] for tag, i1, i2, j1, j2 in m.get_opcodes()]


def score_ocr(folder: Path, record: dict) -> dict:
    frozen = json.loads((folder / "inputs" / "images.json").read_text(encoding="utf-8"))
    outputs = {c["id"]: {i["n"]: i for i in out["images"]} for c in record["candidates"]
               for out in [sr._output(folder, c) or _partial(folder, c)] if out}
    base_id = record["candidates"][0]["id"]
    images = []
    for item in frozen["images"]:
        reads = {cid: by_n.get(item["n"]) for cid, by_n in outputs.items()}
        base_text = (reads.get(base_id) or {}).get("text") or ""
        per = {}
        for cid, read in reads.items():
            if read is None:
                per[cid] = {"status": "missing"}
                continue
            text = read.get("text") or ""
            per[cid] = {k: read.get(k) for k in ("status", "reasons", "mean_prob", "error", "micro_usd")} | {
                "text": text, "local_text": read.get("local_text"),
                "diff": char_diff(base_text, text) if cid != base_id and base_id in reads else None}
        texts = {json.dumps([p["status"], p.get("text")]) for p in per.values()}
        images.append({"n": item["n"], "title": item.get("title"), "doc_id": item.get("doc_id"),
                       "where": item.get("bindata") or f"p{item.get('page')}", "sample": item["sample"],
                       "cached_reasons": item["reasons"], "shown": (folder / "images" / f"{item['n']}.png").exists(),
                       "candidates": per, "differs": len(texts) > 1})
    summary = {}
    for cid, by_n in outputs.items():
        reads = list(by_n.values())
        flagged = [r for r in reads if r.get("reasons")]
        summary[cid] = {"images": len(frozen["images"]),
                        "unreadable": sum(r["status"] == "unreadable" for r in reads),
                        "read": sum(r["status"] != "unreadable" for r in reads), "flagged": len(flagged),
                        "reread": sum(r["status"] == "remote" for r in flagged),
                        "unresolved": sum(r["status"] == "unresolved" for r in flagged),
                        "not_reached": len(frozen["images"]) - len(reads),
                        "spent_micro_usd": sum(r.get("micro_usd") or 0 for r in reads),
                        "ledger": next((c.get("ledger") for c in record["candidates"] if c["id"] == cid), None)}
    return {"schema": sr.SCHEMA, "stage": "ocr", "run_id": record["run_id"],
            "candidates": [sr._summary(c) for c in record["candidates"]], "summary": summary,
            "set": {k: frozen[k] for k in ("ocr_version", "flagged", "sample", "seed")},
            "images": sorted(images, key=lambda i: (not i["differs"], i["n"]))}
