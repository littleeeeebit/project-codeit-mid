"""The Korean bridge's checks, the threshold fit and the pre-declared replacement rule (judges.py)."""

import unittest

from rfp_assistant import judges

ORGS = ["국방과학연구소"]


class BridgeTest(unittest.TestCase):
    def test_placeholders_are_restored(self):
        masked, values = judges.protect("국방과학연구소는 2024. 6. 11. 17:00까지 SFR-001 산출물 1억 3천만원과 10%를 제출", ORGS)
        self.assertNotRegex(judges.PLACEHOLDER.sub("", masked), r"\d|국방과학연구소|SFR")
        self.assertEqual(values, [judges.org_label("국방과학연구소"), "2024-06-11 17:00", "SFR-001", "130,000,000", "10%"])
        english = "[[V0]] must submit the SFR deliverable [[V2]], KRW [[V3]] and [[V4]] by [[V1]]"
        self.assertEqual(judges.restore(english, values),
                         f"{values[0]} must submit the SFR deliverable SFR-001, KRW 130,000,000 and 10% by 2024-06-11 17:00")

    def test_residual_hangul_is_rejected(self):
        masked, values = judges.protect("하자보수 기간은 12개월", ORGS)
        with self.assertRaisesRegex(judges.Untranslatable, "residual_hangul"):
            judges.restore("The 하자보수 period is [[V0]] months", values)

    def test_changed_number_is_rejected(self):
        _, values = judges.protect("계약기간은 12개월", ORGS)
        with self.assertRaisesRegex(judges.Untranslatable, "changed_number"):
            judges.restore("The contract period is [[V0]] months, about 1 year", values)
        with self.assertRaisesRegex(judges.Untranslatable, "changed_protected_value"):
            judges.restore("The contract period is twelve months", values)
        with self.assertRaisesRegex(judges.Untranslatable, "changed_protected_value"):
            judges.restore("The contract period is [[V0]] or [[V0]] months", values)


class RuleTest(unittest.TestCase):
    def arm(self, judged, kappa, false_accepts, coverage):
        return {"judged": judged, "kappa": kappa, "kappa_exact": kappa, "false_accepts": false_accepts,
                "coverage": coverage}

    def test_replacement_rule(self):
        luna = self.arm(370, 0.60, 3, 1.0)
        self.assertEqual(judges.replacement_verdict(luna, self.arm(350, 0.56, 3, 0.93))["verdict"], "replaceable")
        no = judges.replacement_verdict(luna, self.arm(350, 0.54, 2, 0.95))
        self.assertEqual((no["verdict"], [c["condition"] for c in no["deciding"]]), ("not_replaceable", ["kappa"]))
        no = judges.replacement_verdict(luna, self.arm(330, 0.70, 4, 0.89))
        self.assertEqual([c["condition"] for c in no["deciding"]], ["false_accepts", "coverage"])
        few = judges.replacement_verdict(luna, self.arm(99, 0.9, 0, 1.0))
        self.assertEqual((few["verdict"], few["deciding"][0]["condition"]), ("inconclusive", "min_judged"))

    def test_band_fit_keeps_coverage_and_separates(self):
        points = [(0.95, True)] * 40 + [(0.6, True)] * 3 + [(0.55, False)] * 2 + [(0.1, False)] * 5
        band = judges.fit_band(points)
        self.assertGreaterEqual(band["coverage"], 0.9)
        self.assertEqual(band["false_accepts"], 0)
        self.assertGreater(band["kappa"], 0.7)

    def test_uncertain_and_failed_jev_abstain(self):
        item = {"kind": "link"}
        band = {"lo": 0.3, "hi": 0.7}
        self.assertIsNone(judges.jev_label(item, {"status": "done", "answers": {"support": 0.5}}, band))
        self.assertIsNone(judges.jev_label(item, {"status": "abstained", "abstain": "api_failure: timeout"}, band))
        self.assertEqual(judges.jev_label(item, {"status": "done", "answers": {"support": 0.2}}, band), "unsupported")


if __name__ == "__main__":
    unittest.main()
