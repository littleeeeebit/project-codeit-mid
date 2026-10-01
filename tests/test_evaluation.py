"""Phase 4: gold-2 validation and freezing, grouped evidence metrics, answer runs with safe resume, blind review,
the release freeze and the single sealed run. Fake provider and temporary corpus only."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rfp_assistant import answers, auth, budget, evaluation, generation, gold, sealed, service, store
from rfp_assistant.contracts import Principal
from rfp_assistant.generation import FakeTransport, ProviderError
from tests import fixtures
from tests import phase4_fixtures as p4


def chunk(element_id: str, start: int, end: int, x: str = "x") -> dict:
    return {"extraction_id": x, "spans": [{"element_id": element_id, "start": start, "end": end}]}


ELS = {("x", "amount"): {"raw_text": "사업 예산은 금 130,000,000원으로 한다.", "table": None},
       ("x", "vat"): {"raw_text": "위 금액은 부가가치세를 포함한 금액이다.", "table": None},
       ("x", "alt"): {"raw_text": "총 사업비: 130,000,000원", "table": None}}


def gold_row(groups: list[dict]) -> dict:
    return {"dataset_version": evaluation.GOLD_SCHEMA, "question_id": "q", "evidence_groups": groups}


def grp(gid: str, *alternatives: tuple[str, str]) -> dict:
    return {"group_id": gid, "doc_id": "d", "alternatives": [{"element_id": e, "quote": q, "extraction_id": "x"}
                                                            for e, q in alternatives]}


AMOUNT_G = grp("g1", ("amount", "130,000,000원"))
VAT_G = grp("g2", ("vat", "부가가치세를 포함한"))


class MetricFixtureTest(unittest.TestCase):
    """The plan's metric/check regression fixtures that need no corpus."""

    def test_two_groups_one_recovered(self):
        row = gold_row([AMOUNT_G, VAT_G])
        m = evaluation.score_row(row, [chunk("amount", 0, 30)], [chunk("amount", 0, 30)], ELS)
        self.assertEqual((m["recall@20"], m["complete@20"], m["units"]), (0.5, 0, 2))
        agg = evaluation.aggregate([{"id": "q", "type": "t", "metrics": m, "families": ["f"]}], [])
        self.assertEqual(agg["single_evidence"]["hit@20"]["denominator"], 0)  # not a single-hit success
        self.assertEqual(agg["multi_evidence"]["complete@20"]["numerator"], 0)

    def test_alternate_spans_are_one_fact(self):
        row = gold_row([grp("g1", ("amount", "130,000,000원"), ("alt", "130,000,000원"))])
        ranking = [chunk("amount", 0, 30), chunk("alt", 0, 20)]
        m = evaluation.score_row(row, ranking, ranking, ELS)
        self.assertEqual((m["units"], m["recall@20"], m["hit@20"]), (1, 1.0, 1))
        self.assertEqual(m["graded@5"], [2, 0])  # the second alternative earns nothing more

    def test_amount_without_vat_note_is_incomplete(self):
        row = gold_row([AMOUNT_G, VAT_G])
        m = evaluation.score_row(row, [chunk("amount", 0, 30)], [chunk("amount", 0, 30)], ELS)
        self.assertEqual((m["packed_complete"], m["packed_missing"]), (0, 1))
        claim = p4.claim("c1", ["g1", "g2"], {"type": "number", "value": 130000000, "unit": "KRW"}, "amount",
                         qualifiers=[["부가가치세 포함", "부가세 포함"]])
        answer = {"summary": "", "claims": [{"text": "사업 예산은 1억 3천만 원입니다.", "doc_id": "d"}]}
        self.assertEqual(answers.claim_verdict(claim, answer, "d"), "incomplete_qualifier")
        answer["claims"][0]["text"] += " 부가세 포함 금액입니다."
        self.assertEqual(answers.claim_verdict(claim, answer, "d"), "correct")
        answer["claims"][0]["text"] = "사업 예산은 120,000,000원입니다."
        self.assertEqual(answers.claim_verdict(claim, answer, "d"), "wrong_value")
        answer["claims"][0]["text"] = "예산은 문서에 나옵니다."
        self.assertEqual(answers.claim_verdict(claim, answer, "d"), "missing")

    def test_claims_keep_their_document_unit_and_time(self):
        """Review round 1: a value stated for the other document, in another unit, or at another cutoff time is
        never credited, and a correct value next to a contradicting one is contested, not correct."""
        months = p4.claim("c1", ["g1"], {"type": "number", "value": 12, "unit": "개월"})
        both = {"summary": "두 사업 비교", "claims": [{"doc_id": "A", "text": "하자보수는 6개월"},
                                                 {"doc_id": "B", "text": "하자보수는 12개월"}]}
        self.assertEqual(answers.claim_verdict(months, both, "A", single_document=False), "wrong_value")
        self.assertEqual(answers.claim_verdict(months, both, "B", single_document=False), "correct")
        self.assertEqual(answers.claim_verdict(months, {"claims": [{"doc_id": "A", "text": "하자보수는 12일"}]}, "A"),
                         "missing")
        self.assertEqual(answers.claim_verdict(months, {"claims": [{"doc_id": "A", "text": "12개월 또는 6개월"}]},
                                               "A"), "contested")
        # the unattributed summary never credits one side of a comparison
        self.assertEqual(answers.claim_verdict(months, {"summary": "12개월", "claims": [
            {"doc_id": "B", "text": "12개월"}]}, "A", single_document=False), "missing")
        deadline = p4.claim("c1", ["g1"], {"type": "date", "value": "2024-06-11", "time": "17:00"}, "deadline")
        answer = lambda text: {"claims": [{"doc_id": "A", "text": text}]}  # noqa: E731
        self.assertEqual(answers.claim_verdict(deadline, answer("제출 마감은 2024. 6. 11. 18:00까지"), "A"), "wrong_value")
        self.assertEqual(answers.claim_verdict(deadline, answer("2024. 6. 11.(화) 17:00까지"), "A"), "correct")
        self.assertEqual(answers.claim_verdict(deadline, answer("2024. 6. 11. 17:00, 정정 2024. 6. 11. 18:00"), "A"),
                         "contested")
        self.assertEqual(answers.claim_verdict(deadline, answer("2024. 6. 11.까지"), "A"), "incomplete_qualifier")
        scored = [{"question_id": "q", "type": "direct_fact", "answerability": "answerable", "outcome": "answered",
                   "status_ok": True, "scope_leaks": 0, "settled_micro_usd": 0, "latency_ms": 1, "attempt_no": 1,
                   "links": [], "answer_claims": [],
                   "claims": [{"claim_id": "c1", "verdict": v, "critical_kind": "deadline"}]}
                  for v in ("wrong_value", "contested")]
        agg = answers.aggregate_answers(scored)
        self.assertEqual(len(agg["critical_wrong"]), 1)
        self.assertEqual([c["verdict"] for c in agg["critical_unresolved"]], ["contested"])
        self.assertEqual(agg["required_claim_correctness"]["numerator"], 0)

    def test_a_hallucinated_claim_sharing_a_correct_citation_is_not_supported(self):
        """Review round 1: a cited chunk holding a complete gold span is retrieval relevance; the generated claim
        itself must state that span's value, or it goes to blind review."""
        from types import SimpleNamespace

        claim = p4.claim("c1", ["g1"], {"type": "number", "value": 130000000, "unit": "KRW"}, "amount")
        row = {**gold_row([AMOUNT_G]), "question_type": "direct_fact", "answerability": "answerable",
               "expected_status": "answered", "mode": "single", "scope": [{"doc_id": "d"}],
               "required_claims": [claim]}
        index = SimpleNamespace(chunks=[{"chunk_id": "k1", **chunk("amount", 0, 30)}], elements=ELS)
        record = {"finalist": "F", "outcome": "answered", "link_validity": {"E1": True},
                  "evidence": {"E1": {"doc_id": "d", "chunk_id": "k1"}},
                  "answer": {"summary": "", "claims": [
                      {"doc_id": "d", "text": "사업 예산은 130,000,000원이다.", "evidence_ids": ["E1"]},
                      {"doc_id": "d", "text": "누구나 무조건 낙찰받는다.", "evidence_ids": ["E1"]},
                      {"doc_id": "d", "text": "사업 예산은 130,000,000원이며 누구나 낙찰받는다. 계약은 수의계약이다.",
                       "evidence_ids": ["E1"]}]}}
        out = answers.score_record(row, record, index, {})
        self.assertEqual([link["support"] for link in out["links"]], ["supporting", "unjudged", "unjudged"])
        self.assertEqual([a["supported"] for a in out["answer_claims"]], [True, None, None])
        self.assertEqual(out["claims"][0]["verdict"], "correct")
        agg = answers.aggregate_answers([out])
        self.assertEqual(agg["citation_precision_lower_bound"]["numerator"], 1)
        self.assertEqual(agg["answer_claims_unjudged"], 2)  # both go to the blind review sheet

    def test_overlapping_duplicates_count_once_and_rechunking_keeps_the_truth(self):
        row = gold_row([AMOUNT_G])
        a = evaluation.score_row(row, [chunk("amount", 0, 30), chunk("amount", 5, 30)], [], ELS)
        b = evaluation.score_row(row, [chunk("amount", 0, 30)], [], ELS)
        c = evaluation.score_row(row, [chunk("amount", 3, 40)], [], ELS)  # another chunking of the same span
        self.assertEqual(a["graded@5"], [2, 0])
        self.assertEqual((a["recall@20"], a["ndcg@5"]), (b["recall@20"], b["ndcg@5"]))
        self.assertEqual((b["recall@20"], b["ndcg@5"]), (c["recall@20"], c["ndcg@5"]))

    def test_ndcg_matches_the_hand_calculation_and_mrr(self):
        import math

        hand = (3 / math.log2(2) + 0 + 1 / math.log2(4)) / (3 / math.log2(2) + 1 / math.log2(3))
        self.assertAlmostEqual(evaluation.ndcg_from_grades([2, 0, 1], [2, 1, 0]), round(hand, 4))
        row = gold_row([AMOUNT_G, VAT_G])
        ranking = [chunk("amount", 0, 30), chunk("alt", 0, 5), chunk("vat", 0, 8)]  # grades 2, 0, 1
        m = evaluation.score_row(row, ranking, [], ELS)
        self.assertEqual(m["graded@5"], [2, 0, 1])
        self.assertEqual(m["mrr"], 1.0)  # first full support at rank 1
        self.assertIsNone(evaluation.ndcg_from_grades([0], [0]))

    def test_no_eligible_passage_is_not_applicable(self):
        agg = evaluation.aggregate([{"id": "m", "type": "metadata_direct", "families": ["f"]}], [])
        self.assertIsNone(agg["single_evidence"]["hit@20"]["rate"])
        self.assertIsNone(agg["ndcg@5"])
        self.assertEqual(agg["not_scored"]["types"], ["metadata_direct"])
        self.assertEqual(answers.aggregate_answers([])["required_claim_correctness"]["rate"], None)

    def test_korean_amounts_and_dates(self):
        self.assertEqual(evaluation.extract_numbers("1억 3천만 원, 130백만원, 1.3억"), {130000000})
        self.assertIn("2024-06-11T17:00", evaluation.extract_dates("2024. 6. 11.(화) 17:00까지"))
        self.assertIn("2024-06-11T17:00", evaluation.extract_dates("2024년 6월 11일 오후 5시"))

    def test_family_bootstrap_is_seeded_and_grouped(self):
        results = [{"id": i, "families": [f"f{i % 3}"], "metrics": {"ndcg@5": i % 2}} for i in range(12)]
        a = evaluation.family_bootstrap(results, "ndcg@5")
        self.assertEqual(a, evaluation.family_bootstrap(results, "ndcg@5"))
        self.assertEqual((a["families"], a["unit"], a["seed"]), (3, "source family", evaluation.BOOTSTRAP_SEED))
        self.assertIsNone(evaluation.family_bootstrap(results[:1], "ndcg@5"))


class Phase4Case(unittest.TestCase):
    splits = {"기관A": "dev", "기관F": "dev", "기관D": "test", "기관E": "test"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = p4.make_env(self.root, splits=self.splits)
        self.s = self.env.settings

    def tearDown(self):
        self.tmp.cleanup()

    def dev_rows(self) -> list[dict]:
        return [p4.amount_row(self.env), p4.deadline_row(self.env), p4.warranty_row(self.env),
                p4.absent_row(self.env)]


class ValidatorTest(Phase4Case):
    def test_reviewed_rows_validate_as_a_labeled_pilot(self):
        p4.write(self.env, "dev", self.dev_rows())
        report = evaluation.validate_gold(self.s, "dev")
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["label"], "pilot")  # 4 rows are far from the 60-row target
        self.assertEqual(report["source_positions"]["late"], 2)
        self.assertTrue((self.s.data_dir / "datasets" / "dev.validation.json").exists())

    def test_pending_self_approved_and_fabricated_rows_are_refused(self):
        rows = self.dev_rows()
        rows[0]["review"].update(reviewed_by=None, status="pending", approved_at=None)
        rows[1]["review"]["reviewed_by"] = "agent-a"  # the drafter
        rows[2]["evidence_groups"][0]["alternatives"][0]["quote"] = "원문에 없는 문장"
        rows[3]["generation_provenance"] = {"method": "llm", "model": "person-b", "prompt_version": "p",
                                            "source_set_hash": "h"}  # the drafting model approving itself
        p4.write(self.env, "dev", rows)
        errors = " ".join(evaluation.validate_gold(self.s, "dev")["errors"])
        for needle in ("pending or unreviewed", "needs an independent reviewer", "quote not found",
                       "cannot approve itself"):
            self.assertIn(needle, errors)

    def test_same_original_hash_in_dev_and_test_is_leakage(self):
        fam_path = self.s.data_dir / "datasets" / "families.json"
        fams = json.loads(fam_path.read_text(encoding="utf-8"))
        key = p4.family_of(self.env, "기관A")
        copy = self.env.refs["기관C"].doc_id  # byte-identical to 기관A
        fams["families"][key]["doc_ids"].remove(copy)
        fams["families"]["src-handmade"] = {"split": "test", "source_hash": fams["families"][key]["source_hash"],
                                           "doc_ids": [copy]}
        fam_path.write_text(json.dumps(fams, ensure_ascii=False), encoding="utf-8")
        p4.write(self.env, "dev", self.dev_rows())
        report = evaluation.validate_gold(self.s, "dev")
        self.assertFalse(report["ok"])
        self.assertTrue(any("split leakage" in e for e in report["errors"]))

    def test_a_scoped_source_failure_is_not_source_absence(self):
        nv = {"scope_searched": [self.env.refs["기관E"].doc_id], "methods": ["read"], "locations": ["전체"],
              "original_complete": True, "rationale": "없음"}
        p4.set_splits(self.env, {"기관E": "dev"})
        row = p4.row(self.env, "dev-e", "재난 관리 시스템의 하자보수 기간은?", "기관E", answerability="unanswerable",
                     expected_status="insufficient_evidence", qtype="missing_false_premise", negative_validation=nv)
        p4.write(self.env, "dev", [row])
        errors = evaluation.validate_gold(self.s, "dev")["errors"]
        self.assertTrue(any("operational suite" in e for e in errors), errors)
        p4.write(self.env, "dev", [{**row, "operational_case": True}])
        self.assertTrue(any("operational suite" in e for e in evaluation.validate_gold(self.s, "dev")["errors"]))

    def test_chunk_labels_negatives_disputes_and_offsets(self):
        rows = self.dev_rows()
        rows[0]["evidence_groups"][0]["alternatives"][0]["chunk_id"] = "c-123"
        rows[3]["negative_validation"] = None
        rows[1]["review"]["disputed"] = True
        rows[2]["evidence_groups"][0]["alternatives"][0]["offsets"] = [0, 3]
        p4.write(self.env, "dev", rows)
        errors = " ".join(evaluation.validate_gold(self.s, "dev")["errors"])
        for needle in ("chunk-only labels", "needs negative_validation", "independent second review",
                       "raw offsets do not hold the quote"):
            self.assertIn(needle, errors)
        rows = self.dev_rows()
        rows[1]["review"].update(disputed=True, second_review={"reviewer": "person-c", "agreed": True})
        rows[2]["evidence_groups"][0]["alternatives"][0]["offsets"] = [0, len(p4.WARRANTY)]
        p4.write(self.env, "dev", rows)
        self.assertTrue(evaluation.validate_gold(self.s, "dev")["ok"])

    def test_paraphrase_across_splits_and_quiet_test_errors(self):
        p4.set_splits(self.env, {"기관D": "test"})
        test_row = p4.row(self.env, "test-seat", "제안서는 언제까지 내야 하나요", "기관D", split="test",
                          groups=[p4.group(self.env, "g1", "기관D", ("%좌석%", p4.SEATS))],
                          claims=[p4.claim("c1", ["g1"], {"type": "text", "patterns": ["좌석 예약"]})])
        p4.write(self.env, "test", [test_row])
        self.assertTrue(evaluation.dataset_path(self.s, "test").is_relative_to(self.s.data_dir / "sealed"))
        p4.write(self.env, "dev", self.dev_rows())
        report = evaluation.validate_gold(self.s, "dev")
        self.assertTrue(any("paraphrases test a sealed row (ID withheld)" in e for e in report["errors"]),
                        report["errors"])
        self.assertNotIn("test-seat", json.dumps(report, ensure_ascii=False))
        test_row["evidence_groups"][0]["alternatives"][0]["quote"] = "비밀 인용"
        p4.write(self.env, "test", [test_row])
        report = evaluation.validate_gold(self.s, "test")
        text = json.dumps(report, ensure_ascii=False)
        self.assertNotIn(test_row["question"], text)  # a sealed report names IDs only
        self.assertNotIn("비밀 인용", text)

    def test_freeze_records_hashes_and_notices_changes(self):
        rows = self.dev_rows()
        p4.write(self.env, "dev", rows)
        with self.assertRaises(evaluation.EvaluationError):
            evaluation.freeze_dataset(self.s, "dev", "owner", "")
        manifest = evaluation.freeze_dataset(self.s, "dev", "owner", "development set for phase 4")
        self.assertEqual(manifest["rows"], 4)
        self.assertTrue(evaluation.frozen_dataset(self.s, "dev")["current"])
        p4.write(self.env, "dev", rows[:3])
        self.assertFalse(evaluation.frozen_dataset(self.s, "dev")["current"])
        rows[0]["review"]["reviewed_by"] = "agent-a"
        p4.write(self.env, "dev", rows)
        with self.assertRaises(evaluation.EvaluationError):
            evaluation.freeze_dataset(self.s, "dev", "owner", "again")


class GoldQueueTest(Phase4Case):
    def submit(self, batch: str, rows: list[dict], dataset: str) -> dict:
        for r in rows:
            r["review"].update(reviewed_by=None, approved_at=None, status="pending", original_inspected=False)
        path = self.root / f"{batch}.jsonl"
        store.write_jsonl_atomic(path, rows)
        return gold.submit(self.s, path, batch, dataset, "agent-a")

    def test_dev_review_needs_the_original_and_projects_valid_gold(self):
        self.submit("g1", self.dev_rows(), "dev")
        res = service.Resources(self.s)
        try:
            v = auth.visitor("person-b")
            queue = service.gold_queue(res, v)["pending"]
            self.assertEqual(len(queue), 4)
            c = service.gold_candidate(res, v, "dev-amount-r1")
            self.assertEqual(len(c["context"]["evidence"]), 2)
            with self.assertRaises(service.ServiceError):
                service.gold_decide(res, v, "dev-amount-r1", "approve", c["row_sha256"])
            for cid in ("dev-amount-r1", "dev-deadline-r1", "dev-warranty-r1", "dev-absent-r1"):
                sha = service.gold_candidate(res, v, cid)["row_sha256"]
                service.gold_decide(res, v, cid, "approve", sha, original_inspected=True,
                                    disputed=cid == "dev-deadline-r1")
            self.assertEqual([r["question_id"] for r in service.gold_awaiting_second_review(res, v)], ["dev-deadline"])
            report = evaluation.validate_gold(self.s, "dev")
            self.assertTrue(any("second review" in e for e in report["errors"]))
            with self.assertRaises(service.ServiceError):  # the first reviewer cannot be the second
                service.gold_second_review(res, v, "dev-deadline-r1", True, "확인")
            service.gold_second_review(res, auth.visitor("person-c"), "dev-deadline-r1", True, "원문 4쪽 확인")
        finally:
            res.close()
        self.assertTrue(evaluation.validate_gold(self.s, "dev")["ok"])
        log_path, _ = evaluation.export_review_log(self.s, "dev")
        kinds = [r["kind"] for r in store.read_jsonl(log_path)]
        self.assertEqual(kinds.count("second"), 1)
        self.assertEqual(gold.check(self.s), [])

    def test_sealed_candidates_stay_off_the_review_screen(self):
        row = p4.row(self.env, "test-seat", "도서관에서 무엇을 만드나요?", "기관D", split="test",
                     groups=[p4.group(self.env, "g1", "기관D", ("%좌석%", p4.SEATS))],
                     claims=[p4.claim("c1", ["g1"], {"type": "text", "patterns": ["좌석 예약"]})])
        out = self.submit("t1", [row], "test")
        self.assertIn("sealed", out["batch_file"])
        res = service.Resources(self.s)
        try:
            v = auth.visitor("person-b")
            self.assertEqual(service.gold_queue(res, v)["pending"], [])
            with self.assertRaises(service.ServiceError):
                service.gold_candidate(res, v, "test-seat-r1")
            with self.assertRaises(auth.AuthError):
                service.dataset_rows(res, v, "test")
        finally:
            res.close()
        c = gold.candidate(self.s, "test-seat-r1", include_sealed=True)
        gold.decide(self.s, "test-seat-r1", "approve", "person-b", c["row_sha256"], original_inspected=True,
                    include_sealed=True)
        self.assertEqual(len(store.read_jsonl(self.s.data_dir / "sealed" / "test.jsonl")), 1)
        self.assertFalse((self.s.data_dir / "datasets" / "test.jsonl").exists())
        self.assertTrue(evaluation.validate_gold(self.s, "test")["ok"])
        self.assertEqual(gold.check(self.s), [])

    def test_a_correction_appends_a_revision(self):
        rows = self.dev_rows()[:1]
        self.submit("g1", rows, "dev")
        c = gold.candidate(self.s, "dev-amount-r1")
        gold.decide(self.s, "dev-amount-r1", "approve", "person-b", c["row_sha256"], original_inspected=True)
        fixed = p4.amount_row(self.env, difficulty_reason="단서가 다음 쪽에 있음")
        with self.assertRaises(gold.GoldError):  # the same revision again
            self.submit("g2", [dict(fixed)], "dev")
        self.submit("g3", [{**fixed, "revision": 2}], "dev")
        c = gold.candidate(self.s, "dev-amount-r2")
        gold.decide(self.s, "dev-amount-r2", "approve", "person-b", c["row_sha256"], original_inspected=True)
        (row,) = store.read_jsonl(evaluation.dataset_path(self.s, "dev"))
        self.assertEqual((row["revision"], row["difficulty_reason"]), (2, "단서가 다음 쪽에 있음"))


class GoldRetrievalTest(Phase4Case):
    splits = {"기관A": "dev", "기관F": "dev", "기관D": "dev", "기관E": "test"}

    def compare_row(self) -> dict:
        env = self.env
        return p4.row(env, "dev-compare", "두 사업의 하자보수와 예산 조건을 비교해 주세요", keys=["기관F", "기관A"],
                      qtype="cross_document",
                      groups=[p4.group(env, "g1", "기관F", ("%130,000,000%", p4.AMOUNT)),
                              p4.group(env, "g2", "기관A", ("%하자보수%", p4.WARRANTY))],
                      claims=[p4.claim("c1", ["g1"], {"type": "number", "value": 130000000, "unit": "KRW"}),
                              p4.claim("c2", ["g2"], {"type": "number", "value": 12, "unit": "개월"})])

    def test_runs_score_groups_per_document_and_refuse_the_sealed_split(self):
        p4.write(self.env, "dev", self.dev_rows() + [self.compare_row()])
        self.assertTrue(evaluation.validate_gold(self.s, "dev")["ok"])
        (k1,) = evaluation.evaluate_retrieval(self.s, fixtures.analyzer(), None, "dev", ["K1"])
        config, scores = evaluation.load_run(self.s, k1["run_id"])
        agg = scores["aggregate"]
        self.assertEqual(agg["passage_rows"], 4)
        self.assertEqual(agg["ndcg_eligible_rows"], 3)  # the comparison has no single ranking
        self.assertEqual(agg["multi_evidence"]["complete@20"]["denominator"], 2)
        traces = {t["id"]: t for t in store.read_jsonl(self.s.data_dir / "runs" / k1["run_id"] / "traces.jsonl")}
        self.assertTrue(traces["dev-compare"]["metrics"]["per_document"])
        self.assertEqual(traces["dev-compare"]["wrong_scope"], 0)
        self.assertIn("git_revision", scores["provenance"]["code"])
        with self.assertRaisesRegex(evaluation.EvaluationError, "sealed"):
            evaluation.evaluate_retrieval(self.s, fixtures.analyzer(), None, "test", ["K1"])


class AnswerRunTest(GoldRetrievalTest):
    def setUp(self):
        super().setUp()
        p4.write(self.env, "dev", self.dev_rows() + [self.compare_row()])
        runs = evaluation.evaluate_retrieval(self.s, fixtures.analyzer(), None, "dev", ["K1", "K0"])
        self.k1, self.k0 = (r["run_id"] for r in runs)

    def answer_run(self, transport, runs=None, **kw):
        est = answers.plan_run(self.s, "answer-finalists", "dev", runs or [self.k1, self.k0])
        res = service.Resources(self.s, transport=transport, recover=True)
        try:
            return est, answers.run_answers(self.s, res, est["estimate_id"], "tester", **kw)
        finally:
            res.close()

    def test_a_complete_run_scores_and_a_rerun_dispatches_nothing(self):
        transport = FakeTransport()
        est, out = self.answer_run(transport)
        self.assertEqual(est["judge"]["attempts"], 0)
        self.assertEqual(out["status"], "complete")
        calls = len(transport.calls)
        self.assertGreater(calls, 0)
        scores = json.loads((answers.run_dir(self.s, out["run_id"]) / "scores.json").read_text(encoding="utf-8"))
        k1 = scores["finalists"][self.k1]
        self.assertEqual(k1["link_validity"]["rate"], 1.0)
        self.assertEqual(k1["scope_leaks"], 0)
        self.assertIsNotNone(k1["served_retrieval"])
        self.assertIsNotNone(scores["selection"])
        with store.open_db(self.s.db_path) as conn:
            purposes = {r[0] for r in conn.execute("SELECT purpose FROM attempts")}
        self.assertEqual(purposes, {"gold_eval"})
        _, again = self.answer_run(transport)
        self.assertEqual(len(transport.calls), calls)
        self.assertEqual(again["status"], "complete")

    def test_an_interrupted_run_keeps_its_rows_and_never_replays_unknown_billing(self):
        n = {"calls": 0}

        def flaky(messages):
            n["calls"] += 1
            if n["calls"] == 2:
                return ProviderError("APITimeoutError (injected)", pre_execution=False)
            return generation._echo_first_evidence(messages)

        transport = FakeTransport(flaky)
        _, out = self.answer_run(transport)
        self.assertEqual(out["status"], "partial")
        self.assertIn("unknown_billing", out["stop_reason"])
        progress = answers.load_progress(self.s, out["run_id"])
        done = {k for k, r in progress.items() if r["status"] == "done"}
        unknown = [r for r in progress.values() if r["status"] == "unknown_billing"]
        self.assertEqual(len(unknown), 1)
        self.assertTrue(done)
        before = len(transport.calls)
        _, again = self.answer_run(transport)  # resumes other rows; the unknown row is not sent again
        progress = answers.load_progress(self.s, out["run_id"])
        self.assertEqual(progress[(unknown[0]["finalist"], unknown[0]["question_id"])]["status"], "unknown_billing")
        self.assertEqual(again["status"], "partial")
        with store.open_db(self.s.db_path) as conn:
            per_request = conn.execute("SELECT request_id, COUNT(*) FROM attempts WHERE stage = 'generation' "
                                       "GROUP BY request_id HAVING COUNT(*) > 1").fetchall()
        self.assertEqual(per_request, [])
        self.assertGreater(len(transport.calls), before)
        # once its billing is settled from provider evidence, the row may run again as a recorded new attempt
        res = service.Resources(self.s, transport=transport)
        try:
            (attempt,) = [a for a in service.unresolved_attempts(res, auth.OWNER_CLI) if a["state"] == "unknown"]
            service.settle_from_evidence(res, auth.OWNER_CLI, attempt["attempt_id"],
                                         {"prompt_tokens": 100, "completion_tokens": 10}, None, "export", "reconciled")
        finally:
            res.close()
        _, final = self.answer_run(transport)
        self.assertEqual(final["status"], "complete")
        row = answers.load_progress(self.s, out["run_id"])[(unknown[0]["finalist"], unknown[0]["question_id"])]
        self.assertEqual((row["status"], row["attempt_no"]), ("done", 2))
        self.assertEqual(row["history"][0]["attempt_states"], ["settled"])

    def test_a_budget_refusal_stops_the_run_and_a_new_attempt_follows_more_room(self):
        transport = FakeTransport()
        with store.open_db(self.s.db_path) as conn:
            conn.execute("UPDATE budget_settings SET cap_micro_usd = 1")
        with self.assertRaisesRegex(answers.AnswerEvalError, "no longer fits"):
            self.answer_run(transport)
        est = answers.plan_run(self.s, "answer-finalists", "dev", [self.k1, self.k0])
        self.assertFalse(est["fits"])
        with store.open_db(self.s.db_path) as conn:
            conn.execute("UPDATE budget_settings SET cap_micro_usd = ?", (16 * budget.MICRO,))
        with mock.patch.object(budget, "reserve", side_effect=[
                {"admitted": False, "reason": "cap_exhausted", "attempt_id": None}]):
            _, out = self.answer_run(transport)
        self.assertIn("budget_blocked", out["stop_reason"])
        self.assertEqual(len(transport.calls), 0)
        _, out = self.answer_run(transport)
        self.assertEqual(out["status"], "complete")
        blocked = [r for r in answers.load_progress(self.s, out["run_id"]).values() if r["attempt_no"] == 2]
        self.assertEqual(len(blocked), 1)

    def test_changed_prices_or_prompts_need_a_new_plan(self):
        est = answers.plan_run(self.s, "answer-finalists", "dev", [self.k1])
        with mock.patch.object(generation, "PROMPT_VERSION", "grounded-answer-x"):
            res = service.Resources(self.s, transport=FakeTransport(), recover=True)
            try:
                with self.assertRaisesRegex(answers.AnswerEvalError, "changed since the estimate"):
                    answers.run_answers(self.s, res, est["estimate_id"], "tester")
            finally:
                res.close()
        with self.assertRaisesRegex(answers.AnswerEvalError, "one or two"):
            answers.plan_run(self.s, "answer-finalists", "dev", [self.k1, self.k0, "K1-other"])
        with self.assertRaisesRegex(answers.AnswerEvalError, "development split"):
            answers.plan_run(self.s, "answer-finalists", "test", [self.k1])

    def test_blind_review_sheet_and_import(self):
        transport = FakeTransport()
        _, out = self.answer_run(transport, runs=[self.k1])
        sheet = answers.export_review_sheet(self.s, out["run_id"])
        items = store.read_jsonl(Path(sheet["sheet"]))
        self.assertTrue(items)
        self.assertNotIn(self.k1, json.dumps(items, ensure_ascii=False))
        self.assertNotIn("kiwi_bm25", json.dumps(items, ensure_ascii=False))
        claim_items = [i for i in items if i["kind"] == "claim"]
        reviewed = [{**i, "verdict": "wrong_value"} for i in claim_items[:1]]
        path = self.root / "reviews.jsonl"
        store.write_jsonl_atomic(path, reviewed + [{"blind_id": "nope", "verdict": "correct"}])
        with self.assertRaises(answers.AnswerEvalError):
            answers.import_reviews(self.s, out["run_id"], path, "person-c")
        store.write_jsonl_atomic(path, reviewed)
        result = answers.import_reviews(self.s, out["run_id"], path, "person-c")
        self.assertEqual(result["imported"], 1)
        scored = store.read_jsonl(answers.run_dir(self.s, out["run_id"]) / "scored.jsonl")
        verdicts = [c for s in scored for c in s["claims"] if c.get("reviewed_by") == "person-c"]
        self.assertEqual([c["verdict"] for c in verdicts], ["wrong_value"])


class SealedTest(Phase4Case):
    answer_run = AnswerRunTest.answer_run

    def setUp(self):
        super().setUp()
        p4.write(self.env, "dev", self.dev_rows())
        test_row = p4.row(self.env, "test-seat", "도서관에서 무엇을 만드는 사업인가요?", "기관D", split="test",
                          groups=[p4.group(self.env, "g1", "기관D", ("%좌석%", p4.SEATS))],
                          claims=[p4.claim("c1", ["g1"], {"type": "text", "patterns": ["좌석 예약"]})])
        p4.write(self.env, "test", [test_row])
        (k1,) = evaluation.evaluate_retrieval(self.s, fixtures.analyzer(), None, "dev", ["K1"])
        self.k1, self.k0 = k1["run_id"], None
        decision = self.root / "decision.json"
        decision.write_text(json.dumps({"run_id": self.k1, "mode": "kiwi_bm25", "decided_by": "owner",
                                        "rationale": "K1 baseline"}), encoding="utf-8")
        evaluation.activate_run(self.s, self.k1, decision)

    def freeze(self, answer_run: str) -> dict:
        return sealed.freeze_release(self.s, auth.OWNER_CLI, self.k1, answer_run, "owner", "K1 selected")

    def sealed_run(self, transport, post_test=False, reason=None):
        est = answers.plan_run(self.s, "sealed", freeze_id=self.freeze_id, post_test=post_test)
        res = service.Resources(self.s, transport=transport, recover=True)
        try:
            return answers.run_answers(self.s, res, est["estimate_id"], "owner", reason)
        finally:
            res.close()

    def test_freeze_then_one_sealed_run_then_only_labeled_regressions(self):
        transport = FakeTransport()
        _, dev = self.answer_run(transport, runs=[self.k1])
        with self.assertRaisesRegex(sealed.SealedError, "not frozen"):
            self.freeze(dev["run_id"])
        evaluation.freeze_dataset(self.s, "dev", "owner", "dev frozen")
        evaluation.freeze_dataset(self.s, "test", "owner", "test frozen")
        with self.assertRaises(auth.AuthError):
            sealed.freeze_release(self.s, Principal("v", frozenset({"verifier"})), self.k1,
                                  dev["run_id"], "owner", "r")
        freeze = self.freeze(dev["run_id"])
        self.freeze_id = freeze["freeze_id"]
        self.assertFalse(freeze["post_test"])
        out = self.sealed_run(transport)
        self.assertEqual(out["status"], "complete")
        self.assertTrue(answers.run_dir(self.s, out["run_id"]).is_relative_to(self.s.data_dir / "sealed"))
        with self.assertRaisesRegex(sealed.SealedError, "already complete"):
            self.sealed_run(transport)
        with self.assertRaisesRegex(sealed.SealedError, "reason"):
            self.sealed_run(transport, post_test=True)
        post = self.sealed_run(transport, post_test=True, reason="재현 확인")
        self.assertTrue(post["run_id"].endswith("-post"))
        config = json.loads((answers.run_dir(self.s, post["run_id"]) / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["label"], "post-test regression")
        actions = [e["action"] for e in service.audit_events(service.Resources(self.s), auth.OWNER_CLI)]
        self.assertIn("freeze_release", actions)
        self.assertEqual(actions.count("sealed_run_started"), 2)
        # a changed prompt breaks the freeze: no sealed plan under it
        with mock.patch.object(generation, "PROMPT_VERSION", "grounded-answer-x"):
            with self.assertRaisesRegex(answers.AnswerEvalError, "freeze no longer holds"):
                answers.plan_run(self.s, "sealed", freeze_id=self.freeze_id)


class EvaluationScreenTest(GoldRetrievalTest):
    """The verifier's phase-4 tab and the gold-2 review form, rendered with Streamlit's AppTest."""

    def setUp(self):
        super().setUp()
        self.transport = FakeTransport()
        self.res = service.Resources(self.s, transport=self.transport, recover=True)

    def tearDown(self):
        self.res.close()
        super().tearDown()

    def app(self, body: str):
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_string("import streamlit as st\nfrom rfp_assistant import ui\n" + body)
        app.session_state["res"], app.session_state["principal"] = self.res, auth.visitor("person-b")
        return app

    def test_the_tab_estimates_then_runs_a_development_evaluation_with_consent(self):
        import time

        p4.write(self.env, "dev", self.dev_rows())
        evaluation.validate_gold(self.s, "dev")
        (k1,) = evaluation.evaluate_retrieval(self.s, fixtures.analyzer(), None, "dev", ["K1"])
        decision = self.root / "decision.json"
        decision.write_text(json.dumps({"run_id": k1["run_id"], "mode": "kiwi_bm25", "decided_by": "owner",
                                        "rationale": "baseline"}), encoding="utf-8")
        evaluation.activate_run(self.s, k1["run_id"], decision)
        app = self.app("ui._evaluation_tab(st, st.session_state.res, st.session_state.principal)\n")
        app.run(timeout=60)
        self.assertFalse(app.exception, [e.message for e in app.exception])
        self.assertTrue(any("봉인 시험 세트" in c.value for c in app.caption))
        next(b for b in app.button if b.key == "eval-plan").click().run(timeout=60)
        est = app.session_state["eval_estimate"]
        run_button = next(b for b in app.button if b.key == f"eval-run-{est['estimate_id']}")
        self.assertTrue(run_button.disabled)  # no consent yet
        self.assertEqual(len(self.transport.calls), 0)
        app.checkbox(key=f"eval-agree-{est['estimate_id']}").check().run(timeout=60)
        next(b for b in app.button if b.key == f"eval-run-{est['estimate_id']}").click().run(timeout=60)
        self.assertFalse(app.exception, [e.message for e in app.exception])
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and service._EVAL_JOBS[est["run_id"]].is_alive():
            time.sleep(0.1)
        scores = json.loads((answers.run_dir(self.s, est["run_id"]) / "scores.json").read_text(encoding="utf-8"))
        self.assertEqual(scores["status"], "complete")
        with self.assertRaises(service.ServiceError):  # the sealed path is not a verifier action
            service.start_answer_evaluation(self.res, auth.visitor("x"), "missing")

    def test_a_gold_row_needs_the_inspection_attestation(self):
        rows = [p4.deadline_row(self.env)]
        rows[0]["review"].update(reviewed_by=None, approved_at=None, status="pending", original_inspected=False)
        path = self.root / "b.jsonl"
        store.write_jsonl_atomic(path, rows)
        gold.submit(self.s, path, "b1", "dev", "agent-a")
        app = self.app("ui.gold_review_page(st, st.session_state.res, st.session_state.principal)\n")
        app.run(timeout=60)
        self.assertFalse(app.exception, [e.message for e in app.exception])
        self.assertTrue(any("필수 주장" in m.value for m in app.markdown))
        submit = lambda: next(b for b in app.button if "승인" in b.label)  # noqa: E731
        submit().click().run(timeout=60)
        self.assertTrue(any("원문을 직접 확인" in e.value for e in app.error))
        self.assertEqual(gold.candidate(self.s, "dev-deadline-r1")["status"], "pending")
        app.checkbox[0].check().run(timeout=60)
        submit().click().run(timeout=60)
        self.assertFalse(app.exception, [e.message for e in app.exception])
        self.assertEqual(gold.candidate(self.s, "dev-deadline-r1")["status"], "approved")


if __name__ == "__main__":
    unittest.main()
