"""Run the same offline observations against either isolated source snapshot."""

from __future__ import annotations

import json
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict
from pathlib import Path


def replay(bundle: dict, workspace: Path) -> list[dict]:
    from rank_bm25 import BM25Okapi
    from rfp_assistant.contracts import DocRef, EvidenceUnit
    from rfp_assistant.corpus import ingestion
    from rfp_assistant.gateway import generation, tracing
    from rfp_assistant.retrieval import chunking, dense, models, retrieval
    from rfp_assistant.settings import Settings

    observations = []

    def observe(key, stage, inputs, action):
        started = time.perf_counter()
        row = {"id": key, "stage": stage, "input": inputs}
        try:
            output, checks = action()
            row.update(output=output, checks=checks)
        except Exception as exc:
            row.update(output=None, checks={"실행 완료": False}, error=f"{type(exc).__name__}: {exc}")
        row["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        observations.append(row)
        return row

    settings = Settings(source_dir=workspace, data_dir=workspace, hwp_converter=None, provider="fake")
    observe("settings", "parameters", {}, lambda: (
        {k: v for k, v in asdict(settings).items() if k not in
         ("source_dir", "data_dir", "hwp_converter", "database_dsn_env")}, {}))
    observe("models", "models", {}, lambda: (
        {"embeddings": {k: asdict(v) for k, v in models.EMBEDDINGS.items()},
         "rerankers": {k: asdict(v) for k, v in models.RERANKERS.items()}}, {}))

    chunks, elements, extraction_rows, scope = [], {}, {}, {}
    for doc in bundle["documents"]:
        extraction = doc["id"] + "-extraction"
        parsed = observe("parse:" + doc["id"], "data", doc, lambda: (
            ingestion.finalize_elements(ingestion.walk_hwp(ET.fromstring(doc["xml"])), extraction), {}))
        if parsed["output"] is None:
            continue
        els = parsed["output"]
        text = "\n".join(e["raw_text"] for e in els)
        parsed["checks"] = {"원문 보존: " + phrase: phrase in text for phrase in doc["must_preserve"]}
        built = observe("chunks:" + doc["id"], "data", els,
                        lambda: (dict(zip(("chunks", "inventory"), chunking.build_chunks(els, extraction))), {}))
        if built["output"] is None:
            continue
        new = built["output"]["chunks"]
        built["checks"] = {"원문 보존: " + phrase: any(phrase in c["body"] for c in new)
                           for phrase in doc["must_preserve"]}
        extraction_rows[extraction] = list(range(len(chunks), len(chunks) + len(new)))
        chunks.extend(new)
        elements.update({(extraction, e["element_id"]): e for e in els})
        scope[doc["id"]] = (DocRef(doc["id"], doc["source_hash"]), extraction)

    analyzer = retrieval.Analyzer()
    index = None
    if chunks:
        tokens = [analyzer.tokens(c["payload"]) or ["∅"] for c in chunks]
        index = retrieval.KeywordIndex("notebook", "reviewed_only", chunks, BM25Okapi(tokens),
                                       extraction_rows, elements)
    for question in bundle["questions"]:
        key = question["id"]
        observe("tokens:" + key, "bm25", question["question"],
                lambda: (analyzer.tokens(question["question"]), {}))

        def search():
            if index is None or any(doc not in scope for doc in question["documents"]):
                raise ValueError("required source did not parse; retrieval cannot be evaluated")
            result = retrieval.retrieve(settings, index, analyzer, question["question"],
                                        [scope[d] for d in question["documents"]], mode="kiwi_bm25")
            output = asdict(result)
            output.pop("trace_id")
            output.pop("timings_ms")
            output["ranked_passages"] = [index.chunks[index.row_of[c]]["payload"] for c in result.ranking]
            quotes = "\n".join(e.quote for e in result.evidence)
            checks = {"필요한 근거: " + text: text in quotes for text in question["required"]}
            checks.update({"제외할 근거: " + text: text not in quotes for text in question.get("forbidden", [])})
            checks["선택 문서 범위 유지"] = all(e.doc_id in question["documents"] for e in result.evidence)
            if question.get("expect_empty"):
                checks["없는 근거를 반환하지 않음"] = not result.evidence
            return output, checks

        searched = observe("search:" + key, "retriever", question, search)
        if searched["output"] is not None:
            result = searched["output"]
            evidence = [EvidenceUnit(**e) for e in result["evidence"]]

            def prompt():
                messages = generation.build_messages(question["question"], bundle["as_of"],
                    [{"doc_id": d, "title": d} for d in question["documents"]], evidence, result["limitations"])
                count = generation.count_request_tokens(messages, generation.answer_json_schema(),
                                                        settings.framing_margin_tokens)
                return {"messages": messages, "request_tokens": count}, {
                    "근거가 사용자 데이터에 포함됨":
                        all(e.quote in json.loads(messages[-1]["content"])["evidence"][i]["text"]
                            for i, e in enumerate(evidence))}

            observe("prompt:" + key, "context", {"question": question["question"], "evidence": result["evidence"]}, prompt)

    for case in bundle["fusion"]:
        def fusion():
            fused = retrieval.fuse(settings, case["lexical"], case["dense"])
            ids = [key for key, _ in fused]
            return fused, {"후보 누락 없음": set(ids) == set(case["lexical"] + case["dense"]),
                           "중복 없음": len(ids) == len(set(ids))}
        observe("fusion:" + case["id"], "retriever", case, fusion)

    for case in bundle["vectors"]:
        def vector():
            try:
                values = dense.unit_vector(case["values"], case["dimensions"]).tolist()
                return {"values": values}, {"기대 결과": not case["reject"],
                    "벡터 길이 1": abs(sum(v * v for v in values) - 1) < 1e-5}
            except dense.DenseError as exc:
                return {"rejected": str(exc)}, {"기대 결과": case["reject"]}
        observe("vector:" + case["id"], "dense", case, vector)

    for case in bundle["answers"]:
        def answer():
            evidence = [EvidenceUnit(**e) for e in case["evidence"]]
            response = generation.ProviderResponse(json.dumps(case["answer"], ensure_ascii=False), None,
                                                    "stop", None, "recorded")
            try:
                result = generation.validate_answer(response, evidence, set(case["documents"]),
                                                     {e.evidence_id: e.quote for e in evidence})
                return {"accepted": True, "answer": result.model_dump()}, {"기대 판정": case["accept"]}
            except generation.TechnicalError as exc:
                return {"accepted": False, "reason": str(exc)}, {"기대 판정": not case["accept"]}
        observe("answer:" + case["id"], "generation", case, answer)

    for case in bundle["logging"]:
        observe("logging:" + case["id"], "logging", case["input"],
                lambda: (tracing.redact(case["input"]),
                         {"기대 마스킹": tracing.redact(case["input"]) == case["expected"]}))
    return observations


def main():
    snapshot, input_path, output_path = map(Path, sys.argv[1:])
    sys.path.insert(0, str(snapshot / "src"))

    # A replay must fail visibly if a future edit tries to reach a database or paid service.
    def offline(event, args):
        if event in ("socket.connect", "socket.getaddrinfo"):
            raise RuntimeError("network access is disabled in notebook replay")
    sys.addaudithook(offline)
    try:
        import rfp_assistant
        if not Path(rfp_assistant.__file__).resolve().is_relative_to(snapshot.resolve()):
            raise RuntimeError("the worker imported code outside its snapshot")
        result = {"observations": replay(json.loads(input_path.read_text(encoding="utf-8")), snapshot)}
    except Exception as exc:
        result = {"observations": [], "error": f"{type(exc).__name__}: {exc}"}
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
