"""The judge golden set: every mutation type, the deterministic difference check and the per-type rates."""

import unittest

from rfp_assistant import judge_set

PASSAGE = ("- 하자보수 기간은 검수 완료 후 12개월 이상으로 한다.\n- 하자보수 비용 300만원은 부가세 포함 금액이며 사업자가 부담하여야 함\n"
           "- 제안사는 하자보수 계획서를 사전에 제출하여야 함")
CLAIM = ("하자보수 기간은 검수 완료 후 12개월 이상입니다. 하자보수 비용 300만원은 부가세 포함 금액입니다. "
         "제안사는 하자보수 계획서를 사전에 제출해야 합니다.")


def link(blind_id="aaaa00000001", question_id="q1", claim=CLAIM, passages=(PASSAGE,)):
    return {"blind_id": blind_id, "kind": "link", "allowed": ["supporting", "unsupported"], "question_id": question_id,
            "question": "하자보수 조건은?", "reference": "supporting", "claim": claim, "passages": list(passages)}


DONOR = link("bbbb00000002", "q2", "교육은 착수 후 1개월 안에 실시합니다.", ("- 사용자 교육은 착수 후 1개월 이내 실시",))


def claim_item():
    return {"blind_id": "cccc00000003", "kind": "claim", "question_id": "q3", "question": "하자보수 비용은?",
            "allowed": ["correct", "wrong_value", "incomplete_qualifier", "missing"], "reference": "correct",
            "required": "300만원", "conditions": ["부가세 포함", "검수 완료 후 / 검수 후"], "passages": [PASSAGE],
            "answer_summary": "하자보수 비용은 부가세 포함 300만원이며 검수 완료 후 12개월 동안 부담합니다.",
            "answer_claims": ["하자보수 비용 300만원(부가세 포함)은 사업자가 부담합니다."], "deterministic": "needs_review"}


class MutationTest(unittest.TestCase):
    def test_every_support_mutation_is_unsupported_and_really_differs(self):
        source = link()
        got = {m["mutation"]["type"]: m for m in judge_set.mutants(source, [source, DONOR])}
        self.assertEqual(set(got), {"amount", "unit", "qualifier", "date", "negation", "wrong_evidence"})
        for kind, m in got.items():
            with self.subTest(kind=kind):
                self.assertIsNone(judge_set.differs(source, m))
                self.assertEqual((m["reference"], m["source_id"]), ("unsupported", source["blind_id"]))
                self.assertEqual(m["blind_id"], f"{source['blind_id']}-{kind}")
        self.assertIn("4,000,000원", got["amount"]["claim"])
        self.assertIn("12년 이상", got["unit"]["claim"])
        self.assertIn("15개월 이상", got["date"]["claim"])
        self.assertEqual((got["qualifier"]["mutation"]["from"], got["qualifier"]["mutation"]["to"]),
                         ("부가세 포함", "부가세 별도"))
        self.assertIn("제출하지 않아도 됩니다", got["negation"]["claim"])
        self.assertEqual(got["wrong_evidence"]["passages"], DONOR["passages"])
        self.assertEqual(got["wrong_evidence"]["claim"], CLAIM)

    def test_a_comparison_word_flips_only_after_a_number(self):
        self.assertIsNone(judge_set.mutate_qualifier("이상 징후를 알립니다.", "이상 징후 발생 시 통보"))
        out, info = judge_set.mutate_qualifier("인계기간은 15일 이상입니다.", "인수인계 기간 15일 이상")
        self.assertEqual((out, info["to"]), ("인계기간은 15일 이하입니다.", "이하"))

    def test_the_check_rejects_a_mutant_that_does_not_differ(self):
        source = link()
        same = {**source, "blind_id": "x-amount", "mutation": {"type": "amount", "from": "3000000", "to": "4000000"}}
        self.assertEqual(judge_set.differs(source, same), "text unchanged")
        stated = {**same, "claim": CLAIM.replace("300만원", "400만원"),
                  "passages": [PASSAGE + " 추가 비용 400만원"]}
        self.assertEqual(judge_set.differs(source, stated), "the passages also state the new amount")
        no_neg = {**source, "claim": CLAIM.replace("입니다.", "임."), "mutation": {"type": "negation"}}
        self.assertEqual(judge_set.differs(source, no_neg), "no negation was added")

    def test_required_fact_mutations_change_the_answer_only(self):
        source = claim_item()
        got = {m["mutation"]["type"]: m for m in judge_set.mutants(source, [source])}
        self.assertEqual({k: m["reference"] for k, m in got.items()},
                         {"amount": "wrong_value", "unit": "wrong_value", "qualifier": "incomplete_qualifier",
                          "dropped_condition": "incomplete_qualifier"})
        self.assertIn("300만달러", got["unit"]["answer_summary"])
        for m in got.values():
            self.assertIsNone(judge_set.differs(source, m))
            self.assertEqual((m["required"], m["passages"], m["deterministic"]), (source["required"], [PASSAGE], None))
        self.assertNotIn("부가세 포함", judge_set.target_text(got["dropped_condition"]))
        self.assertNotIn("300만", judge_set.target_text(got["amount"]))

    def test_a_condition_the_answer_restates_in_other_words_is_never_dropped(self):
        # review round 1, F4: removing "부가세 포함" leaves the answer complete when it also says 부가가치세 포함
        source = claim_item()
        source["answer_claims"] = [*source["answer_claims"], "해당 300만원에는 부가가치세가 포함됩니다."]
        got = {m["mutation"]["type"]: m for m in judge_set.mutants(source, [source])}
        self.assertNotEqual(got["dropped_condition"]["mutation"]["from"], "부가세 포함")
        self.assertIsNone(judge_set.differs(source, got["dropped_condition"]))
        removed = {**source, "answer_summary": source["answer_summary"].replace("부가세 포함 ", ""),
                   "answer_claims": [c.replace("(부가세 포함)", "") for c in source["answer_claims"]],
                   "blind_id": "x-dropped_condition", "mutation": {"type": "dropped_condition", "from": "부가세 포함"}}
        self.assertEqual(judge_set.differs(source, removed), "the condition is still stated in other words")

    def test_the_set_pairs_each_mutated_source_with_its_unmutated_positive(self):
        unusable = link("dddd00000004", "q4", "담당자를 지정합니다.", ("- 사업 담당자를 지정",))
        items = [link(), DONOR, unusable, {**link("eeee00000005"), "reference": "unsupported"}]
        rows, stats = judge_set.build(items, [i["blind_id"] for i in items])
        sources = {r["blind_id"] for r in rows if r["mutation"] is None}
        self.assertIn("aaaa00000001", sources)
        self.assertNotIn("eeee00000005", sources)  # only approved positives are mutated
        self.assertEqual(stats["positives"], len(sources))
        self.assertTrue(all(r["source_id"] in sources for r in rows))
        self.assertEqual(stats["negatives"], sum(stats["by_type"].values()))

    def test_rates_count_the_judges_own_passes_per_type(self):
        rows = [{"type": "amount", "judged": "supported"}, {"type": "amount", "judged": "unsupported"},
                {"type": "amount", "judged": None, "code": True}, {"type": None, "judged": "supported"}]
        rates = judge_set.mutation_rates(rows)
        self.assertEqual({k: (v["items"], v["judged"], v["passed"], v["rate"], v["code_settled"])
                          for k, v in rates.items()},
                         {"amount": (3, 2, 1, 0.5, 1), "unmutated": (1, 1, 1, 1.0, 0)})
        self.assertEqual(list(rates)[-1], "unmutated")


if __name__ == "__main__":
    unittest.main()
