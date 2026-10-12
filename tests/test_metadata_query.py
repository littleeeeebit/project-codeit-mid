"""Corpus metadata questions: the model's query structure (fixed here per question), its validation, and the complete
set of matching projects computed by the service over every project's metadata, on real PostgreSQL with the fake
provider. Each pinned question's expected set is computed directly from the stored metadata by the oracle below,
independently of `metadata_query.run_query`."""

import gzip
import hashlib
import json
import os
import tempfile
import unittest
import uuid
from dataclasses import fields
from pathlib import Path
from unittest import mock

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from rfp_assistant.contracts import AnswerRequest, AnswerResult
from rfp_assistant.corpus import ingestion
from rfp_assistant.evaluation import error_analysis, similar_cases, traffic_review
from rfp_assistant.gateway import budget, generation, tracing
from rfp_assistant.service import metadata_answers as ma
from rfp_assistant.service import metadata_query as mq
from rfp_assistant.service import service
from rfp_assistant.storage import store
from tests import fixtures

AS_OF = "2024-09-30"
# Projects added beside the fixture corpus's four (기관A and 기관C share one original and conflict on institution,
# notice and closing time; 기관D has no amount and no closing date; 기관E is 0원). Traps: a university sports council
# and a non-university project with 대학생 in its title, a university hospital, 0원, 1원, a missing amount, an amount
# in conflict, a tie for the cheapest, and the 50,000,000원 and 100,000,000원 boundaries.
PROJECTS = [  # title, institution, amount, published, bid_close, conflicting fields
    ("학사 정보시스템 고도화", "한국대학교", 150_000_000, "2024-03-02", "2024-03-20", ()),
    ("도서관 이용 시스템 구축", "한국대학교", 80_000_000, "2024-04-01", "2024-04-15", ()),
    ("통합 행정 시스템", "한국대학교", 100_000_000, "2024-05-03", "2024-05-24", ()),
    ("연구관리 시스템", "서울과학기술대학교", 50_000_000, "2024-06-03", "2024-06-17", ()),
    ("캠퍼스 안전 시스템", "서울과학기술대학교", 0, "2024-06-10", "2024-06-28", ()),
    ("대학생 진로 지원 플랫폼", "한국교육학술정보원", 120_000_000, "2024-02-01", "2024-02-20", ()),
    ("디지털 교과서 플랫폼", "한국교육학술정보원", 30_000_000, "2024-07-01", "2024-07-15", ()),
    ("대학 스포츠 통합 플랫폼", "대학스포츠협의회", 90_000_000, "2024-05-20", "2024-06-05", ()),
    ("스마트 교통 시스템", "부산광역시", 300_000_000, "2024-01-10", "2024-02-01", ()),
    ("민원 챗봇 구축", "부산광역시", 50_000_000, "2024-08-01", "2024-08-30", ()),
    ("CCTV 통합관제", "경기도 화성시", 1, "2024-04-10", "2024-04-30", ()),
    ("주민 참여 플랫폼", "경기도 화성시", None, "2024-05-10", None, ()),
    ("전자정부 표준 프레임워크", "행정안전부", 1_200_000_000, "2024-06-01", "2024-06-20", ()),
    ("재난안전 데이터 공유", "행정안전부", 200_000_000, "2024-03-15", "2024-04-01", ("amount_krw",)),
    ("전력 데이터 분석", "한국전력공사", 30_000_000, "2024-09-01", "2024-09-20", ()),
    ("바이오 데이터 저장소", "국립보건연구원", 450_000_000, "2024-02-15", "2024-03-05", ()),
    ("의료 영상 판독 시스템", "서울대학교병원", 220_000_000, "2024-07-20", "2024-08-09", ()),
]
# What the fake classifier answers; the fixture's 기관A/C/D/E become 기타.
TYPES = {"한국대학교": "대학", "서울과학기술대학교": "대학", "한국교육학술정보원": "공공기관", "대학스포츠협의회": "협회·단체",
         "부산광역시": "지방자치단체", "경기도 화성시": "지방자치단체", "행정안전부": "중앙행정기관", "한국전력공사": "공공기관",
         "국립보건연구원": "연구기관", "서울대학교병원": "의료기관"}


def model(filters=(), sort=None, limit=None, count_only=False) -> dict:
    """The model's output for a metadata question (values as strings, as the provider returns them)."""
    return {"metadata_question": True, "filters": [{"field": f, "operator": o, "values": list(v)} for f, o, v in filters],
            "sort": dict(zip(("field", "direction"), sort)) if sort else None, "limit": limit, "count_only": count_only}


def query(filters=(), sort=None, limit=None, count_only=False) -> dict:
    """The validated structure the server runs."""
    return {"filters": [{"field": f, "operator": o, "value": v} for f, o, v in filters],
            "sort": dict(zip(("field", "direction"), sort)) if sort else None, "limit": limit, "count_only": count_only}


# ---------------------------------------------------------------- the oracle: plain reads of the stored metadata


def amount(p):
    a = p["amount_krw"]
    return None if "amount_krw" in p["conflicts"] or a is None or a < 1_000_000 else a


def day(p, field):
    v = p[field]
    return None if field in p["conflicts"] or not v else v["value"][:10]


def kind(p):
    """A conflicting institution decides the type only when every competing name has the same type."""
    if p["corrected"] or "institution" not in p["conflicts"]:
        return p["type"]
    types = {TYPES.get(v, "기타") for v in p["institution_alternatives"]}
    return types.pop() if len(types) == 1 else None


def name(p):
    return None if "institution" in p["conflicts"] else p["institution"]


def extreme(table, value, highest, n=1):
    """Every project whose valid value is among the n lowest (or highest), ties included."""
    ranked = sorted((value(p) for p in table if value(p) is not None), reverse=highest)
    edge = ranked[min(n, len(ranked)) - 1]
    return {p["doc_id"] for p in table if value(p) is not None and (value(p) >= edge if highest else value(p) <= edge)}


# question -> (model output, pinned validated structure, oracle over the stored table)
PINNED = {
    "사업 예산이 1억원 이상인 사업들 리스트와, 그 사업들이 어떤 사업인지 간략하게 알려줘.": (
        model([("amount_krw", ">=", ["1억원"])]), query([("amount_krw", ">=", 100_000_000)]),
        lambda t: {p["doc_id"] for p in t if (amount(p) or 0) >= 100_000_000}),
    "5천만원 이상인 사업": (
        model([("amount_krw", ">=", ["5천만원"])]), query([("amount_krw", ">=", 50_000_000)]),
        lambda t: {p["doc_id"] for p in t if (amount(p) or 0) >= 50_000_000}),
    "가장 싼 사업": (
        model(sort=("amount_krw", "asc"), limit=1), query(sort=("amount_krw", "asc"), limit=1),
        lambda t: extreme(t, amount, highest=False)),
    "대학에서 낸 제안서의 가격 목록": (
        model([("institution_type", "=", ["대학"])], sort=("amount_krw", "desc")),
        query([("institution_type", "=", "대학")], sort=("amount_krw", "desc")),
        lambda t: {p["doc_id"] for p in t if kind(p) == "대학" and amount(p)}),
    "대학에서 낸 제안서 중 1억 이상인 제안서": (
        model([("institution_type", "=", ["대학"]), ("amount_krw", ">=", ["1억"])]),
        query([("institution_type", "=", "대학"), ("amount_krw", ">=", 100_000_000)]),
        lambda t: {p["doc_id"] for p in t if kind(p) == "대학" and (amount(p) or 0) >= 100_000_000}),
    "예산 규모가 제일 큰 사업 세 개만 뽑아줘": (
        model(sort=("amount_krw", "desc"), limit=3), query(sort=("amount_krw", "desc"), limit=3),
        lambda t: extreme(t, amount, highest=True, n=3)),
    "1억 넘는 사업이 몇 개나 돼?": (
        model([("amount_krw", ">", ["1억"])], count_only=True),
        query([("amount_krw", ">", 100_000_000)], count_only=True),
        lambda t: {p["doc_id"] for p in t if (amount(p) or 0) > 100_000_000}),
    "오천만원도 안 되는 저예산 사업들": (
        model([("amount_krw", "<", ["50,000,000원"])]), query([("amount_krw", "<", 50_000_000)]),
        lambda t: {p["doc_id"] for p in t if amount(p) and amount(p) < 50_000_000}),
    "8천만~2억 원 규모 사업": (
        model([("amount_krw", "between", ["8천만", "2억 원"])]),
        query([("amount_krw", "between", [80_000_000, 200_000_000])]),
        lambda t: {p["doc_id"] for p in t if amount(p) and 80_000_000 <= amount(p) <= 200_000_000}),
    "지자체 발주 사업 보여줘": (
        model([("institution_type", "=", ["지방자치단체"])]), query([("institution_type", "=", "지방자치단체")]),
        lambda t: {p["doc_id"] for p in t if kind(p) == "지방자치단체"}),
    "부산광역시가 발주한 사업 목록": (
        model([("institution", "=", ["부산광역시"])]), query([("institution", "=", "부산광역시")]),
        lambda t: {p["doc_id"] for p in t if name(p) == "부산광역시"}),
    "한국전력이 낸 공고 있어?": (
        model([("institution", "contains", ["한국전력"])]), query([("institution", "contains", "한국전력")]),
        lambda t: {p["doc_id"] for p in t if "한국전력" in (name(p) or "")}),
    "2024년 6월 중에 입찰 마감된 사업": (
        model([("bid_close", "between", ["2024-06-01", "2024-06-30"])]),
        query([("bid_close", "between", ["2024-06-01", "2024-06-30"])]),
        lambda t: {p["doc_id"] for p in t if "2024-06-01" <= (day(p, "bid_close") or "") <= "2024-06-30"}),
    "입찰 마감이 가장 늦은 사업은?": (
        model(sort=("bid_close", "desc"), limit=1), query(sort=("bid_close", "desc"), limit=1),
        lambda t: extreme(t, lambda p: day(p, "bid_close"), highest=True)),
    "공공기관 사업 중 가장 비싼 건 뭐야": (
        model([("institution_type", "=", ["공공기관"])], sort=("amount_krw", "desc"), limit=1),
        query([("institution_type", "=", "공공기관")], sort=("amount_krw", "desc"), limit=1),
        lambda t: extreme([p for p in t if kind(p) == "공공기관"], amount, highest=True)),
    "대학 말고 다른 기관이 낸 3억 이상 사업": (
        model([("institution_type", "!=", ["대학"]), ("amount_krw", ">=", ["3억"])]),
        query([("institution_type", "!=", "대학"), ("amount_krw", ">=", 300_000_000)]),
        lambda t: {p["doc_id"] for p in t if kind(p) and kind(p) != "대학" and (amount(p) or 0) >= 300_000_000}),
    "중앙부처가 발주한 사업은 총 몇 건?": (
        model([("institution_type", "=", ["중앙행정기관"])], count_only=True),
        query([("institution_type", "=", "중앙행정기관")], count_only=True),
        lambda t: {p["doc_id"] for p in t if kind(p) == "중앙행정기관"}),
    "2024년 5월 이후 공고된 2억 이하 사업": (
        model([("published_at", ">=", ["2024-05-01"]), ("amount_krw", "<=", ["2억"])]),
        query([("published_at", ">=", "2024-05-01"), ("amount_krw", "<=", 200_000_000)]),
        lambda t: {p["doc_id"] for p in t if (day(p, "published_at") or "") >= "2024-05-01" and amount(p)
                   and amount(p) <= 200_000_000}),
}


def reply(body: dict, messages) -> generation.ProviderResponse:
    return generation.fake_json(body, messages=messages)


EXTRA = {"모든 사업 목록": model()}  # model outputs for questions only one test asks


def pinned_translator(messages):
    asked = json.loads(messages[-1]["content"])["question"]
    return reply(EXTRA[asked] if asked in EXTRA else PINNED[asked][0], messages)


def classifier(messages):
    names = json.loads(messages[-1]["content"])["institutions"]
    return reply({"items": [{"institution": n, "type": TYPES.get(n, "기타")} for n in names]}, messages)


def add_projects(settings) -> None:
    """The PROJECTS as CSV associations whose originals were not ingested (no passage to describe)."""
    def when(d):
        return {"value": d, "precision": "date"} if d else None

    with store.open_db(settings.db_path) as conn, store.tx(conn, immediate=True):
        for i, (title, inst, amt, published, close, conflicts) in enumerate(PROJECTS, 100):
            h = hashlib.sha256(title.encode()).hexdigest()
            conn.execute("INSERT INTO sources(source_hash, format, original_path, parse_status, reason_code) "
                         "VALUES (?, 'hwp', ?, 'quarantined', 'hwp_converter_failed')", (h, f"/nowhere/{i}.hwp"))
            meta = {"notice": None, "revision": 0, "title": title, "amount_krw": amt, "institution": inst,
                    "published_at": when(published), "bid_start": None, "bid_close": when(close), "format": "hwp"}
            quality = {"flags": [], "shared_source_with": [],
                       "provenance_conflicts": [{"field": f, "values": {"x": amt, "y": (amt or 0) + 1}}
                                                for f in conflicts]}
            conn.execute("INSERT INTO documents(doc_id, csv_row_id, filename, active_source_hash, raw_metadata_json, "
                         "normalized_metadata_json, quality_json) VALUES (?, ?, ?, ?, '{}', ?, ?)",
                         (str(uuid.uuid4()), i, f"{i}.hwp", h, json.dumps(meta, ensure_ascii=False),
                          json.dumps(quality, ensure_ascii=False)))


def stored_table(settings) -> list[dict]:
    """Every project's metadata as stored, with its fixed classification or a recorded type correction."""
    with store.open_db(settings.db_path) as conn:
        docs = conn.execute("SELECT doc_id, normalized_metadata_json, quality_json FROM documents").fetchall()
        fixes = {r["doc_id"]: json.loads(r["value_json"]) for r in conn.execute(
            "SELECT doc_id, value_json FROM metadata_resolutions WHERE field = 'institution_type' ORDER BY created_at")}
    table = []
    for d in docs:
        m = json.loads(d["normalized_metadata_json"])
        conflicts = {c["field"]: c["values"] for c in json.loads(d["quality_json"])["provenance_conflicts"]}
        table.append({**m, "doc_id": d["doc_id"], "corrected": d["doc_id"] in fixes,
                      "type": fixes.get(d["doc_id"], TYPES.get(m["institution"], "기타")), "conflicts": set(conflicts),
                      "institution_alternatives": list(conflicts.get("institution", {}).values())})
    return table


class PureTest(unittest.TestCase):
    def test_korean_amounts_normalise_deterministically(self):
        for text, want in [("1억 2천만원", 120_000_000), ("5천만", 50_000_000), ("50,000,000원", 50_000_000),
                           ("3.5억", 350_000_000), ("1억 5천", 150_000_000), ("천만원", 10_000_000),
                           ("5백만원", 5_000_000), ("2억 원", 200_000_000), ("100000000", 100_000_000),
                           ("1억 5000원", 100_005_000), ("1억 5천원", 100_005_000), ("1억 5000", 150_000_000),
                           ("0억원", 0), ("0억", 0), ("0조 5억", 500_000_000), ("억", 100_000_000),
                           ("천억", 100_000_000_000)]:
            self.assertEqual(mq.parse_krw(text), want, text)
        for text in ("일억", "많이", "1억원쯤", "", "-5천만"):
            self.assertIsNone(mq.parse_krw(text), text)

    def test_the_schema_refuses_what_it_cannot_run(self):
        refused = {
            "not_metadata": {**model(), "metadata_question": False},
            "unknown_field": model([("summary", "=", ["x"])]),
            "operator_not_allowed:amount_krw:contains": model([("amount_krw", "contains", ["1억"])]),
            "operator_not_allowed:institution_type:>": model([("institution_type", ">", ["대학"])]),
            "value_count:amount_krw:between": model([("amount_krw", "between", ["1억"])]),
            "value_invalid:institution_type": model([("institution_type", "=", ["대학교"])]),
            "value_invalid:amount_krw": model([("amount_krw", ">=", ["일억"])]),
            "value_invalid:bid_close": model([("bid_close", ">=", ["6월"])]),
            "between_reversed": model([("amount_krw", "between", ["2억", "1억"])]),
            "limit_invalid": model(sort=("amount_krw", "asc"), limit=0),
            "sort_invalid": model(sort=("institution", "asc")),
            "schema_invalid": {"metadata_question": True},
        }
        for reason, body in refused.items():
            with self.assertRaises(mq.QueryInvalid, msg=reason) as caught:
                mq.validate_query(json.dumps(body, ensure_ascii=False))
            self.assertTrue(str(caught.exception).startswith(reason), (reason, str(caught.exception)))

    def test_every_pinned_model_output_validates_to_its_pinned_structure(self):
        self.assertGreaterEqual(len(PINNED), 15)
        for question, (body, structure, _) in PINNED.items():
            self.assertEqual(mq.validate_query(json.dumps(body, ensure_ascii=False)), structure, question)

    def test_no_question_wording_reaches_the_code(self):
        """Matching is the structure's job: the module holds no question phrase it routes on."""
        source = Path(mq.__file__).read_text(encoding="utf-8")
        code = source.split("class QueryInvalid")[1]  # the prompts above may explain the fields in Korean
        for phrase in ("가장 싼", "대학에서", "발주한", "이상인", "몇 개"):
            self.assertTrue(phrase not in code, phrase)

    def test_a_description_cites_only_its_own_passages_and_an_empty_one_is_none(self):
        batch = [{"doc_id": "a", "passages": [{"evidence_id": "P1"}, {"evidence_id": "P2"}]},
                 {"doc_id": "b", "passages": [{"evidence_id": "P3"}]},
                 {"doc_id": "c", "passages": [{"evidence_id": "P4"}]}]
        items = [{"doc_id": "a", "description": "가를 구축하는 사업 [P2]", "evidence_ids": ["P2", "P1"]},
                 {"doc_id": "b", "description": "나를 운영하는 사업", "evidence_ids": ["P1"]},
                 {"doc_id": "c", "description": "", "evidence_ids": []}]
        self.assertEqual(mq.validate_descriptions(json.dumps({"items": items}), batch),
                         {"a": {"description": "가를 구축하는 사업", "evidence_ids": ["P1", "P2"]}})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = fixtures.make_env(Path(self.tmp.name))
        add_projects(self.env.settings)
        self.transport = generation.FakeTransport(structured={"metadata_query": pinned_translator,
                                                              "institution_types": classifier})
        self.exporter = InMemorySpanExporter()
        self.tracer = tracing.Tracing("http://127.0.0.1:9", "pk-lf-mq", "sk-lf-mq", span_exporter=self.exporter)
        self.res = service.Resources(self.env.settings, transport=self.transport, tracer=self.tracer)

    def tearDown(self):
        self.res.close()
        self.tmp.cleanup()

    def ask(self, question, previous=""):
        return service.answer(self.res, self.env.consultant, AnswerRequest(
            idempotency_key=str(uuid.uuid4()), generation_id="g", question=question, scope=[], mode="corpus",
            as_of=AS_OF, previous_request_id=previous))

    def kinds(self):
        return [c["kind"] for c in self.transport.query_calls]

    def stored(self, request_id):
        with store.open_db(self.env.settings.db_path) as conn:
            row = conn.execute("SELECT trace_json FROM requests WHERE request_id = ?", (request_id,)).fetchone()
            attempts = [(r["stage"], r["state"]) for r in conn.execute(
                "SELECT stage, state FROM attempts WHERE request_id = ? ORDER BY created_at", (request_id,))]
        return json.loads(row["trace_json"]), attempts


class PinnedQuestionsTest(Base):
    def test_every_pinned_question_lists_exactly_the_projects_its_metadata_satisfies(self):
        table = stored_table(self.env.settings)
        for question, (_, structure, oracle) in PINNED.items():
            with self.subTest(question=question):
                r = self.ask(question)
                self.assertEqual(r.status, "answered", r.error)
                self.assertEqual(r.listing["query"], structure)
                want = oracle(table)
                self.assertTrue(want, "a pinned question with no match proves nothing")
                self.assertEqual({row["doc_id"] for row in r.listing["rows"]}, want)
                self.assertEqual(r.listing["count"], len(want))
                self.assertFalse({row["doc_id"] for row in r.listing["review"]} & want)
                self.assertEqual(self.transport.calls, [], "a metadata question never asks for a passage answer")

    def test_the_cheapest_project_lists_every_tie_and_placeholders_never_win(self):
        r = self.ask("가장 싼 사업")
        self.assertEqual(sorted(row["title"] for row in r.listing["rows"]), ["디지털 교과서 플랫폼", "전력 데이터 분석"])
        reasons = {row["title"]: [x["label"] for x in row["reasons"]] for row in r.listing["review"]}
        self.assertEqual(reasons["캠퍼스 안전 시스템"], ["0원 검토"])
        self.assertEqual(reasons["CCTV 통합관제"], ["금액 검토"])
        self.assertEqual(reasons["주민 참여 플랫폼"], ["금액 없음"])
        self.assertEqual(reasons["재난안전 데이터 공유"], ["출처 충돌"])
        self.assertEqual(reasons["도서관 좌석 예약"], ["금액 없음"])
        self.assertIn("확인 필요", r.summary)

    def test_a_university_is_a_classified_type_not_a_word_in_a_name_or_title(self):
        r = self.ask("대학에서 낸 제안서의 가격 목록")
        titles = [row["title"] for row in r.listing["rows"]]
        self.assertEqual(titles, ["학사 정보시스템 고도화", "통합 행정 시스템", "도서관 이용 시스템 구축", "연구관리 시스템"])
        for trap in ("대학 스포츠 통합 플랫폼", "대학생 진로 지원 플랫폼", "의료 영상 판독 시스템"):
            self.assertNotIn(trap, titles + [row["title"] for row in r.listing["review"]])
        self.assertEqual([row["title"] for row in r.listing["review"]], ["캠퍼스 안전 시스템"])
        self.assertEqual({row["institution_type"] for row in r.listing["rows"]}, {"대학"})

    def test_a_closing_date_filter_sends_undecided_dates_to_review(self):
        r = self.ask("2024년 6월 중에 입찰 마감된 사업")
        reasons = {row["title"]: [x["label"] for x in row["reasons"]] for row in r.listing["review"]}
        self.assertEqual(reasons, {"통합 정보시스템 구축": ["출처 충돌"], "도서관 좌석 예약": ["날짜 없음"],
                                   "주민 참여 플랫폼": ["날짜 없음"]})


class ClassificationTest(Base):
    def test_institutions_are_classified_once_with_provenance_and_a_correction_needs_no_rerun(self):
        first = self.ask("대학에서 낸 제안서의 가격 목록")
        self.assertEqual(self.kinds().count("institution_types"), 1)
        with store.open_db(self.env.settings.db_path) as conn:
            rows = {r["institution"]: dict(r) for r in conn.execute("SELECT * FROM institution_types")}
        self.assertEqual(rows["대학스포츠협의회"]["type"], "협회·단체")
        self.assertEqual({(r["model"], r["run_id"], r["prompt_version"]) for r in rows.values()},
                         {("gpt-5-mini", first.request_id, mq.CLASSIFY_PROMPT_VERSION)})
        self.assertTrue(all(r["classified_at"] for r in rows.values()))
        sports = next(p["doc_id"] for p in stored_table(self.env.settings) if p["title"] == "대학 스포츠 통합 플랫폼")
        ingestion.resolve_metadata(self.env.settings, sports, "institution_type", "대학", "fixture correction",
                                   "기관 홈페이지", "reviewer")
        with self.assertRaises(ingestion.IngestionError):
            ingestion.resolve_metadata(self.env.settings, sports, "institution_type", "대학교", "x", "y", "z")
        second = self.ask("대학에서 낸 제안서의 가격 목록")
        self.assertEqual(self.kinds().count("institution_types"), 1, "the stored types were reused")
        corrected = next(row for row in second.listing["rows"] if row["doc_id"] == sports)
        self.assertEqual((corrected["institution_type"], corrected["type_source"]), ("대학", "resolution"))
        self.assertEqual({row["doc_id"] for row in second.listing["rows"]},
                         PINNED["대학에서 낸 제안서의 가격 목록"][2](stored_table(self.env.settings)))
        trace, attempts = self.stored(first.request_id)
        self.assertEqual(attempts[:2], [("metadata_query", "settled"), ("institution_type", "settled")])
        self.assertTrue(all(state == "settled" for _, state in attempts))
        self.assertNotIn("generation", [s for s, _ in attempts])


class DescriptionTest(Base):
    QUESTION = "모든 사업 목록"

    def test_each_listed_project_gets_one_cited_line_from_its_overview_in_batches(self):
        with mock.patch.object(ma, "DESCRIBE_BATCH", 1):
            r = self.ask("5천만원 이상인 사업")
        described = [row for row in r.listing["rows"] if row["description"]]
        self.assertEqual({row["institution"] for row in described}, {"기관A", "기관C"})  # the indexed originals
        batches = [c for c in self.transport.query_calls if c["kind"] == "project_descriptions"]
        self.assertEqual(len(batches), 2)
        self.assertEqual({c["max_completion_tokens"] for c in batches}, {ma.DESCRIBE_MAX_OUTPUT_TOKENS})
        asked = [p for c in batches for p in json.loads(c["messages"][-1]["content"])["projects"]]
        ids = [x["evidence_id"] for p in asked for x in p["passages"]]
        self.assertTrue(all(1 <= len(p["passages"]) <= ma.OVERVIEW_PASSAGES for p in asked))
        self.assertEqual(len(ids), len(set(ids)))  # numbered across the whole list, never reused between batches
        for row in described:
            view = service.open_evidence(self.res, self.env.consultant, r.request_id, row["evidence_ids"][0])
            self.assertEqual(view.doc_id, row["doc_id"])
            self.assertIn(row["description"][:20], " ".join(view.quote.split()))
        undescribed = {row["note"] for row in r.listing["rows"] if not row["description"]}
        self.assertEqual(undescribed, {"원문 색인 없음"})

    def test_a_batch_that_fails_validation_leaves_its_projects_undescribed_and_the_answer_stands(self):
        calls = []

        def second_batch_cites_wrongly(messages):
            calls.append(1)
            response = generation._first_words_of_passage(messages)
            if len(calls) == 2:
                body = json.loads(response.content)
                for item in body["items"]:
                    item["evidence_ids"] = ["P99"]
                response.content = json.dumps(body, ensure_ascii=False)
            return response

        self.transport.structured["project_descriptions"] = second_batch_cites_wrongly
        with mock.patch.object(ma, "DESCRIBE_BATCH", 1):
            r = self.ask(self.QUESTION)
        self.assertEqual(r.status, "answered", r.error)
        self.assertEqual(len(calls), 3)  # 기관A, 기관C (shared original) and 기관D; 기관E has no passage
        notes = {row["institution"]: row["note"] for row in r.listing["rows"]}
        self.assertEqual({k: notes[k] for k in ("기관A", "기관C", "기관D", "기관E")},
                         {"기관A": None, "기관C": "설명 없음", "기관D": None, "기관E": "원문 색인 없음"})
        self.assertEqual(r.limitations, ["metadata_query:descriptions_incomplete_batch_2"])

    def test_a_list_the_allowance_cannot_describe_whole_is_listed_without_descriptions(self):
        with mock.patch.object(budget, "headroom", return_value=0):
            r = self.ask(self.QUESTION)
        self.assertEqual(r.status, "answered", r.error)
        self.assertNotIn("project_descriptions", self.kinds())
        self.assertIn("metadata_query:descriptions_over_allowance", r.limitations)
        self.assertEqual(len(r.listing["rows"]), len(stored_table(self.env.settings)))
        self.assertFalse(any(row["description"] for row in r.listing["rows"]))

    def test_a_count_only_question_lists_without_descriptions(self):
        r = self.ask("1억 넘는 사업이 몇 개나 돼?")
        self.assertTrue(r.summary.startswith(f"조건에 맞는 사업은 {r.listing['count']}개입니다."))
        self.assertNotIn("project_descriptions", self.kinds())


class FallbackAndTraceTest(Base):
    def test_an_invalid_or_non_metadata_query_answers_from_passages_and_records_why(self):
        for body, reason in ((model([("amount_krw", "contains", ["1억"])]), "operator_not_allowed:amount_krw:contains"),
                             ({**model(), "metadata_question": False}, "not_metadata")):
            self.transport.structured["metadata_query"] = lambda messages, body=body: reply(body, messages)
            self.transport.calls.clear()
            r = self.ask("하자보수 기간은 얼마인가요?")
            self.assertEqual(r.status, "answered", r.error)
            self.assertIsNone(r.listing)
            self.assertEqual([c["kind"] for c in self.transport.calls], ["answer"])
            trace, attempts = self.stored(r.request_id)
            self.assertEqual(trace["metadata_query"]["fallback_reason"], reason)
            self.assertEqual([s for s, _ in attempts], ["metadata_query", "generation"])

    def test_one_question_traces_translation_query_and_each_description_batch(self):
        with mock.patch.object(ma, "DESCRIBE_BATCH", 1):
            r = self.ask("5천만원 이상인 사업")
        self.res.close()  # flushes the spans
        spans = {s.name: s for s in self.exporter.get_finished_spans()}
        for name in ("answer-question", "metadata-query-translation", "translate-query", "classify-institutions",
                     "classify-call", "metadata-query", "describe-batch-1", "describe-batch-2", "describe-projects"):
            self.assertIn(name, spans)
        translation = json.loads(spans["metadata-query-translation"].attributes["langfuse.observation.output"])
        self.assertEqual(translation["query"], PINNED["5천만원 이상인 사업"][1])
        self.assertEqual(translation["raw_output"], PINNED["5천만원 이상인 사업"][0])
        ran = json.loads(spans["metadata-query"].attributes["langfuse.observation.output"])
        self.assertEqual(ran["matched_count"], r.listing["count"])
        self.assertEqual(ran["review_count"], len(r.listing["review"]))
        self.assertEqual(ran["review_reasons"], {"0원 검토": 2, "금액 검토": 1, "금액 없음": 2, "출처 충돌": 1})
        root = spans["answer-question"]
        self.assertEqual(spans["describe-projects"].parent.span_id, spans["describe-batch-2"].context.span_id)
        self.assertEqual(spans["metadata-query"].parent.span_id, root.context.span_id)

    def test_a_fallback_is_visible_in_the_translation_span(self):
        self.transport.structured["metadata_query"] = lambda m: reply({**model(), "metadata_question": False}, m)
        self.ask("하자보수 기간은 얼마인가요?")
        self.res.close()
        spans = {s.name: s for s in self.exporter.get_finished_spans()}
        out = json.loads(spans["metadata-query-translation"].attributes["langfuse.observation.output"])
        self.assertEqual(out["fallback_reason"], "not_metadata")
        self.assertIn("retrieve-evidence", spans)

    def test_a_follow_up_sees_the_listed_projects(self):
        first = self.ask("가장 싼 사업")
        with store.open_db(self.env.settings.db_path) as conn:  # as the conversation reads the previous answer
            row = conn.execute("SELECT result_json FROM requests WHERE request_id = ?", (first.request_id,)).fetchone()
            text = service._answer_text(service._stored_result(conn, first.request_id, row["result_json"]))
        self.assertIn("조건: 사업 금액 낮은 순 1개(동률 포함)", text)
        self.assertIn("디지털 교과서 플랫폼 · 한국교육학술정보원 · 공공기관 · 30,000,000원", text)
        self.assertIn("확인 필요: CCTV 통합관제 · 1원 (금액 검토)", text)


class RollbackTest(Base):
    def test_stored_answers_keep_the_envelope_an_older_version_rebuilds(self):
        """A rolled-back version (same database, schema 6) rebuilds result_json as AnswerResult(**) without
        `listing`: every stored answer leaves it out, and this version joins it back from request_listings."""
        listed = self.ask("가장 싼 사업")
        self.transport.structured["metadata_query"] = lambda m: reply({**model(), "metadata_question": False}, m)
        passage = self.ask("하자보수 기간은 얼마인가요?")
        older = {f.name for f in fields(AnswerResult)} - {"listing"}
        with store.open_db(self.env.settings.db_path) as conn:
            for r in (listed, passage):
                row = conn.execute("SELECT result_json FROM requests WHERE request_id = ?", (r.request_id,)).fetchone()
                self.assertEqual(set(json.loads(row["result_json"])), older)
        consultant = self.env.consultant
        self.assertEqual(service.request_status(self.res, consultant, listed.request_id).result.listing, listed.listing)
        self.assertIsNone(service.request_status(self.res, consultant, passage.request_id).result.listing)
        history = {v.request_id: v.result.listing for v in service.list_requests(self.res, consultant)}
        self.assertEqual(history[listed.request_id], listed.listing)
        self.assertEqual(service.export_request(self.res, consultant, listed.request_id)["result"]["listing"]["count"],
                         listed.listing["count"])
        with store.open_db(self.env.settings.db_path) as conn:
            key = conn.execute("SELECT idempotency_key FROM requests WHERE request_id = ?",
                               (listed.request_id,)).fetchone()[0]
        replay = service.answer(self.res, consultant, AnswerRequest(
            idempotency_key=key, generation_id="g", question="가장 싼 사업", scope=[], mode="corpus", as_of=AS_OF))
        self.assertEqual((replay.request_id, replay.listing), (listed.request_id, listed.listing))

    def traffic(self, question: str):
        """`question` asked as a 질문하기 request (idempotency key = generation id), then a traffic run over its
        window: (the answer, its frozen record, its answer in the run's view). Every value the consultant's table
        showed must come through: titles, descriptions, types and their source, amounts, the queried date columns,
        and the 확인 필요 reasons."""
        key = str(uuid.uuid4())
        asked = service.answer(self.res, self.env.consultant, AnswerRequest(
            idempotency_key=key, generation_id=key, question=question, scope=[], mode="corpus", as_of=AS_OF))
        self.assertTrue(asked.listing["rows"] and asked.listing["review"])
        run = traffic_review.run(self.env.settings, "2000-01-01", 5, "UNUSED",
                                 dsn=os.environ[self.env.settings.database_dsn_env])
        folder = Path(run["folder"])
        frozen = [json.loads(line) for line in gzip.decompress(
            (folder / "inputs" / "requests.jsonl.gz").read_bytes()).decode("utf-8").splitlines()]
        (record,) = [r for r in frozen if r["request_id"] == asked.request_id]
        view = json.loads((folder / "view.json").read_text(encoding="utf-8"))
        (q,) = [q for q in view["questions"] if q["id"] == asked.request_id]
        shown = q["candidates"][traffic_review.CANDIDATE]
        columns = lambda rows: [  # noqa: E731 - member-free fields, as the consultant's table showed them
            (r["title"], r.get("description"), r["institution_type"], r["type_source"], r["amount_krw"],
             r["published_at"], r["bid_start"], r["bid_close"]) for r in rows]
        for part in ("rows", "review"):
            self.assertEqual(columns(record["result"]["listing"][part]), columns(asked.listing[part]))
            self.assertEqual(columns(shown["listing"][part]), columns(asked.listing[part]))
        self.assertEqual(shown["listing"]["conditions"], asked.listing["conditions"])
        self.assertEqual(shown["listing"]["fields"],
                         [f for f in asked.listing["fields"] if f in traffic_review.LISTING_DATES])
        self.assertEqual([r["reasons"] for r in shown["listing"]["review"]],
                         [[x["label"] for x in r["reasons"]] for r in asked.listing["review"]])
        return asked, record, shown

    def test_a_traffic_run_shows_the_queried_dates_and_a_corrected_type(self):
        first = self.ask("2024년 6월 중에 입찰 마감된 사업")
        corrected = first.listing["rows"][0]["doc_id"]
        ingestion.resolve_metadata(self.env.settings, corrected, "institution_type", "연구기관", "fixture correction",
                                   "기관 홈페이지", "reviewer")
        asked, _, shown = self.traffic("2024년 6월 중에 입찰 마감된 사업")
        self.assertEqual(shown["listing"]["fields"], ["bid_close"])
        self.assertTrue(all(r["bid_close"] for r in shown["listing"]["rows"]), "every match has its closing date")
        self.assertIn(None, [r["bid_close"] for r in shown["listing"]["review"]], "날짜 없음 stays visible as none")
        (row,) = [r for r, a in zip(shown["listing"]["rows"], asked.listing["rows"]) if a["doc_id"] == corrected]
        self.assertEqual((row["institution_type"], row["type_source"]), ("연구기관", "resolution"))

    def test_a_traffic_run_freezes_and_shows_the_listing_the_consultant_saw(self):
        _, _, shown = self.traffic("5천만원 이상인 사업")
        self.assertEqual(shown["listing"]["fields"], [])  # an amount question shows no date column
        cited = {i for r in shown["listing"]["rows"] for i in r["evidence_ids"]}
        self.assertTrue(cited and cited <= set(shown["evidence"]), "each description opens its passage")
        described = next(i for i, r in enumerate(shown["listing"]["rows"]) if r["description"])
        self.assertEqual(error_analysis.field_text(shown, f"listing.{described}"),  # takes span notes like a claim
                         shown["listing"]["rows"][described]["description"])
        self.assertIn(f"listing.{described}", similar_cases.answer_fields(shown))


def metadata_units(messages, doc_id=None) -> list[dict]:
    """The metadata units an answer prompt supplies (of one document)."""
    evidence = json.loads(messages[-1]["content"])["evidence"]
    return [e for e in evidence if e["location"].get("format") == "metadata" and doc_id in (None, e["doc_id"])]


def cite_metadata(doc_id: str, field: str, claim_doc: str | None = None, absences=(), before=None):
    """A fake answer model restating `doc_id`'s `field` from its metadata unit (as a claim of `claim_doc`, by default
    the same document), plus `absences` declared for that document. `before(unit)` runs as the call is made."""
    def respond(messages):
        (unit,) = [u for u in metadata_units(messages, doc_id) if u["location"]["field"] == field]
        if before:
            before(unit)
        owner = claim_doc or doc_id
        return reply({"status": "answered", "summary": unit["text"], "summary_evidence_ids": [unit["evidence_id"]],
                      "claims": [{"text": unit["text"], "kind": "source_fact", "doc_id": owner,
                                  "evidence_ids": [unit["evidence_id"]]}]
                      + [{"text": t, "kind": "absence", "doc_id": doc_id, "evidence_ids": []} for t in absences],
                      "missing_fields": [], "conflicts": [], "next_action": None}, messages)
    return respond


class MetadataStatementTest(unittest.TestCase):
    def doc(self, conflicts=None, resolutions=None, **meta):
        base = {"title": "t", "institution": "기관X", "amount_krw": 130_000_000,
                "published_at": {"value": "2024-08-08T00:00:00+09:00", "precision": "timestamp"},
                "bid_start": {"value": "2024-08-20", "precision": "date"},
                "bid_close": {"value": "2024-09-09T15:00:00+09:00", "precision": "timestamp"}, **meta}
        resolutions = resolutions or {}
        return {"doc_id": "d", "active_source_hash": "h", "resolutions": resolutions,
                "effective": {**base, **{f: r["value"] for f, r in resolutions.items()}},
                "quality": {"provenance_conflicts": [{"field": f, "values": v} for f, v in (conflicts or {}).items()]}}

    def statements(self, d, types=None):
        return {s["field"]: (s["text"], s["source"]) for s in mq.metadata_statements(
            d, {"기관X": {"type": "공공기관"}} if types is None else types)}

    def test_known_values_are_stated_with_their_source(self):
        self.assertEqual(self.statements(self.doc()), {
            "amount_krw": ("사업 금액: 130,000,000원", "csv"), "institution": ("발주 기관: 기관X", "csv"),
            "institution_type": ("기관 유형: 공공기관", "classification"),
            "published_at": ("공개 일자: 2024-08-08", "csv"), "bid_start": ("입찰 시작일: 2024-08-20", "csv"),
            "bid_close": ("입찰 마감일: 2024-09-09 15:00", "csv")})
        corrected = self.doc(conflicts={"amount_krw": {"x": 1, "y": 2}}, resolutions={
            "amount_krw": {"value": 90_000_000}, "institution_type": {"value": "대학"}})
        got = self.statements(corrected)
        self.assertEqual(got["amount_krw"], ("사업 금액: 90,000,000원", "resolution"))
        self.assertEqual(got["institution_type"], ("기관 유형: 대학", "resolution"))

    def test_a_value_no_query_may_use_is_stated_as_needing_review_never_as_a_plain_value(self):
        cases = {
            "사업 금액: 확인 필요 (0원 검토, 기록된 값 0원)": self.doc(amount_krw=0),
            "사업 금액: 확인 필요 (금액 검토, 기록된 값 1원)": self.doc(amount_krw=1),
            "사업 금액: 확인 필요 (금액 없음)": self.doc(amount_krw=None),
            "사업 금액: 확인 필요 (출처 충돌, 기록된 값 100,000,000원 / 200,000,000원)":
                self.doc(conflicts={"amount_krw": {"x": 100_000_000, "y": 200_000_000}}),
        }
        for text, d in cases.items():
            self.assertEqual(self.statements(d)["amount_krw"][0], text)
        got = self.statements(self.doc(bid_start=None), types={})
        self.assertEqual(got["bid_start"][0], "입찰 시작일: 확인 필요 (날짜 없음)")
        self.assertEqual(got["institution_type"], ("기관 유형: 확인 필요 (유형 미분류)", "classification"))


class MetadataCitationTest(Base):
    """Every answer prompt states each of its documents' typed metadata as citable units, validated like passages."""

    def setUp(self):
        super().setUp()
        self.transport.structured["metadata_query"] = lambda m: reply({**model(), "metadata_question": False}, m)
        self.a, self.d = self.env.refs["기관A"], self.env.refs["기관D"]

    def scoped(self, scope, mode="single", previous=""):
        return service.answer(self.res, self.env.consultant, AnswerRequest(
            idempotency_key=str(uuid.uuid4()), generation_id="g", question="하자보수 기간은 얼마인가요?", scope=scope,
            mode=mode, as_of=AS_OF, previous_request_id=previous))

    def test_every_document_of_every_prompt_gets_metadata_units_apart_from_the_passages(self):
        single = self.scoped([self.a])
        asked = {"single": single, "compare": self.scoped([self.a, self.d], "compare"), "corpus": self.ask("하자보수 기간"),
                 "follow-up": self.scoped([self.a], previous=single.request_id)}
        prompts = [c["messages"] for c in self.transport.calls if c["kind"] == "answer"]
        self.assertEqual(len(prompts), 4)
        for (mode, r), messages in zip(asked.items(), prompts):
            with self.subTest(mode=mode):
                self.assertEqual(r.status, "answered", r.error)
                request = json.loads(messages[-1]["content"])
                units = metadata_units(messages)
                self.assertEqual(sorted((u["doc_id"], u["location"]["field"]) for u in units),
                                 sorted((d["doc_id"], f) for d in request["selected_documents"]
                                        for f in mq.EVIDENCE_FIELDS))
                passages = [e["evidence_id"] for e in request["evidence"] if e not in units]
                self.assertTrue(passages and all(i.startswith("E") for i in passages))
                self.assertEqual([u["evidence_id"] for u in units], [f"M{i}" for i in range(1, len(units) + 1)])
                for d in request["selected_documents"]:  # each document's IDs list its metadata units too
                    self.assertTrue({u["evidence_id"] for u in units if u["doc_id"] == d["doc_id"]}
                                    <= set(request["evidence_ids_by_doc"][d["doc_id"]]))

    def test_a_metadata_citation_is_served_and_opens_as_metadata(self):
        self.transport.responder = cite_metadata(self.a.doc_id, "amount_krw")
        r = self.scoped([self.a])
        self.assertEqual(r.status, "answered", r.error)
        (eid,) = r.claims[0]["evidence_ids"]
        self.assertEqual(r.claims[0]["text"], "사업 금액: 130,000,000원")
        self.assertEqual(r.evidence[eid]["location"], {"format": "metadata", "field": "amount_krw",
                                                       "value": "130,000,000원", "source": "csv"})
        view = service.open_evidence(self.res, self.env.consultant, r.request_id, eid)
        self.assertEqual((view.quote, view.context, view.download_available, view.location["format"]),
                         ("사업 금액: 130,000,000원", [], False, "metadata"))

    def test_a_metadata_unit_of_another_document_is_a_scope_mismatch(self):
        self.transport.responder = cite_metadata(self.d.doc_id, "amount_krw", claim_doc=self.a.doc_id)
        r = self.scoped([self.a, self.d], "compare")
        self.assertEqual(r.status, "technical_error")
        self.assertRegex(r.error, r"^evidence_scope_mismatch: M\d+ belongs to another document$")

    def test_a_metadata_unit_is_checked_against_the_metadata_stored_when_the_answer_is_validated(self):
        def corrected(unit):  # a reviewer corrects the type while the answer is being written
            ingestion.resolve_metadata(self.env.settings, self.a.doc_id, "institution_type", "연구기관", "fixture",
                                       "기관 홈페이지", "reviewer")
        self.transport.responder = cite_metadata(self.a.doc_id, "institution_type", before=corrected)
        r = self.scoped([self.a])
        self.assertEqual(r.status, "technical_error")
        self.assertRegex(r.error, r"^evidence_quote_mismatch: M\d+$")
        self.transport.responder = cite_metadata(self.a.doc_id, "institution_type")
        again = self.scoped([self.a])
        self.assertEqual((again.status, again.claims[0]["text"]), ("answered", "기관 유형: 연구기관"))


# Every metadata question type of the pinned set, by one pinned question of that type.
QUESTION_TYPES = {"highest": "예산 규모가 제일 큰 사업 세 개만 뽑아줘", "lowest": "가장 싼 사업",
                  "threshold above": "5천만원 이상인 사업", "threshold below": "오천만원도 안 되는 저예산 사업들",
                  "institution type": "지자체 발주 사업 보여줘", "combined": "대학에서 낸 제안서 중 1억 이상인 제안서"}
ABSENCES = ("Contract period: not in the supplied passages", "Nothing supplied gives  the submission deadline")


class ListFollowUpTest(unittest.TestCase):
    """Generated two-turn conversations: a listing, then a follow-up about the project it listed first. Every project
    has its own indexed original, so the follow-up's prompt holds the project's passages and its metadata units."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        rows, originals = [], {}
        for i, (title, inst, amt, published, close, _) in enumerate(PROJECTS):
            name = f"P{i:02d}_{title}.pdf"
            originals[name] = fixtures.make_pdf([["제안요청서", "Ⅰ. 사업 개요", f"사업명: {title}",
                                                  f"{title} 시스템을 구축하고 운영한다."]])
            rows.append([f"2024{i:04d}", "0.0", title, "" if amt is None else f"{amt}.0", inst, published or "", "",
                         close or "", "요약", "pdf", name, "미리보기"])
        self.env = fixtures.make_env(Path(self.tmp.name), rows=rows, originals=originals)
        def translator(messages):  # a pinned question is a metadata question; the follow-up is not
            asked = json.loads(messages[-1]["content"])["question"]
            return reply(PINNED[asked][0] if asked in PINNED else {**model(), "metadata_question": False}, messages)

        self.transport = generation.FakeTransport(structured={"metadata_query": translator,
                                                              "institution_types": classifier})
        self.res = service.Resources(self.env.settings, transport=self.transport)

    def tearDown(self):
        self.res.close()
        self.tmp.cleanup()

    def ask(self, question, previous=""):
        return service.answer(self.res, self.env.consultant, AnswerRequest(
            idempotency_key=str(uuid.uuid4()), generation_id="g", question=question, scope=[], mode="corpus",
            as_of=AS_OF, previous_request_id=previous))

    def test_a_follow_up_about_a_listed_project_restates_its_amount_from_metadata(self):
        self.assertTrue(set(QUESTION_TYPES.values()) <= set(PINNED))
        for kind, question in QUESTION_TYPES.items():
            with self.subTest(kind=kind):
                listed = self.ask(question)
                self.assertEqual(listed.status, "answered", listed.error)
                project = listed.listing["rows"][0]  # the project comes from the listing, never fixed in the test
                self.assertTrue(project["evidence_ids"], "its description passages carry it into the follow-up")
                self.transport.responder = cite_metadata(project["doc_id"], "amount_krw", absences=ABSENCES)
                r = self.ask("첫 번째로 나온 사업을 처음 보는 사람도 알도록 풀어서 설명해 줘", listed.request_id)
                self.assertEqual(r.status, "answered", r.error)
                (claim,) = r.claims  # the absences are never shown
                (eid,) = claim["evidence_ids"]
                unit = r.evidence[eid]
                self.assertEqual((claim["doc_id"], unit["doc_id"], unit["location"]["format"],
                                  unit["location"]["field"]), (project["doc_id"],) * 2 + ("metadata", "amount_krw"))
                amount = project["amount_krw"]
                self.assertEqual(claim["text"], f"사업 금액: {amount:,}원" if amount and amount >= mq.MIN_AMOUNT_KRW
                                 else unit["quote"])
                self.assertEqual([m for m in r.missing_fields if m["doc_id"] == project["doc_id"]],
                                 [{"doc_id": project["doc_id"], "field": " ".join(t.split()),
                                   "reason": "not_found_in_context"} for t in ABSENCES])


if __name__ == "__main__":
    unittest.main()
