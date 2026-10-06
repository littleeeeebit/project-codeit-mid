"""Phase-4 fixture: the shared temporary corpus plus one document with an amount, its VAT note, a deadline and a
requirement code in separate elements, a family map with dev and test families, and gold-2 row builders."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from rfp_assistant.corpus import ingestion
from rfp_assistant.evaluation import evaluation
from rfp_assistant.retrieval.retrieval import build_keyword_index
from rfp_assistant.storage import store
from tests import fixtures

PDF_F = fixtures.make_pdf([
    ["제안요청서", "Ⅰ. 사업 개요", "사업명: 스마트 행정 플랫폼 구축"],
    ["Ⅱ. 사업 예산", "사업 예산은 금 130,000,000원으로 한다."],
    ["Ⅲ. 예산 단서", "위 금액은 부가가치세를 포함한 금액이다."],
    ["Ⅳ. 제출 안내", "제안서 제출 마감은 2024. 6. 11.(화) 17:00까지이다."],
    ["Ⅴ. 요구사항", "SFR-001 사용자 인증 기능을 제공하여야 한다."],
])
ROW_F = ["20240006", "0.0", "스마트 행정 플랫폼 구축", "130000000.0", "기관F", "2024-05-20 10:00:00", "",
         "2024-06-11 17:00:00", "요약", "pdf", "기관F_스마트 행정 플랫폼.pdf", "미리보기"]

AMOUNT = "사업 예산은 금 130,000,000원으로 한다."
VAT = "위 금액은 부가가치세를 포함한 금액이다."
DEADLINE = "제안서 제출 마감은 2024. 6. 11.(화) 17:00까지이다."
CODE = "SFR-001 사용자 인증 기능을 제공하여야 한다."
WARRANTY = "하자보수 기간은 검수 완료일로부터 12개월로 한다."
SEATS = "도서관 좌석 예약 시스템을 구축한다."


def make_env(root: Path, *, splits: dict[str, str] | None = None, paid: bool = True) -> fixtures.Env:
    """`splits` maps a document key (기관A, 기관D, 기관E, 기관F) to dev/test; default A, F dev and D, E test."""
    env = fixtures.make_env(root, paid=paid, index=False)
    s = env.settings
    (s.files_dir / ROW_F[10]).write_bytes(PDF_F)
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    w.writerow(fixtures.CSV_HEADER)
    w.writerows(fixtures.ROWS + [ROW_F])
    s.csv_path.write_bytes(b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8"))
    ingestion.import_manifest(s)
    ingestion.ingest(s)
    with store.open_db(s.db_path) as conn:
        f = conn.execute("SELECT doc_id, active_source_hash FROM documents WHERE filename = ?", (ROW_F[10],)).fetchone()
    env.refs["기관F"] = fixtures.DocRef(f["doc_id"], f["active_source_hash"])
    ingestion.record_review(s, f["active_source_hash"], "fixture-reviewer", "sample_checked", ["p1/i0"], {"fixture": 1})
    build_keyword_index(s, fixtures.analyzer())
    set_splits(env, splits or {"기관A": "dev", "기관F": "dev", "기관D": "test", "기관E": "test"})
    return env


def set_splits(env, splits: dict[str, str]) -> None:
    s = env.settings
    evaluation.assign_families(s)
    path = s.data_dir / "datasets" / "families.json"
    fams = json.loads(path.read_text(encoding="utf-8"))
    for key, split in splits.items():
        for f in fams["families"].values():
            if env.refs[key].doc_id in f["doc_ids"]:
                f["split"] = split
    path.write_text(json.dumps(fams, ensure_ascii=False), encoding="utf-8")


def family_of(env, key: str) -> str:
    fams = evaluation.load_families(env.settings)["families"]
    return next(k for k, f in fams.items() if env.refs[key].doc_id in f["doc_ids"])


def extraction(env, key: str) -> str:
    with store.open_db(env.settings.db_path) as conn:
        return conn.execute("SELECT active_extraction_id FROM sources WHERE source_hash = ?",
                            (env.refs[key].source_hash,)).fetchone()[0]


def element(env, key: str, like: str) -> str:
    with store.open_db(env.settings.db_path) as conn:
        row = conn.execute("SELECT element_id FROM elements WHERE extraction_id = ? AND raw_text LIKE ? "
                           "ORDER BY source_order", (extraction(env, key), like)).fetchone()
    assert row is not None, like
    return row[0]


def scope(env, key: str) -> dict:
    ref = env.refs[key]
    return {"doc_id": ref.doc_id, "source_hash": ref.source_hash, "extraction_id": extraction(env, key)}


def alt(env, key: str, like: str, quote: str) -> dict:
    ref = env.refs[key]
    return {"source_hash": ref.source_hash, "extraction_id": extraction(env, key),
            "element_id": element(env, key, like), "quote": quote}


def group(env, gid: str, key: str, *alternatives: tuple[str, str]) -> dict:
    return {"group_id": gid, "doc_id": env.refs[key].doc_id,
            "alternatives": [alt(env, key, like, quote) for like, quote in alternatives]}


def claim(cid: str, groups: list[str], match: dict, critical_kind: str | None = None, qualifiers=None) -> dict:
    return {"claim_id": cid, "match": match, "criticality": "critical" if critical_kind else "normal",
            "critical_kind": critical_kind, "qualifiers": qualifiers or [], "support_groups": groups}


def row(env, qid: str, question: str, key: str = "기관F", *, split: str = "dev", qtype: str = "direct_fact",
        groups: list[dict] | None = None, claims: list[dict] | None = None, answerability: str = "answerable",
        expected_status: str = "answered", keys: list[str] | None = None, reviewed: bool = True,
        **extra) -> dict:
    keys = keys or [key]
    mode = extra.pop("mode", "compare" if len(keys) == 2 else "single")
    r = {"dataset_version": evaluation.GOLD_SCHEMA, "question_id": qid, "revision": 1, "split": split,
         "question": question, "mode": mode, "scope": [scope(env, k) for k in keys], "as_of_date": "2024-06-01",
         "family_ids": sorted({family_of(env, k) for k in keys}), "question_type": qtype,
         "difficulty_reason": "숫자와 단서가 서로 다른 문단에 있음", "answerability": answerability,
         "expected_status": expected_status, "required_claims": claims or [], "evidence_groups": groups or [],
         "negative_validation": None,
         "review": {"drafted_by": "agent-a", "reviewed_by": "person-b" if reviewed else None,
                    "original_inspected": reviewed, "approved_at": "2026-10-01T00:00:00+00:00" if reviewed else None,
                    "status": "approved" if reviewed else "pending", "disputed": False, "second_review": None},
         "generation_provenance": {"method": "human"}}
    r.update(extra)
    return r


def amount_row(env, qid: str = "dev-amount", question: str = "이 사업 예산은 얼마이며 세금 포함인가요?", **kw) -> dict:
    groups = [group(env, "g1", "기관F", ("%130,000,000%", AMOUNT)), group(env, "g2", "기관F", ("%부가가치세%", VAT))]
    claims = [claim("c1", ["g1", "g2"], {"type": "number", "value": 130000000, "unit": "KRW"}, "amount",
                    qualifiers=[["부가가치세를 포함", "부가가치세 포함", "부가세 포함", "VAT 포함"]])]
    return row(env, qid, question, groups=groups, claims=claims, qtype="table_numeric", **kw)


def deadline_row(env, qid: str = "dev-deadline", question: str = "제안서는 언제까지 내야 하나요?", **kw) -> dict:
    groups = [group(env, "g1", "기관F", ("%마감%", DEADLINE))]
    claims = [claim("c1", ["g1"], {"type": "date", "value": "2024-06-11", "time": "17:00"}, "deadline")]
    return row(env, qid, question, groups=groups, claims=claims, **kw)


def warranty_row(env, qid: str = "dev-warranty", question: str = "검수 뒤 하자를 고쳐 주는 기간은?", **kw) -> dict:
    groups = [group(env, "g1", "기관A", ("%하자보수%", WARRANTY))]
    claims = [claim("c1", ["g1"], {"type": "number", "value": 12, "unit": "개월"})]
    return row(env, qid, question, "기관A", groups=groups, claims=claims, qtype="semantic_paraphrase", **kw)


def absent_row(env, qid: str = "dev-absent", question: str = "이 사업의 지체상금률은 얼마인가요?", **kw) -> dict:
    nv = {"scope_searched": [env.refs["기관F"].doc_id], "methods": ["full_original_read", "keyword:지체상금"],
          "locations": ["1-5쪽 전체"], "original_complete": True, "rationale": "원문 전체에 지체상금 조항이 없음"}
    return row(env, qid, question, answerability="unanswerable", expected_status="insufficient_evidence",
               qtype="missing_false_premise", negative_validation=nv, **kw)


def write(env, name: str, rows: list[dict]) -> Path:
    path = evaluation.dataset_path(env.settings, name)
    store.write_jsonl_atomic(path, rows)
    return path
