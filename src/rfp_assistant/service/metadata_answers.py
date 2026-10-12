"""Corpus metadata questions answered over metadata: the model's query translation, institution-type classification,
the deterministic query (`metadata_query`) and one cited description line per matched project, each a Langfuse span
and each paid call reserved in the shared ledger before it is sent. The service's answer path calls `route`, and takes
every answer prompt's citable metadata units from `evidence_units`."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict

from . import metadata_query as mq
from . import service
from ..contracts import AnswerRequest, AnswerResult, EvidenceUnit, Principal
from ..gateway import budget, generation, tracing
from ..retrieval.chunking import count_tokens
from ..storage.store import open_db, tx, utcnow

QUERY_MAX_OUTPUT_TOKENS = 2000  # reasoning included; the output is one small query object
CLASSIFY_BATCH = 60  # institutions per classification call
CLASSIFY_MAX_OUTPUT_TOKENS = 6000
DESCRIBE_BATCH = 12  # projects per description call: a long list never meets the output cap or one refusal
DESCRIBE_MAX_OUTPUT_TOKENS = 4000
OVERVIEW_QUERY = "사업 개요 사업 목적 추진 배경 주요 내용"  # locates each project's overview passages, not a question
OVERVIEW_PASSAGES = 3  # per project: the best-scoring chunk is often a table of contents or a form, the next ones not


def _soft_fail(record: dict):
    """A `_metered_chat` failure that leaves the request running: recorded in `record`, returned as None."""
    def fail(status: str, summary: str, request_status: str = "completed", **kw) -> None:
        record["failed"] = {"status": status, "summary": summary, "error": kw.get("error")}
    return fail


def route(res: service.Resources, principal: Principal, request_id: str, request: AnswerRequest, question: str,
          trace: dict, done) -> AnswerResult | None:
    """A corpus question answered over metadata (`metadata_query`), or None to answer it from passages as before:
    the model said it is not a metadata question, or its query failed validation (the reason is traced). Every call
    is reserved in the shared ledger before it is sent; a refused or failed translation ends the request."""
    docs = service._doc_rows(res)
    institutions = sorted({d["effective"]["institution"] for d in docs if d["effective"].get("institution")})
    record = trace["metadata_query"] = {"prompt_version": mq.QUERY_PROMPT_VERSION}
    messages, rf = mq.query_messages(question, request.as_of, institutions), mq.query_json_schema()
    with tracing.step("metadata-query-translation", input={"question": question, "as_of": request.as_of}) as span:
        response = service._metered_chat(
            res, principal, request_id, stage="metadata_query", step="translate-query",
            prompt_version=mq.QUERY_PROMPT_VERSION, messages=messages, response_format=rf,
            input_tokens=generation.count_request_tokens(messages, rf, res.settings.framing_margin_tokens),
            max_output_tokens=QUERY_MAX_OUTPUT_TOKENS, record=record, fail=done)
        if isinstance(response, AnswerResult):
            span.update(output={"failed": response.status, "error": response.error}, level="ERROR")
            return response
        record["raw_output"] = (response.content or "")[:4000]
        try:
            generation._complete(response)
            query = mq.validate_query(response.content)
        except (generation.TechnicalError, mq.QueryInvalid) as exc:
            record["fallback_reason"] = str(exc)[:300]
            span.update(lambda: {"output": {"raw_output": tracing.readable(response.content),
                                            "fallback_reason": record["fallback_reason"]}})
            return None
        record["query"] = query
        span.update(lambda: {"output": {"raw_output": tracing.readable(response.content), "query": query}})
    types = _institution_types(res)
    unclassified = [n for n in institutions if n not in types]
    if unclassified:  # each institution once; the types are kept for every later question
        _classify_institutions(res, principal, request_id, unclassified, record)
        types = _institution_types(res)
    rows = mq.metadata_rows(docs, types)
    with tracing.step("metadata-query", input={"query": query, "projects": len(rows)}) as span:
        found = mq.run_query(query, rows)
        review_reasons = [mq.REASONS[r["reason"]] for row in found["review"] for r in row["reasons"]]
        record["result"] = {"matched": [r["doc_id"] for r in found["matched"]],
                            "review": {r["doc_id"]: [x["reason"] for x in r["reasons"]] for r in found["review"]}}
        span.update(lambda: {"output": {
            "conditions": mq.describe_query(query), "matched_count": len(found["matched"]),
            "review_count": len(found["review"]), "review_reasons": dict(Counter(review_reasons)),
            "matched": [{"doc_id": r["doc_id"], "title": r["title"]} for r in found["matched"]],
            "review": [{"doc_id": r["doc_id"], "title": r["title"], "reasons": [x["reason"] for x in r["reasons"]]}
                       for r in found["review"]]}})
    by_id = {d["doc_id"]: d for d in docs}
    described, units = {}, {}
    if not query["count_only"] and found["matched"]:
        units = _overview_passages(res, found["matched"], by_id)
        described = _describe_projects(res, principal, request_id, found["matched"], units, record)
    listing = {"query": query, "conditions": mq.describe_query(query), "count": len(found["matched"]),
               "count_only": query["count_only"],
               "fields": list(dict.fromkeys([*(f["field"] for f in query["filters"]),
                                             *([query["sort"]["field"]] if query["sort"] else [])])),
               "rows": [_listing_row(r, described, units, query["count_only"]) for r in found["matched"]],
               "review": [{**_listing_row(r, {}, {}, True),
                           "reasons": [{**x, "label": mq.REASONS[x["reason"]]} for x in r["reasons"]]}
                          for r in found["review"]]}
    n, k = len(found["matched"]), len(found["review"])
    summary = (f"조건에 맞는 사업은 {n}개입니다." if query["count_only"] else
               f"조건에 맞는 사업 {n}개입니다." if n else "조건에 맞는 사업이 없습니다.")
    if k:
        summary += f" 값이 없거나 검토가 필요한 {k}개는 확인 필요에 따로 표시했습니다."
    evidence = {u.evidence_id: asdict(u) for doc, d in described.items() for u in units[doc]
                if u.evidence_id in d["evidence_ids"]}
    return done("answered", summary, listing=listing, evidence=evidence,
                limitations=[f"metadata_query:{x}" for x in record.get("description_limitations", [])])


def _listing_row(row: dict, described: dict, units: dict, count_only: bool) -> dict:
    d = described.get(row["doc_id"])
    note = None if d or count_only else ("원문 색인 없음" if row["doc_id"] not in units else "설명 없음")
    return {k: row[k] for k in ("doc_id", "source_hash", "title", "institution", "institution_type", "type_source",
                                "amount_krw", "published_at", "bid_start", "bid_close")} | {
        "description": d["description"] if d else None, "evidence_ids": d["evidence_ids"] if d else [], "note": note}


def _institution_types(res: service.Resources) -> dict[str, dict]:
    with open_db(res.settings.db_path) as conn:
        return {r["institution"]: dict(r) for r in conn.execute("SELECT * FROM institution_types")}


def evidence_units(res: service.Resources, docs: list[dict]) -> list[EvidenceUnit]:
    """The citable metadata units of the documents in an answer prompt, M1, M2, ... in document order: one per typed
    field (`metadata_query.metadata_statements`), from the effective metadata. A unit has no chunk: validation
    compares its text with the document's current statement (`current_statements`), as a passage's with the index."""
    types = _institution_types(res) if docs else {}
    units: list[EvidenceUnit] = []
    for d in docs:
        for s in mq.metadata_statements(d, types):
            units.append(EvidenceUnit(f"M{len(units) + 1}", d["doc_id"], d["active_source_hash"], "", "", [], s["text"],
                                      {"format": "metadata", "field": s["field"], "value": s["value"],
                                       "source": s["source"]}, count_tokens(s["text"])))
    return units


def is_unit(e: EvidenceUnit) -> bool:
    return (e.location or {}).get("format") == "metadata"


def current_statements(res: service.Resources, evidence: list[EvidenceUnit], docs: list[dict]) -> dict[str, str | None]:
    """Each metadata unit of `evidence` by ID: its document's statement of that field in the stored metadata now."""
    units = [e for e in evidence if is_unit(e)]
    if not units:
        return {}
    now = {(u.doc_id, u.location["field"]): u.quote
           for u in evidence_units(res, service._doc_rows(res, [d["doc_id"] for d in docs]))}
    return {e.evidence_id: now.get((e.doc_id, e.location["field"])) for e in units}


def _classify_institutions(res: service.Resources, principal: Principal, request_id: str, names: list[str],
                           record: dict) -> None:
    """Classifies institutions with no stored type and stores what validated, with its provenance (model, this
    request as the run, date); the first stored type of an institution is kept. A refused or failed call leaves
    the rest unclassified (유형 미분류) and the answer goes on."""
    for start in range(0, len(names), CLASSIFY_BATCH):
        batch = names[start:start + CLASSIFY_BATCH]
        principal = service._checkpoint(res, request_id, principal)
        messages, rf = mq.classification_messages(batch), mq.classification_json_schema()
        rec = {"institutions": len(batch)}
        record.setdefault("classification", []).append(rec)
        with tracing.step("classify-institutions", input={"institutions": batch}) as span:
            response = service._metered_chat(
                res, principal, request_id, stage="institution_type", step="classify-call",
                prompt_version=mq.CLASSIFY_PROMPT_VERSION, messages=messages, response_format=rf,
                input_tokens=generation.count_request_tokens(messages, rf, res.settings.framing_margin_tokens),
                max_output_tokens=CLASSIFY_MAX_OUTPUT_TOKENS, record=rec, fail=_soft_fail(rec))
            if response is None:
                span.update(output=rec["failed"], level="ERROR")
                return
            try:
                generation._complete(response)
                types = mq.validate_classification(response.content, batch)
            except (generation.TechnicalError, mq.QueryInvalid) as exc:
                rec["invalid"] = str(exc)[:300]
                span.update(output={"invalid": rec["invalid"]}, level="WARNING")
                continue
            with open_db(res.settings.db_path) as conn, tx(conn, immediate=True):
                for name, kind in types.items():
                    conn.execute("INSERT INTO institution_types(institution, type, model, run_id, prompt_version, "
                                 "classified_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(institution) DO NOTHING",
                                 (name, kind, res.generation_model(), request_id, mq.CLASSIFY_PROMPT_VERSION,
                                  utcnow()))
            rec["classified"] = len(types)
            span.update(output={"types": types, "unclassified": [n for n in batch if n not in types]})


def _overview_passages(res: service.Resources, rows: list[dict],
                       by_id: dict[str, dict]) -> dict[str, list[EvidenceUnit]]:
    """Each project's overview passages in the serving index (its OVERVIEW_PASSAGES best chunks for OVERVIEW_QUERY,
    in document order), numbered P1, P2, ... across the list; a project whose original is not indexed has none."""
    from ..retrieval.retrieval import _location

    idx = res.index()
    if idx is None:
        return {}
    scores = idx.bm25.get_scores(res.analyzer.tokens(OVERVIEW_QUERY))
    units: dict[str, list[EvidenceUnit]] = {}
    n = 0
    for row in rows:
        d = by_id[row["doc_id"]]
        chunk_rows = idx.rows_by_extraction.get(d["active_extraction_id"] or "") if d["parse_status"] == "parsed" \
            else None
        if not chunk_rows:
            continue
        units[row["doc_id"]] = []
        for i in sorted(sorted(chunk_rows, key=lambda i: (-scores[i], i))[:OVERVIEW_PASSAGES]):
            c, n = idx.chunks[i], n + 1
            units[row["doc_id"]].append(EvidenceUnit(
                f"P{n}", row["doc_id"], d["active_source_hash"], c["extraction_id"], c["chunk_id"],
                [s["element_id"] for s in c["spans"]], c["body"], _location(idx, c), c["token_count"]))
    return units


def _describe_projects(res: service.Resources, principal: Principal, request_id: str, rows: list[dict],
                       units: dict[str, list[EvidenceUnit]], record: dict) -> dict[str, dict]:
    """One cited line per project, DESCRIBE_BATCH projects per call. Every batch is priced first: a list the shared
    allowance cannot cover whole is left undescribed. A batch whose output fails validation leaves its projects
    undescribed; a refused or failed call stops the remaining batches. Returns {doc_id: {description, evidence_ids}}."""
    projects = [{"doc_id": r["doc_id"], "title": r["title"], "institution": r["institution"],
                 "passages": [{"evidence_id": u.evidence_id, "text": u.quote} for u in units[r["doc_id"]]]}
                for r in rows if r["doc_id"] in units]
    batches = [projects[i:i + DESCRIBE_BATCH] for i in range(0, len(projects), DESCRIBE_BATCH)]
    rf = mq.description_json_schema()
    plan = [(b, mq.description_messages(b)) for b in batches]
    sizes = [generation.count_request_tokens(m, rf, res.settings.framing_margin_tokens) for _, m in plan]
    limitations = record.setdefault("description_limitations", [])
    try:
        total = sum(budget.estimate(res.settings.db_path, res.generation_model(), t, DESCRIBE_MAX_OUTPUT_TOKENS)
                    for t in sizes)
    except budget.BudgetError:
        limitations.append("descriptions_unknown_rate")
        return {}
    record["descriptions"] = {"batches": len(batches), "estimate_micro_usd": total, "runs": []}
    if budget.headroom(res.settings.db_path, res.paid_purpose) < total:
        limitations.append("descriptions_over_allowance")
        return {}
    described: dict[str, dict] = {}
    for k, ((batch, messages), tokens) in enumerate(zip(plan, sizes), 1):
        principal = service._checkpoint(res, request_id, principal)
        rec = {"batch": k, "doc_ids": [p["doc_id"] for p in batch]}
        record["descriptions"]["runs"].append(rec)
        with tracing.step(f"describe-batch-{k}", input={"projects": [p["doc_id"] for p in batch]}) as span:
            response = service._metered_chat(
                res, principal, request_id, stage="project_description", step="describe-projects",
                prompt_version=mq.DESCRIBE_PROMPT_VERSION, messages=messages, response_format=rf,
                input_tokens=tokens, max_output_tokens=DESCRIBE_MAX_OUTPUT_TOKENS, record=rec,
                fail=_soft_fail(rec))
            if response is None:
                span.update(output=rec["failed"], level="ERROR")
                limitations.append(f"descriptions_stopped_at_batch_{k}")
                break
            try:
                generation._complete(response)
                found = mq.validate_descriptions(response.content, batch)
            except (generation.TechnicalError, mq.QueryInvalid) as exc:
                rec["invalid"] = str(exc)[:300]
                limitations.append(f"descriptions_invalid_batch_{k}")
                span.update(output={"invalid": rec["invalid"]}, level="WARNING")
                continue
            described.update(found)
            undescribed = [p["doc_id"] for p in batch if p["doc_id"] not in found]
            if undescribed:
                limitations.append(f"descriptions_incomplete_batch_{k}")
            span.update(output={"described": found, "undescribed": undescribed},
                        level="WARNING" if undescribed else "DEFAULT")
    return described
