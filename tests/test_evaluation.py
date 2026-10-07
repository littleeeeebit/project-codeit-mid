"""Phase 4: gold-2 validation and freezing, grouped evidence metrics, answer runs with safe resume, blind review,
the release freeze and the single sealed run. Fake provider and temporary corpus only."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rfp_assistant.contracts import Principal
from rfp_assistant.evaluation import evaluation, gold, sealed
from rfp_assistant.gateway import budget, generation
from rfp_assistant.gateway.generation import FakeTransport, ProviderError
from rfp_assistant.retrieval.retrieval import KeywordIndex
from rfp_assistant.service import answers, auth, service
from rfp_assistant.settings import Settings
from rfp_assistant.storage import store
from tests import fixtures
from tests import release_fixtures as p4


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

    def test_conflict_status_without_a_next_action_does_not_pass_negative_handling(self):
        row = {**gold_row([]), 'question_type': 'cross_document', 'answerability': 'conflicting',
               'expected_status': 'conflicting_evidence', 'mode': 'single', 'scope': [{'doc_id': 'd'}]}
        conflict = {'field': 'warranty', 'alternatives': [{'doc_id': 'd', 'value': '1년'}, {'doc_id': 'd', 'value': '2년'}]}
        for action in (None, ' ', 'Ask the purchaser which conflicting condition controls'):
            record = {'finalist': 'F', 'outcome': 'conflicting_evidence',
                      'answer': {'next_action': action, 'conflicts': [conflict]}}
            scored = answers.score_record(row, record, None, {})
            self.assertEqual(answers.aggregate_answers([scored])['negative_handling']['numerator'],
                             1 if action and action.strip() else 0)

    def test_conflict_answers_fail_the_shared_status_rule_offline(self):
        row = {**gold_row([]), 'question_type': 'revision_conflict', 'answerability': 'conflicting',
               'expected_status': 'conflicting_evidence', 'mode': 'single', 'scope': [{'doc_id': 'd'}]}
        alt = {'doc_id': 'd', 'value': '1년', 'evidence_ids': ['E1']}
        action = 'Ask the purchaser which condition controls'
        for name, conflicts, ok in (('no conflicts', [], False), ('one alternative', [{'field': 'f', 'alternatives': [alt]}], False),
                                    ('two alternatives', [{'field': 'f', 'alternatives': [alt, alt]}], True)):
            with self.subTest(name):
                record = {'finalist': 'F', 'outcome': 'conflicting_evidence',
                          'answer': {'next_action': action, 'conflicts': conflicts}}
                self.assertEqual(answers.score_record(row, record, None, {})['status_ok'], ok)

    def test_a_run_graded_under_another_version_is_never_rescored(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Settings(source_dir=Path(tmp), data_dir=Path(tmp), hwp_converter=None)
            d = answers.run_dir(s, 'A-0123456789ab')
            d.mkdir(parents=True)
            (d / 'config.json').write_text(json.dumps({'eval_version': 'answer-eval-1'}), encoding='utf-8')
            with self.assertRaisesRegex(answers.AnswerEvalError, 'graded under answer-eval-1'):
                answers.finalize(s, 'A-0123456789ab')
        self.assertNotEqual(answers.ANSWER_EVAL_VERSION, 'answer-eval-1')
        self.assertNotEqual(generation.PROMPT_VERSION, 'grounded-answer-12')

    def test_status_must_match_the_rows_own_statuses(self):
        row = {**gold_row([]), 'question_type': 'missing_false_premise', 'answerability': 'unanswerable',
               'expected_status': 'insufficient_evidence', 'mode': 'single', 'scope': [{'doc_id': 'd'}]}
        record = {'finalist': 'F', 'outcome': 'clarification_required', 'answer': {}}
        scored = answers.score_record(row, record, None, {})
        self.assertEqual((scored['status_ok'], scored['passed']), (False, False))
        listed = {**row, 'accepted_statuses': ['clarification_required']}
        scored = answers.score_record(listed, record, None, {})
        self.assertEqual((scored['status_ok'], scored['passed']), (True, True))
        check = evaluation.GoldChecker.__new__(evaluation.GoldChecker)._check_kind
        base = {**row, 'question': 'q?', 'difficulty_reason': 'r', 'as_of_date': '2024-05-01'}
        self.assertEqual(check({**base, 'accepted_statuses': ['clarification_required']}, 'r'), [])
        for bad in (['answered'], 'clarification_required', ['conflicting_evidence']):
            with self.subTest(bad=bad):
                self.assertTrue(any('accepted_statuses' in e for e in check({**base, 'accepted_statuses': bad}, 'r')))
        answerable = {**base, 'answerability': 'answerable', 'expected_status': 'answered'}
        self.assertTrue(any('accepted_statuses' in e for e in check(
            {**answerable, 'accepted_statuses': ['insufficient_evidence']}, 'r')))

    def test_a_link_supporting_part_of_a_claim_never_supports_the_whole_claim(self):
        row = {**gold_row([AMOUNT_G]), 'question_type': 'direct_fact', 'answerability': 'answerable',
               'expected_status': 'answered', 'mode': 'single', 'scope': [{'doc_id': 'd'}]}
        claim = '예산은 1억 3천만 원이며 누구나 낙찰받는다'
        record = {'finalist': 'F', 'outcome': 'answered',
                  'evidence': {'E1': {'doc_id': 'd', 'chunk_id': 'c1', 'quote': '사업 예산은 금 130,000,000원으로 한다.'}},
                  'answer': {'claims': [{'text': claim, 'kind': 'source_fact', 'doc_id': 'd', 'evidence_ids': ['E1']}]}}
        reviews = {'F|q|link|0|E1': {'verdict': 'supporting', 'reviewer': 'r1'}}
        scored = answers.score_record(row, record, None, reviews)
        self.assertEqual(scored['links'][0]['support'], 'supporting')
        self.assertIsNot(scored['answer_claims'][0]['supported'], True)
        agg = answers.aggregate_answers([scored])
        self.assertEqual((agg['citation_coverage']['numerator'], agg['answer_claims_unjudged']), (0, 1))
        reviews['F|q|answer_claim|0'] = {'verdict': 'supported', 'reviewer': 'r2'}
        self.assertIs(answers.score_record(row, record, None, reviews)['answer_claims'][0]['supported'], True)
        quoted = {**record, 'answer': {'claims': [{'text': '사업 예산은 금 130,000,000원', 'kind': 'source_fact',
                                                   'doc_id': 'd', 'evidence_ids': ['E1']}]}}
        self.assertIs(answers.score_record(row, quoted, None, {})['answer_claims'][0]['supported'], True)

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
        # another alternative of a credited fact is a different passage: it keeps its rank and earns nothing
        self.assertEqual((m["graded@5"], m["duplicates_removed"]), ([2, 0], 0))

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

    def citation_case(self, *texts: str, groups=None, claims=None):
        from types import SimpleNamespace

        claims = claims or [p4.claim("c1", ["g1"], {"type": "number", "value": 130000000, "unit": "KRW"}, "amount")]
        row = {**gold_row(groups or [AMOUNT_G]), "question_type": "direct_fact", "answerability": "answerable",
               "expected_status": "answered", "mode": "single", "scope": [{"doc_id": "d"}], "required_claims": claims}
        index = SimpleNamespace(chunks=[{"chunk_id": "k1", **chunk("amount", 0, 30)}], elements=ELS)
        record = {"finalist": "F", "outcome": "answered", "link_validity": {"E1": True},
                  "evidence": {"E1": {"doc_id": "d", "chunk_id": "k1", "quote": ELS[("x", "amount")]["raw_text"]}},
                  "answer": {"summary": "", "claims": [{"doc_id": "d", "text": t, "evidence_ids": ["E1"]}
                                                       for t in texts]}}
        out = answers.score_record(row, record, index, {})
        return out, answers.aggregate_answers([out])

    def test_a_hallucinated_claim_sharing_a_correct_citation_is_not_supported(self):
        """Review rounds 1-2: a cited chunk holding a complete gold span is retrieval relevance. Only a claim quoted
        verbatim from its citation is supported automatically; anything else reaches blind review."""
        out, agg = self.citation_case("사업 예산은 금 130,000,000원으로 한다.", "누구나 무조건 낙찰받는다.",
                                      "사업 예산은 130,000,000원이며 누구나 무조건 낙찰받는다.")
        self.assertEqual([link["support"] for link in out["links"]], ["supporting", "unjudged", "unjudged"])
        self.assertEqual([a["supported"] for a in out["answer_claims"]], [True, None, None])
        self.assertEqual(agg["citation_precision_lower_bound"]["numerator"], 1)
        self.assertEqual(agg["answer_claims_unjudged"], 2)

    def test_a_single_sentence_with_a_correct_value_and_a_hallucination_goes_to_review(self):
        out, agg = self.citation_case("사업 예산은 130,000,000원이며 누구나 무조건 낙찰받는다.")
        self.assertEqual(out["claims"][0]["verdict"], "correct")  # the typed value is stated
        self.assertEqual((out["links"][0]["support"], out["answer_claims"][0]["supported"]), ("unjudged", None))
        self.assertEqual((agg["citation_precision_lower_bound"]["numerator"], agg["answer_claims_unjudged"]), (0, 1))

    def test_a_negated_qualifier_and_a_citation_of_one_of_two_groups(self):
        claims = [p4.claim("c1", ["g1", "g2"], {"type": "number", "value": 130000000, "unit": "KRW"}, "amount",
                           qualifiers=[["부가가치세를 포함", "부가가치세 포함", "부가세 포함"]])]
        out, _ = self.citation_case("사업 예산은 130,000,000원이며 부가가치세를 포함하지 않은 금액이다.",
                                    groups=[AMOUNT_G, VAT_G], claims=claims)
        self.assertEqual(out["claims"][0]["verdict"], "incomplete_qualifier")
        self.assertEqual(out["links"][0]["support"], "unjudged")
        # verbatim from the amount span: the link supports what it states, but the VAT group is not established
        out, _ = self.citation_case("사업 예산은 금 130,000,000원으로 한다.", groups=[AMOUNT_G, VAT_G], claims=claims)
        self.assertEqual((out["links"][0]["support"], out["claims"][0]["verdict"]),
                         ("supporting", "incomplete_qualifier"))

    def test_overlapping_duplicates_count_once_and_rechunking_keeps_the_truth(self):
        row = gold_row([AMOUNT_G])
        a = evaluation.score_row(row, [chunk("amount", 0, 30), chunk("amount", 5, 30)], [], ELS)
        b = evaluation.score_row(row, [chunk("amount", 0, 30)], [], ELS)
        c = evaluation.score_row(row, [chunk("amount", 3, 40)], [], ELS)  # another chunking of the same span
        self.assertEqual((a["graded@5"], a["duplicates_removed"]), ([2], 1))
        self.assertEqual((a["recall@20"], a["ndcg@5"]), (b["recall@20"], b["ndcg@5"]))
        self.assertEqual((b["recall@20"], b["ndcg@5"]), (c["recall@20"], c["ndcg@5"]))

    def test_repeated_spans_do_not_consume_rank_positions(self):
        """Review round 4 (F7): overlapping repeats of a credited span are removed before rank cutoffs, so they
        neither push a second group down nor out of the top five; an unrelated chunk still costs its position."""
        row = gold_row([AMOUNT_G, VAT_G])
        clean = evaluation.score_row(row, [chunk("amount", 0, 30), chunk("vat", 0, 8)], [], ELS)
        interleaved = evaluation.score_row(row, [chunk("amount", 0, 30), chunk("amount", 5, 30),
                                                 chunk("vat", 0, 8)], [], ELS)
        crossing = evaluation.score_row(row, [chunk("amount", 0, 30)] + [chunk("amount", i, 30) for i in range(1, 5)]
                                        + [chunk("vat", 0, 8)], [], ELS)
        for m in (interleaved, crossing):
            self.assertEqual((m["ndcg@5"], m["graded@5"], m["recall@20"], m["mrr"]),
                             (clean["ndcg@5"], clean["graded@5"], clean["recall@20"], clean["mrr"]))
        self.assertEqual((interleaved["duplicates_removed"], crossing["duplicates_removed"]), (1, 4))
        miss = evaluation.score_row(row, [chunk("amount", 0, 30), chunk("alt", 0, 5), chunk("vat", 0, 8)], [], ELS)
        self.assertEqual((miss["graded@5"], miss["duplicates_removed"]), ([2, 0, 1], 0))
        self.assertLess(miss["ndcg@5"], clean["ndcg@5"])
        # Review round 6: two disjoint pieces of one quote are distinct partial support, not a repeat
        left, right = chunk("vat", 6, 10), chunk("vat", 10, 16)
        halves = evaluation.score_row(row, [left, right, chunk("amount", 0, 30)], [], ELS)
        self.assertEqual((halves["duplicates_removed"], halves["graded@5"], halves["mrr"]), (0, [1, 0, 2], 0.3333))
        again = evaluation.score_row(row, [left, right, chunk("vat", 7, 9), chunk("amount", 0, 30)], [], ELS)
        self.assertEqual((again["duplicates_removed"], again["mrr"]), (1, 0.3333))  # inside covered characters
        # packed figures stay faithful to what was packed, repeats included
        packed = evaluation.score_row(row, [], [chunk("amount", 0, 30), chunk("amount", 5, 30)], ELS)
        self.assertEqual((packed["packed_grades"], packed["packed_complete"]), ([2, 0], 0))

    def test_pieces_of_one_table_cell_are_distinct_partial_support(self):
        cells = [{"row": 0, "col": 0, "text": "항목"}, {"row": 0, "col": 1, "text": "금액"},
                 {"row": 1, "col": 0, "text": "사업비"}, {"row": 1, "col": 1, "text": "총 사업비 130,000,000원 부가세 포함"}]
        els = {("x", "t"): {"raw_text": "", "table": {"cells": cells}}}
        line = "사업비 | 총 사업비 130,000,000원 부가세 포함"
        quote = "130,000,000원 부가세 포함"
        a = line.index(quote)
        piece = lambda start, end: {"extraction_id": "x", "spans": [  # noqa: E731
            {"element_id": "t", "rows": [0, 1], "fragment": {"row": 1, "start": start, "end": end}}]}
        groups = [grp("g1", ("t", quote))]
        first, second, inner = piece(a, a + 6), piece(a + 6, len(line)), piece(a + 1, a + 4)
        self.assertEqual([evaluation.group_grade(c, groups[0], els) for c in (first, second, inner)], [1, 1, 1])
        ranking, removed = evaluation.dedup_ranking([first, second, inner], groups, els)
        self.assertEqual((ranking, removed), ([first, second], 1))
        whole = piece(0, len(line))
        self.assertEqual(evaluation.dedup_ranking([whole, first], groups, els), ([whole], 1))

    def test_ndcg_counts_the_same_group_units_as_its_ideal(self):
        """Review round 9 (F8): one passage completing two groups scores as two passages would; numerator, ideal
        and cutoff all count required evidence groups."""
        row = gold_row([AMOUNT_G, VAT_G])
        amount, vat = chunk("amount", 0, 30), chunk("vat", 0, 30)
        joined = {"extraction_id": "x", "spans": amount["spans"] + vat["spans"]}
        compound = evaluation.score_row(row, [joined], [joined], ELS)
        split = evaluation.score_row(row, [amount, vat], [amount, vat], ELS)
        self.assertEqual((compound["ndcg@5"], compound["graded@5"], compound["complete@20"], compound["mrr"]),
                         (1.0, [2, 2], 1, 1.0))
        self.assertEqual((split["ndcg@5"], split["graded@5"]), (1.0, [2, 2]))
        summary = lambda run_id, label, m: {  # noqa: E731
            "run_id": run_id, "label": label, "mode": label, "blocking": [], "population_sha256": "p",
            "critical_failures": [], "ndcg@5": m["ndcg@5"]}
        rec = evaluation.recommend([summary("K1-joined", "K1", compound), summary("H-split", "H", split)])
        self.assertEqual(rec["selected"]["run_id"] if isinstance(rec["selected"], dict) else rec["selected"],
                         "K1-joined")  # fragmenting complete evidence is no measured benefit
        # one group missing is worse however it is chunked, and an unrelated passage still costs its rank
        lone = evaluation.score_row(row, [amount], [], ELS)
        noisy = evaluation.score_row(row, [chunk("alt", 0, 5, x="y"), joined], [], ELS)
        self.assertLess(lone["ndcg@5"], compound["ndcg@5"])
        self.assertEqual((noisy["graded@5"], noisy["unlabelled@5"]), ([0, 2, 2], 1))
        self.assertLess(noisy["ndcg@5"], compound["ndcg@5"])
        # partial then complete support of one group earns at most that group's ideal gain
        raised = evaluation.score_row(gold_row([VAT_G]), [chunk("vat", 6, 10), chunk("vat", 0, 30)], [], ELS)
        self.assertEqual(raised["graded@5"], [1, 2])
        self.assertLess(raised["ndcg@5"], 1.0)
        agg = evaluation.aggregate([{"id": "q", "type": "t", "metrics": noisy, "families": ["f"]}], [])
        self.assertEqual(agg["ndcg_pool"]["unlabelled_top5_passages"], 1)

    def test_an_unreviewed_pool_never_promotes_a_run(self):
        """Review round 10 (F9): a gain that may come only from passages outside the gold labels is provisional;
        once the pool is judged, the same rankings decide normally."""
        els = {**ELS, ("x", "both"): {"raw_text": ELS[("x", "amount")]["raw_text"] + " " + ELS[("x", "vat")]["raw_text"],
                                      "table": None}}
        amount, vat, both = chunk("amount", 0, 30), chunk("vat", 0, 30), chunk("both", 0, 60)

        def summaries(groups, k1_ranking, h_ranking):
            out = []
            for label, ranking in (("K1", k1_ranking), ("H", h_ranking)):
                m = evaluation.score_row(gold_row(groups), ranking, [], els)
                agg = evaluation.aggregate([{"id": "q", "type": "t", "metrics": m, "families": ["f"]}], [])
                out.append({"run_id": f"{label}-run", "label": label, "mode": label, "blocking": [],
                            "population_sha256": "p", "critical_failures": [], "ndcg@5": agg["ndcg@5"],
                            "ndcg_pool": agg.get("ndcg_pool")})
            return out

        labelled = [AMOUNT_G, VAT_G]
        before = summaries(labelled, [both, amount, vat], [amount, vat, both])
        self.assertEqual([(r["ndcg@5"], evaluation.pool_pending(r)) for r in before], [(0.6934, 1), (1.0, 1)])
        rec = evaluation.recommend(before)
        self.assertEqual((rec["status"], rec["selected"], rec["finalist"], rec["provisional"]),
                         ("pending_pool_review", "K1-run", None, True))
        self.assertEqual(rec["pool_pending"], {"K1-run": 1, "H-run": 1})
        # the review finds the compound passage holds both facts: new alternatives, rescored, no benefit remains
        judged = [grp("g1", ("amount", "130,000,000원"), ("both", "130,000,000원")),
                  grp("g2", ("vat", "부가가치세를 포함한"), ("both", "부가가치세를 포함한"))]
        after = summaries(judged, [both, amount, vat], [amount, vat, both])
        self.assertEqual([(r["ndcg@5"], evaluation.pool_pending(r)) for r in after], [(1.0, 0), (1.0, 0)])
        rec = evaluation.recommend(after)
        self.assertEqual((rec["status"], rec["selected"]), ("final", "K1-run"))
        # a fully judged comparison with a real gain still promotes
        real = summaries(judged, [chunk("vat", 6, 10), amount, vat], [amount, vat])
        rec = evaluation.recommend(real)
        self.assertEqual((rec["status"], rec["selected"], rec["finalist"]), ("final", "H-run", "K1-run"))
        # the prerequisite holds on both sides: an unreviewed baseline or an unreviewed candidate keeps it pending
        for k1_pending, h_pending in ((1, 0), (0, 1)):
            one_sided = [{**real[0], "ndcg_pool": {"unlabelled_top5_passages": k1_pending}},
                         {**real[1], "ndcg_pool": {"unlabelled_top5_passages": h_pending}}]
            rec = evaluation.recommend(one_sided)
            self.assertEqual((rec["status"], rec["selected"], rec["finalist"]), ("pending_pool_review", "K1-run", None))
        # pilot summaries have no pool and keep the phase-2 policy
        pilot = [{**r, "ndcg_pool": None} for r in before]
        self.assertEqual((evaluation.recommend(pilot)["status"], evaluation.recommend(pilot)["selected"]),
                         ("final", "H-run"))

    def test_the_reviewers_occurrence_and_cells_are_the_target(self):
        """Review round 11 (F10): a repeated quote is scored at the occurrence the reviewer approved."""
        raw = "이전 계약 하자보수: 12개월. 현재 계약 하자보수: 12개월."
        q = "12개월"
        first, second = raw.index(q), raw.rindex(q)
        els = {("x", "p"): {"raw_text": raw, "table": None}}
        at = lambda p, n=len(q): {"extraction_id": "x", "spans": [{"element_id": "p", "start": p, "end": p + n}]}  # noqa: E731
        pinned = {"group_id": "g1", "doc_id": "d", "alternatives": [
            {"element_id": "p", "quote": q, "extraction_id": "x", "offsets": [second, second + len(q)]}]}
        row = {**gold_row([pinned]), "evidence_groups": [pinned]}
        m = evaluation.score_row(row, [at(second)], [at(second)], els)
        self.assertEqual((m["hit@20"], m["recall@20"], m["ndcg@5"], m["packed_complete"], m["unlabelled@5"]),
                         (1, 1.0, 1.0, 1, 0))
        self.assertEqual(evaluation.score_row(row, [at(first)], [], els)["recall@20"], 0.0)
        # offsets with context: the occurrence inside them is the target
        context = {**pinned["alternatives"][0], "offsets": [raw.index("현재"), len(raw)]}
        self.assertEqual(evaluation.grade(at(second), context, els[("x", "p")]), 2)
        self.assertEqual(evaluation.grade(at(first), context, els[("x", "p")]), 0)
        self.assertEqual(evaluation.grade(at(second, 2), context, els[("x", "p")]), 1)  # part of the approved one
        # dedup compares the approved coverage: the first occurrence is no repeat of the second
        self.assertEqual(evaluation.dedup_ranking([at(second), at(first)], [pinned], els)[1], 0)
        # a coordinate-free (older) alternative keeps the first occurrence
        free = {k: v for k, v in pinned["alternatives"][0].items() if k != "offsets"}
        self.assertEqual(evaluation.grade(at(first), free, els[("x", "p")]), 2)

    def test_answer_coverage_is_graded_on_the_approved_occurrence_not_the_element(self):
        """Review round 1 (F1): both occurrences packed, gold pinned to the second, only the first cited."""
        raw = "이전 계약 하자보수: 12개월. 현재 계약 하자보수: 12개월."
        q = "12개월"
        first, second = raw.index(q), raw.rindex(q)
        chunk = lambda cid, p: {"chunk_id": cid, "extraction_id": "x",  # noqa: E731
                                "spans": [{"element_id": "p", "start": p, "end": p + len(q)}]}
        index = mock.Mock(chunks=[chunk("c1", first), chunk("c2", second)],
                          elements={("x", "p"): {"raw_text": raw, "table": None}})
        pinned = {"group_id": "g1", "doc_id": "d", "alternatives": [
            {"element_id": "p", "quote": q, "extraction_id": "x", "offsets": [second, second + len(q)]}]}
        row = {**gold_row([pinned]), "question_type": "t", "answerability": "answerable",
               "expected_status": "answered", "scope": [{"doc_id": "d"}]}
        ev = lambda cid: {"doc_id": "d", "chunk_id": cid, "element_ids": ["p"], "quote": q}  # noqa: E731
        cite = lambda *ids: {"finalist": "K", "outcome": "answered", "evidence": {"E1": ev("c1"), "E2": ev("c2")},  # noqa: E731
                             "answer": {"claims": [{"text": q, "kind": "source_fact", "doc_id": "d",
                                                    "evidence_ids": list(ids)}]}}
        wrong = answers.score_record(row, cite("E1"), index, {})
        self.assertEqual((wrong["groups"], wrong["passed"]), ({"gold": 1, "retrieved": 1, "cited": 0}, False))
        right = answers.score_record(row, cite("E2"), index, {})
        self.assertEqual((right["groups"], right["passed"]), ({"gold": 1, "retrieved": 1, "cited": 1}, True))
        # only the first occurrence packed: retrieval never reached the approved one
        reached = answers.score_record(row, {**cite("E1"), "evidence": {"E1": ev("c1")}}, index, {})
        self.assertEqual((reached["groups"], reached["passed"]), ({"gold": 1, "retrieved": 0, "cited": 0}, False))
        # without the index nothing can be graded, so nothing passes
        self.assertEqual(answers.score_record(row, cite("E2"), None, {})["passed"], False)

    def test_a_shared_original_credits_only_the_document_the_evidence_is_attributed_to(self):
        """Review round 3 (F1): byte-identical originals of A and C share one chunk; only A's evidence is cited."""
        raw = "하자보수 기간: 12개월."
        q = "12개월"
        start = raw.index(q)
        index = mock.Mock(chunks=[{"chunk_id": "c1", "extraction_id": "x",
                                   "spans": [{"element_id": "p", "start": start, "end": start + len(q)}]}],
                          elements={("x", "p"): {"raw_text": raw, "table": None}})
        group = lambda doc: {"group_id": f"g-{doc}", "doc_id": doc, "alternatives": [  # noqa: E731
            {"element_id": "p", "quote": q, "extraction_id": "x", "offsets": [start, start + len(q)]}]}
        row = {**gold_row([group("A"), group("C")]), "question_type": "t", "answerability": "answerable",
               "expected_status": "answered", "mode": "compare", "scope": [{"doc_id": "A"}, {"doc_id": "C"}]}
        ev = lambda doc: {"doc_id": doc, "chunk_id": "c1", "element_ids": ["p"], "quote": q}  # noqa: E731
        record = {"finalist": "K", "outcome": "answered", "evidence": {"E1": ev("A"), "E2": ev("C")},
                  "answer": {"claims": [{"text": q, "kind": "source_fact", "doc_id": "A", "evidence_ids": ["E1"]}],
                             "missing_fields": [{"doc_id": "C", "field": "하자보수 기간"}]}}
        one_side = answers.score_record(row, record, index, {})
        self.assertEqual((one_side["groups"], one_side["passed"]), ({"gold": 2, "retrieved": 2, "cited": 1}, False))
        both = {**record, "answer": {**record["answer"], "missing_fields": [], "claims": [
            *record["answer"]["claims"], {"text": q, "kind": "source_fact", "doc_id": "C", "evidence_ids": ["E2"]}]}}
        self.assertEqual(answers.score_record(row, both, index, {})["groups"], {"gold": 2, "retrieved": 2, "cited": 2})
        # evidence packed only for A never reaches C's group either
        only_a = {**record, "evidence": {"E1": ev("A")}}
        self.assertEqual(answers.score_record(row, only_a, index, {})["groups"], {"gold": 2, "retrieved": 1, "cited": 1})
        # the served-retrieval report splits a comparison the same way: by the evidence's document, not its extraction
        index.row_of = {"c1": 0}
        row["scope"] = [{"doc_id": d, "source_hash": "h", "extraction_id": "x"} for d in ("A", "C")]
        trace = {"retrieval": {"ranking": ["c1", "c1"], "evidence": [{"doc_id": "A", "chunk_id": "c1"}]}}
        conn = mock.MagicMock()
        conn.__enter__.return_value.execute.return_value.fetchone.return_value = (json.dumps(trace),)
        with mock.patch.object(answers, "open_db", return_value=conn):
            served = answers.served_retrieval(mock.Mock(), index, row, {**only_a, "request_id": "r"})
        self.assertEqual((served["metrics"]["packed_grades"], served["metrics"]["unit_grades@20"]), ([2, 0], [2, 2]))

    def test_named_cells_pick_the_approved_row(self):
        cells = [{"row": 0, "col": 0, "text": "이전 계약"}, {"row": 0, "col": 1, "text": "12개월"},
                 {"row": 1, "col": 0, "text": "현재 계약"}, {"row": 1, "col": 1, "text": "12개월"}]
        el = {"raw_text": "이전 계약 | 12개월\n현재 계약 | 12개월", "table": {"cells": cells}}
        rows = lambda *r: {"extraction_id": "x", "spans": [{"element_id": "t", "rows": list(r)}]}  # noqa: E731
        unit = {"element_id": "t", "quote": "12개월", "extraction_id": "x", "cells": [[1, 1]]}
        self.assertEqual([evaluation.grade(rows(1), unit, el), evaluation.grade(rows(0), unit, el)], [2, 1])
        free = {k: v for k, v in unit.items() if k != "cells"}
        self.assertEqual(evaluation.grade(rows(1), free, el), 1)  # without cells both rows are the target
        by_offsets = {**free, "offsets": [el["raw_text"].rindex("12개월"), len(el["raw_text"])]}
        self.assertEqual(evaluation.grade(rows(1), by_offsets, el), 2)
        # fragments of the approved row: the named cell's characters only
        line = dict(evaluation.table_rows(el))[1]
        a = line.rindex("12개월")
        piece = lambda r, s, e: {"extraction_id": "x", "spans": [  # noqa: E731
            {"element_id": "t", "rows": [r], "fragment": {"row": r, "start": s, "end": e}}]}
        self.assertEqual(evaluation.grade(piece(1, a, len(line)), unit, el), 2)
        self.assertEqual(evaluation.grade(piece(1, a, a + 2), unit, el), 1)
        self.assertEqual(evaluation.grade(piece(0, 0, len(line)), unit, el), 1)  # same table, not the approved row
        self.assertEqual(evaluation.coverage(piece(1, a, a + 2), unit, el), frozenset(("f", 1, i) for i in range(a, a + 2)))

    def test_a_repeated_value_in_one_row_is_found_at_the_approved_column(self):
        """Review round 12 (F10): fragments of one row pick the named column or the offset occurrence, not the
        first equal value in the row."""
        cells = [{"row": 0, "col": i, "text": t} for i, t in enumerate(["이전 계약", "12개월", "현재 계약", "12개월"])]
        cells += [{"row": 1, "col": 0, "text": "비고"}, {"row": 1, "col": 1, "text": "12개월 이내 하자보수"}]
        from rfp_assistant.corpus.ingestion import render_table

        el = {"raw_text": render_table(cells), "table": {"cells": cells}}
        line = dict(evaluation.table_rows(el))[0]
        approved, earlier = line.rindex("12개월"), line.index("12개월")
        self.assertEqual(line, "이전 계약 | 12개월 | 현재 계약 | 12개월")
        piece = lambda r, a, b: {"extraction_id": "x", "spans": [  # noqa: E731
            {"element_id": "t", "rows": [r], "fragment": {"row": r, "start": a, "end": b}}]}
        base = {"element_id": "t", "quote": "12개월", "extraction_id": "x"}
        raw_at = el["raw_text"].index("12개월", el["raw_text"].index("현재"))
        for unit in ({**base, "cells": [[0, 3]]}, {**base, "offsets": [raw_at, raw_at + 4]}):
            self.assertEqual(evaluation.fragment_target(unit, el, 0), (approved, approved + 4))
            self.assertEqual(evaluation.grade(piece(0, approved, approved + 4), unit, el), 2)
            self.assertEqual(evaluation.grade(piece(0, earlier, earlier + 4), unit, el), 0)
            self.assertEqual(evaluation.grade(piece(0, approved, approved + 2), unit, el), 1)
            self.assertEqual(evaluation.coverage(piece(0, approved, approved + 2), unit, el),
                             frozenset(("f", 0, i) for i in range(approved, approved + 2)))
            group = {"group_id": "g1", "doc_id": "d", "alternatives": [unit]}
            row = {**gold_row([group]), "evidence_groups": [group]}
            m = evaluation.score_row(row, [piece(0, approved, len(line))], [piece(0, approved, len(line))],
                                     {("x", "t"): el})
            self.assertEqual((m["recall@20"], m["ndcg@5"], m["packed_complete"]), (1.0, 1.0, 1))
            self.assertEqual(evaluation.score_row(row, [piece(0, 0, approved - 1)], [], {("x", "t"): el})["recall@20"],
                             0.0)
        # offsets in a later row translate from the raw table text into that row's line
        later = el["raw_text"].index("12개월 이내")
        unit = {**base, "offsets": [later, later + 4]}
        self.assertEqual(evaluation.fragment_target(unit, el, 1), (dict(evaluation.table_rows(el))[1].index("12개월"),
                                                                   dict(evaluation.table_rows(el))[1].index("12개월") + 4))
        self.assertIsNone(evaluation.fragment_target(unit, el, 0))
        # without coordinates the first equal value stays the documented fallback
        self.assertEqual(evaluation.fragment_target(base, el, 0), (earlier, earlier + 4))
        # segments follow the fragment line: a spanned cell is carried and an adjacent repeat shares its segment
        spanned = {"table": {"cells": [{"row": 0, "col": 0, "text": "구분", "rowspan": 2},
                                       {"row": 0, "col": 1, "text": "6개월"}, {"row": 0, "col": 2, "text": "6개월"},
                                       {"row": 1, "col": 1, "text": "12개월"}]}}
        self.assertEqual(dict(evaluation.table_rows(spanned))[1], "구분 | 12개월")
        self.assertEqual(evaluation._line_segments(spanned, 1), {(0, 0): (0, 2), (1, 1): (5, 9)})
        self.assertEqual(evaluation._line_segments(spanned, 0)[(0, 2)], evaluation._line_segments(spanned, 0)[(0, 1)])

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
        self.assertIn("2024-06-11T00:00", evaluation.extract_dates("2024년 6월 11일 오전 12시"))
        self.assertIn("2024-06-11T12:00", evaluation.extract_dates("2024년 6월 11일 오후 12시"))
        self.assertNotIn("2024-06-11T12:00", evaluation.extract_dates("2024년 6월 11일 오전 12시"))

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
    def test_a_later_disagreement_blocks_an_ordinary_approval(self):
        self.submit('ordinary-approval', [p4.amount_row(self.env, reviewed=False)], 'dev')
        c = gold.candidate(self.s, 'dev-amount-r1')
        gold.decide(self.s, c['candidate_id'], 'approve', 'first-reviewer', c['row_sha256'],
                    original_inspected=True)
        self.assertTrue(evaluation.validate_gold_v2(self.s, 'dev')['ok'])
        gold.second_review(self.s, c['candidate_id'], 'independent-reviewer', True, 'Ordinary agreement')
        self.assertTrue(evaluation.validate_gold_v2(self.s, 'dev')['ok'])
        gold.second_review(self.s, c['candidate_id'], 'independent-reviewer', False,
                           'Original source contradicts the proposed qualifier')
        report = evaluation.validate_gold_v2(self.s, 'dev')
        self.assertFalse(report['ok'])
        self.assertTrue(any('second reviewer disagreed' in e for e in report['errors']))
        rows, skipped, _ = evaluation.load_eval_rows(self.s, 'dev')
        self.assertEqual(rows, [])
        self.assertEqual(skipped[0]['reason'], 'dispute_unresolved')
        with self.assertRaises(evaluation.EvaluationError):
            evaluation.freeze_dataset(self.s, 'dev', 'owner', 'must not freeze disputed evidence')
        gold.second_review(self.s, c['candidate_id'], 'later-reviewer', True,
                           'Later agreement without changing the rejected qualifier')
        self.assertFalse(evaluation.validate_gold_v2(self.s, 'dev')['ok'])
        self.assertEqual(evaluation.load_eval_rows(self.s, 'dev')[0], [])
        with self.assertRaises(evaluation.EvaluationError):
            evaluation.freeze_dataset(self.s, 'dev', 'owner', 'same revision remains disputed')

        corrected = p4.amount_row(self.env, reviewed=False)
        corrected['revision'] = 2
        corrected['difficulty_reason'] += '; corrected qualifier checked against the original'
        self.submit('corrected-approval', [corrected], 'dev')
        fixed = gold.candidate(self.s, 'dev-amount-r2')
        gold.decide(self.s, fixed['candidate_id'], 'approve', 'first-reviewer', fixed['row_sha256'],
                    original_inspected=True, disputed=True)
        gold.second_review(self.s, fixed['candidate_id'], 'independent-reviewer', True,
                           'Corrected revision agrees with the original')
        self.assertTrue(evaluation.validate_gold_v2(self.s, 'dev')['ok'])
        rows, skipped, _ = evaluation.load_eval_rows(self.s, 'dev')
        self.assertEqual([r['revision'] for r in rows], [2])
        self.assertEqual(skipped, [])
        self.assertEqual(evaluation.freeze_dataset(self.s, 'dev', 'owner', 'corrected revision')['rows'], 1)

    def test_api_generator_cannot_approve_or_second_review_its_draft(self):
        row = p4.amount_row(self.env, reviewed=False)
        row['generation_provenance'] = {'method': 'llm', 'model': 'gpt-6-luna', 'prompt_version': 'p',
                                        'source_set_hash': 'source-set'}
        self.submit('api-roles', [row], 'dev')
        c = gold.candidate(self.s, 'dev-amount-r1')
        with self.assertRaises(gold.GoldError):
            gold.decide(self.s, c['candidate_id'], 'approve', 'gpt-6-luna', c['row_sha256'], original_inspected=True)
        gold.decide(self.s, c['candidate_id'], 'approve', 'reviewer', c['row_sha256'],
                    original_inspected=True, disputed=True)
        with self.assertRaises(gold.GoldError):
            gold.second_review(self.s, c['candidate_id'], 'gpt-6-luna', True, 'Self agreement')
        rows = store.read_jsonl(evaluation.dataset_path(self.s, 'dev'))
        rows[0]['review']['second_review'] = {'reviewer': 'gpt-6-luna', 'agreed': True}
        with store.open_db(self.s.db_path) as conn:
            checker = evaluation.GoldChecker(self.s, conn)
            self.assertTrue(any('independent second review' in e for e in checker.check(rows[0], 'api')))
            scored, skipped = evaluation._gold_eval_rows(rows, 'dev',
                {s['doc_id']: (s['source_hash'], s['extraction_id']) for s in rows[0]['scope']})
        self.assertEqual(scored, [])
        self.assertEqual(skipped[0]['reason'], 'dispute_unresolved')

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
                service.gold_decide(res, v, "dev-amount-r1", "approve", c["row_sha256"], note="원문 확인")
            for cid in ("dev-amount-r1", "dev-deadline-r1", "dev-warranty-r1", "dev-absent-r1"):
                sha = service.gold_candidate(res, v, cid)["row_sha256"]
                service.gold_decide(res, v, cid, "approve", sha, note="원문 확인", original_inspected=True,
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

    def test_ai_reviewers_approve_sealed_rows_but_the_drafter_never_does(self):
        """Operating rule: AI reviewers approve development and sealed rows; only self-approval is refused."""
        row = p4.row(self.env, "test-ai", "도서관에서 무엇을 만드나요?", "기관D", split="test",
                     groups=[p4.group(self.env, "g1", "기관D", ("%좌석%", p4.SEATS))],
                     claims=[p4.claim("c1", ["g1"], {"type": "text", "patterns": ["좌석 예약"]})],
                     generation_provenance={"method": "llm", "model": "gpt-6-luna", "prompt_version": "p",
                                            "source_set_hash": "x"})
        self.submit("t-ai", [row], "test")
        c = gold.candidate(self.s, "test-ai-r1", include_sealed=True)
        for refused in ("agent-a", "gpt-6-luna"):  # the drafter and the drafting model
            with self.assertRaises(gold.GoldError):
                gold.decide(self.s, "test-ai-r1", "approve", refused, c["row_sha256"], original_inspected=True,
                            disputed=True, include_sealed=True)
        gold.decide(self.s, "test-ai-r1", "approve", "ai-review:codex", c["row_sha256"], original_inspected=True,
                    disputed=True, include_sealed=True)
        with self.assertRaises(gold.GoldError):  # the second reviewer differs from the first
            gold.second_review(self.s, "test-ai-r1", "ai-review:codex", True, "같은 검토자", include_sealed=True)
        gold.second_review(self.s, "test-ai-r1", "ai-review:claude", True, "원문 3쪽 좌석 예약 확인",
                           include_sealed=True)
        rows, skipped, _ = evaluation.load_eval_rows(self.s, "test", sealed=True)
        self.assertEqual(([evaluation.row_id(r) for r in rows], skipped), (["test-ai"], []))

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
        self.assertEqual(config["ranking_policy"], evaluation.GOLD_RANKING_POLICY)  # gold-2 ranking is deduplicated
        agg = scores["aggregate"]
        self.assertEqual(agg["passage_rows"], 4)
        self.assertEqual(agg["ndcg_eligible_rows"], 3)  # the comparison has no single ranking
        self.assertEqual(agg["multi_evidence"]["complete@20"]["denominator"], 2)
        traces = {t["id"]: t for t in store.read_jsonl(self.s.data_dir / "runs" / k1["run_id"] / "traces.jsonl")}
        self.assertTrue(traces["dev-compare"]["metrics"]["per_document"])
        self.assertEqual(traces["dev-compare"]["wrong_scope"], 0)
        self.assertIn("git_revision", scores["provenance"]["code"])
        # F9: one returned passage lies outside the labels
        pending = evaluation.pool_pending({"ndcg_pool": scores["aggregate"]["ndcg_pool"]})
        self.assertGreater(pending, 0)
        # Unlabelled top-5 passages are shown beside the row; the person activates without a recorded pool review.
        decision = {"run_id": k1["run_id"], "mode": config["mode"], "decided_by": "owner",
                    "finalist_run_id": k1["run_id"]}
        self.assertFalse(any("pool review" in e for e in evaluation.decision_errors(self.s, k1["run_id"], decision)))
        self.assertEqual(evaluation.pool_pending(evaluation.run_summary(self.s, k1["run_id"])), pending)
        with self.assertRaisesRegex(evaluation.EvaluationError, "sealed"):
            evaluation.evaluate_retrieval(self.s, fixtures.analyzer(), None, "test", ["K1"])


class AnswerRunTest(GoldRetrievalTest):
    def test_conflict_action_survives_execution_and_review_finalization(self):
        first = "Warranty duration is twelve months."
        second = "Warranty duration is twenty-four months."
        with mock.patch.object(fixtures, "PDF_A", fixtures.make_pdf([[first, second]])):
            env = p4.make_env(self.root / "conflict")
        row = p4.row(env, "conflict", "Warranty duration", key="기관A", qtype="revision_conflict",
                     groups=[p4.group(env, "g1", "기관A", ("%twelve%", first)),
                             p4.group(env, "g2", "기관A", ("%twenty-four%", second))],
                     answerability="conflicting", expected_status="conflicting_evidence")
        p4.write(env, "dev", [row])
        (run,) = evaluation.evaluate_retrieval(env.settings, fixtures.analyzer(), None, "dev", ["K1"])
        action = "Ask the purchaser which conflicting warranty condition controls."

        def respond(messages):
            body = json.loads(messages[-1]["content"])
            evidence = [next(e for e in body["evidence"] if text in e["text"]) for text in (first, second)]
            payload = {"status": "conflicting_evidence", "summary": "Competing warranty durations.",
                       "claims": [{"text": text, "kind": "source_fact", "doc_id": e["doc_id"],
                                   "evidence_ids": [e["evidence_id"]]} for text, e in zip((first, second), evidence)],
                       "missing_fields": [], "conflicts": [{"field": "warranty duration", "alternatives": [
                           {"doc_id": e["doc_id"], "value": text, "evidence_ids": [e["evidence_id"]]}
                           for text, e in zip((first, second), evidence)]}], "next_action": action}
            return generation.ProviderResponse(json.dumps(payload), None, "stop",
                                               {"prompt_tokens": 300, "completion_tokens": 90}, "fixture")

        res = service.Resources(env.settings, transport=FakeTransport(respond))
        try:
            estimate = answers.plan_run(env.settings, "answer-finalists", "dev", [run["run_id"]])
            out = answers.run_answers(env.settings, res, estimate["estimate_id"], "fixture-reviewer")
        finally:
            res.close()
        self.assertEqual(out["status"], "complete")
        record = next(iter(answers.load_progress(env.settings, out["run_id"]).values()))
        self.assertEqual(record["answer"]["next_action"], action)
        sheet = answers.export_review_sheet(env.settings, out["run_id"])
        verdicts = [{**i, "verdict": "supporting" if i["kind"] == "link" else "supported"}
                    for i in store.read_jsonl(Path(sheet["sheet"]))]
        path = self.root / "conflict-reviews.jsonl"
        store.write_jsonl_atomic(path, verdicts)
        answers.import_reviews(env.settings, out["run_id"], path, "fixture-reviewer")
        scores = json.loads((answers.run_dir(env.settings, out["run_id"]) / "scores.json").read_text(encoding="utf-8"))
        self.assertEqual(scores["finalists"][run["run_id"]]["negative_handling"]["numerator"], 1)

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

    def test_changed_package_requires_a_fresh_answer_run_and_estimate(self):
        with mock.patch.object(evaluation, 'code_fingerprint', return_value={'source_sha256': 'before'}):
            before = answers.plan_run(self.s, 'answer-finalists', 'dev', [self.k1])
            self.assertEqual(answers.plan_run(self.s, 'answer-finalists', 'dev', [self.k1])['run_id'],
                             before['run_id'])
        with mock.patch.object(evaluation, 'code_fingerprint', return_value={'source_sha256': 'after'}):
            after = answers.plan_run(self.s, 'answer-finalists', 'dev', [self.k1])
            self.assertNotEqual(after['run_id'], before['run_id'])
            with self.assertRaisesRegex(answers.AnswerEvalError, 'changed since the estimate'):
                answers.recheck(self.s, before)
            self.assertEqual(answers.recheck(self.s, after)['run_id'], after['run_id'])

    def assert_cost_matches_ledger(self, run_id: str, expect_partial: bool = False) -> None:
        summary = answers.finalize(self.s, run_id)
        self.assertEqual(summary["status"], "partial" if expect_partial else "complete")
        scores = json.loads((answers.run_dir(self.s, run_id) / "scores.json").read_text(encoding="utf-8"))
        reported = sum(v["cost"]["settled_micro_usd"] for v in scores["finalists"].values())
        earlier = sum(v["cost"]["settled_in_earlier_attempts_micro_usd"] for v in scores["finalists"].values())
        with store.open_db(self.s.db_path) as conn:
            actual = conn.execute("SELECT COALESCE(SUM(a.settled_micro_usd), 0) FROM attempts a JOIN requests r "
                                  "ON r.request_id = a.request_id WHERE a.state = 'settled' AND "
                                  "substr(r.idempotency_key, 1, ?) = ?", (len(run_id) + 1, run_id + ":")).fetchone()[0]
        self.assertEqual(reported, actual)
        self.assertGreater(earlier if not expect_partial else reported, 0)

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
        self.assertEqual(len(transport.calls), before)  # any unknown PostgreSQL billing blocks every dispatch
        # once its billing is settled from provider evidence, the row may run again as a recorded new attempt
        res = service.Resources(self.s, transport=transport)
        try:
            (attempt,) = [a for a in service.unresolved_attempts(res, auth.OWNER_CLI) if a["state"] == "unknown"]
            service.settle_from_evidence(res, auth.OWNER_CLI, attempt["attempt_id"],
                                         {"prompt_tokens": 100, "completion_tokens": 10}, None, "export", "reconciled")
        finally:
            res.close()
        # review round 3 (F5): before the retry the row is unfinished, yet its settled earlier attempt is reported
        self.assert_cost_matches_ledger(out["run_id"], expect_partial=True)
        _, final = self.answer_run(transport)
        self.assertEqual(final["status"], "complete")
        self.assert_cost_matches_ledger(out["run_id"])
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

    def test_a_question_subset_runs_only_its_rows_and_scores_each_row(self):
        # The PR #13 failures are rerun alone: the subset is part of the identity and the only rows planned or sent.
        whole = answers.plan_run(self.s, "answer-finalists", "dev", [self.k1], store=False)
        with self.assertRaisesRegex(answers.AnswerEvalError, "not reviewed dev rows: nope"):
            answers.plan_run(self.s, "answer-finalists", "dev", [self.k1], question_ids=["nope"])
        est = answers.plan_run(self.s, "answer-finalists", "dev", [self.k1], question_ids=["dev-compare"])
        self.assertEqual((est["rows"], est["question_ids"]), (1, ["dev-compare"]))
        self.assertNotEqual(est["run_id"], whole["run_id"])

        def cite_all(messages):
            by_doc = json.loads(messages[-1]["content"])["evidence_ids_by_doc"]
            return generation.ProviderResponse(json.dumps({
                "status": "answered", "summary": "s", "missing_fields": [], "conflicts": [], "next_action": None,
                "summary_evidence_ids": [ids[0] for ids in by_doc.values() if ids][:1],
                "claims": [{"text": "t", "kind": "source_fact", "doc_id": d, "evidence_ids": ids}
                           for d, ids in by_doc.items() if ids]}), None, "stop",
                {"prompt_tokens": 100, "completion_tokens": 10}, f"r-{len(transport.calls)}")

        transport = FakeTransport(cite_all)
        res = service.Resources(self.s, transport=transport, recover=True)
        try:
            out = answers.run_answers(self.s, res, est["estimate_id"], "tester")
        finally:
            res.close()
        self.assertEqual((len(transport.calls), out["status"]), (1, "complete"))
        passed = out["finalists"][self.k1]["rows_passed"]
        self.assertEqual((passed["numerator"], passed["denominator"]), (1, 1))
        # the same answer without the claim that cites a reached group fails: retrieval reached it, the answer didn't
        (row,) = [r for r in evaluation.load_eval_rows(self.s, "dev")[0] if r["question_id"] == "dev-compare"]
        record = answers.load_progress(self.s, out["run_id"])[(self.k1, "dev-compare")]
        config = json.loads((answers.run_dir(self.s, out["run_id"]) / "config.json").read_text(encoding="utf-8"))
        index = KeywordIndex.load(self.s, config["finalists"][0]["index_version"])
        scored = answers.score_record(row, record, index, {})
        self.assertEqual(scored["groups"], {"gold": 2, "retrieved": 2, "cited": 2})
        dropped = {**record, "answer": {**record["answer"], "claims": record["answer"]["claims"][1:]}}
        self.assertFalse(answers.score_record(row, dropped, index, {})["passed"])

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

    def frozen_release(self, transport) -> dict:
        _, dev = self.answer_run(transport, runs=[self.k1])
        evaluation.freeze_dataset(self.s, "dev", "owner", "dev frozen")
        evaluation.freeze_dataset(self.s, "test", "owner", "test frozen")
        freeze = self.freeze(dev["run_id"])
        self.freeze_id = freeze["freeze_id"]
        return freeze

    def test_frozen_family_map_and_review_history_are_enforced(self):
        """Review round 1 (F3): every digest the freeze recorded is checked at admission, not only the JSONL."""
        freeze = self.frozen_release(FakeTransport())
        fam_path = self.s.data_dir / "datasets" / "families.json"
        original = fam_path.read_bytes()
        fams = json.loads(original)
        dev_f = next(f for f in fams["families"].values() if f["split"] == "dev")
        test_f = next(f for f in fams["families"].values() if f["split"] == "test")
        dev_f["related_sources"] = [test_f["source_hash"]]
        fam_path.write_text(json.dumps(fams), encoding="utf-8")
        self.assertTrue(evaluation.validate_gold_v2(self.s, "dev")["errors"])
        self.assertEqual(evaluation.frozen_dataset(self.s, "dev")["changed"], ["family map"])
        self.assertTrue(sealed.freeze_problems(self.s, freeze))
        with self.assertRaisesRegex(answers.AnswerEvalError, "freeze no longer holds"):
            answers.plan_run(self.s, "sealed", freeze_id=freeze["freeze_id"])
        fam_path.write_bytes(original)
        self.assertEqual(sealed.freeze_problems(self.s, freeze), [])
        log = evaluation.review_log_path(self.s, "test")
        log.write_text(log.read_text(encoding="utf-8") + '{"review_id": "forged"}\n', encoding="utf-8")
        self.assertIn("review log file", evaluation.frozen_dataset(self.s, "test")["changed"])
        evaluation.export_review_log(self.s, "test")
        with store.open_db(self.s.db_path) as conn:  # a decision recorded after the freeze
            conn.execute("INSERT INTO gold_candidates(candidate_id, batch_id, batch_sha256, dataset, row_json, "
                         "row_sha256, question_key, drafted_by, submitted_at) VALUES ('late-r1', 'b', 'x', 'dev', "
                         "'{}', 'x', 'k', 'a', '2026-10-01')")
            conn.execute("INSERT INTO gold_reviews(review_id, candidate_id, reviewer, kind, decision, created_at) "
                         "VALUES ('r1', 'late-r1', 'p', 'decision', 'approve', '2026-10-01')")
        self.assertIn("review history in the database", evaluation.frozen_dataset(self.s, "dev")["changed"])
        self.assertTrue(any("dev set changed" in p for p in sealed.freeze_problems(self.s, freeze)))

    def test_an_interrupted_sealed_run_is_already_an_exposure(self):
        """Review round 1 (F4): a partial sealed run exposes the test set; a retuned candidate cannot run it again
        as an untouched test, while the original run may resume unchanged."""
        second = p4.row(self.env, "test-seat-2", "열람 공간 예약 시스템은 무엇을 하는 사업인가요?", "기관D",
                        split="test", groups=[p4.group(self.env, "g1", "기관D", ("%좌석%", p4.SEATS))],
                        claims=[p4.claim("c1", ["g1"], {"type": "text", "patterns": ["도서관"]})])
        p4.write(self.env, "test", store.read_jsonl(evaluation.dataset_path(self.s, "test")) + [second])
        freeze = self.frozen_release(FakeTransport())
        calls = {"n": 0}

        def flaky(messages):
            calls["n"] += 1
            if calls["n"] > 1:
                return ProviderError("APITimeoutError (injected)", pre_execution=False)
            return generation._echo_first_evidence(messages)

        first = self.sealed_run(FakeTransport(flaky))
        self.assertEqual(first["status"], "partial")
        self.assertEqual(sum(r["status"] == "done" for r in answers.load_progress(self.s, first["run_id"]).values()), 1)
        res = service.Resources(self.s, transport=FakeTransport())  # nothing dispatches while billing is unknown
        try:
            for attempt in service.unresolved_attempts(res, auth.OWNER_CLI):
                service.settle_from_evidence(res, auth.OWNER_CLI, attempt["attempt_id"],
                                             {"prompt_tokens": 100, "completion_tokens": 10}, None, "export", "reconciled")
        finally:
            res.close()
        with mock.patch.object(generation, "PROMPT_VERSION", "grounded-answer-tuned"):
            self.assertTrue(sealed.freeze_problems(self.s, freeze))  # the tuned candidate breaks the old freeze
            _, dev = self.answer_run(FakeTransport(), runs=[self.k1])
            tuned = self.freeze(dev["run_id"])
            self.assertTrue(tuned["post_test"])
            self.assertEqual([e["status"] for e in tuned["earlier_sealed_runs"]], ["partial"])
            self.freeze_id = tuned["freeze_id"]
            with self.assertRaisesRegex(sealed.SealedError, "already exposed"):
                self.sealed_run(FakeTransport())
            post = self.sealed_run(FakeTransport(), post_test=True, reason="tuned prompt after an interrupted run")
            config = json.loads((answers.run_dir(self.s, post["run_id"]) / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["label"], "post-test regression")
        self.freeze_id = freeze["freeze_id"]  # the untouched run resumes under its own, unchanged freeze
        resumed = self.sealed_run(FakeTransport())
        self.assertEqual(resumed["run_id"], first["run_id"])

    def test_selection_evidence_must_be_the_current_candidates(self):
        with mock.patch.object(generation, "PROMPT_VERSION", "grounded-answer-other"):
            _, dev = self.answer_run(FakeTransport(), runs=[self.k1])
        evaluation.freeze_dataset(self.s, "dev", "owner", "dev frozen")
        evaluation.freeze_dataset(self.s, "test", "owner", "test frozen")
        with self.assertRaisesRegex(sealed.SealedError, "prompt_version"):
            self.freeze(dev["run_id"])

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
    """The 검증 page's evaluation section and the 데이터셋 review form, over the HTTP API the web screens call."""

    def setUp(self):
        super().setUp()
        from tests import fake_hub

        self.transport = FakeTransport()
        self.res = service.Resources(self.s, transport=self.transport, recover=True)
        self.client = fake_hub.client(self.res, "person-b")
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.res.close()
        super().tearDown()

    def test_trace_selector_loads_only_approved_gold_with_both_document_scopes(self):
        compare = self.compare_row()
        pending = p4.amount_row(self.env, reviewed=False)
        p4.write(self.env, "dev", [compare, pending])
        r = self.client.get("/api/verify/trace-sources")
        self.assertEqual(r.status_code, 200, r.text)
        questions = r.json()["questions"]
        self.assertEqual([q["question_id"] for q in questions], [compare["question_id"]])
        self.assertEqual(len(questions[0]["scope"]), 2)
        self.assertEqual(len(self.transport.calls), 0)

    def test_the_tab_estimates_then_runs_a_development_evaluation_with_consent(self):
        import time

        p4.write(self.env, "dev", self.dev_rows())
        evaluation.validate_gold(self.s, "dev")
        (k1,) = evaluation.evaluate_retrieval(self.s, fixtures.analyzer(), None, "dev", ["K1"])
        decision = self.root / "decision.json"
        decision.write_text(json.dumps({"run_id": k1["run_id"], "mode": "kiwi_bm25", "decided_by": "owner",
                                        "rationale": "baseline"}), encoding="utf-8")
        evaluation.activate_run(self.s, k1["run_id"], decision)
        est = self.client.post("/api/verify/evaluation/plan").json()  # free: prices only
        self.assertTrue(est["fits"], est)
        self.assertEqual(len(self.transport.calls), 0)
        r = self.client.post("/api/verify/evaluation/start", json={"estimate_id": est["estimate_id"]})
        self.assertEqual(r.status_code, 200, r.text)
        run_id = r.json()["run_id"]
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and service._EVAL_JOBS[run_id].is_alive():
            time.sleep(0.1)
        scores = json.loads((answers.run_dir(self.s, run_id) / "scores.json").read_text(encoding="utf-8"))
        self.assertEqual(scores["status"], "complete")
        with self.assertRaises(service.ServiceError):  # the sealed path is not a verifier action
            service.start_answer_evaluation(self.res, auth.visitor("x"), "missing")

    def test_a_gold_row_needs_the_inspection_attestation(self):
        rows = [p4.deadline_row(self.env)]
        rows[0]["review"].update(reviewed_by=None, approved_at=None, status="pending", original_inspected=False)
        path = self.root / "b.jsonl"
        store.write_jsonl_atomic(path, rows)
        gold.submit(self.s, path, "b1", "dev", "agent-a")
        self.assertEqual([p["candidate_id"] for p in self.client.get("/api/gold/pending").json()], ["dev-deadline-r1"])
        card = self.client.get("/api/gold/dev-deadline-r1").json()
        self.assertTrue(card["required_claims"])
        decide = lambda **kw: self.client.post("/api/gold/dev-deadline-r1/decide", json={  # noqa: E731
            "decision": "approve", "expected_sha": card["row_sha256"], **kw})
        r = decide(note="")
        self.assertEqual(r.status_code, 400)
        self.assertIn("메모", r.text)  # every decision carries a note
        r = decide(note="원문 4쪽 마감 일시 확인")
        self.assertEqual(r.status_code, 400)
        self.assertIn("원문을 직접 확인", r.text)
        self.assertEqual(gold.candidate(self.s, "dev-deadline-r1")["status"], "pending")
        r = decide(note="원문 4쪽 마감 일시 확인", original_inspected=True)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(gold.candidate(self.s, "dev-deadline-r1")["status"], "approved")


if __name__ == "__main__":
    unittest.main()
