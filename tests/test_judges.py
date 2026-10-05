"""The Korean bridge's checks, the threshold fit and the pre-declared replacement rule (judges.py)."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rfp_assistant import judges, service

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


def fake_reference(data_dir: Path, n: int = 400) -> SimpleNamespace:
    """A minimal installed reference: link items, half supported, in the layout `load_reference` checks."""
    items = [{"blind_id": f"B{i:04d}", "kind": "link", "claim": "c", "passages": ["p"],
              "reference": "supporting" if i % 2 else "unsupported"} for i in range(n)]
    body = "".join(json.dumps(i, ensure_ascii=False, sort_keys=True) + "\n" for i in items)
    out = data_dir / "judges" / "reference"
    out.mkdir(parents=True)
    (out / "reference.jsonl").write_text(body, encoding="utf-8", newline="")
    manifest = {"run_id": "A-test", "items": n, "reference_sha256": judges._sha(body), "labels": {}}
    (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return SimpleNamespace(data_dir=data_dir)


class SplitIntegrityTest(unittest.TestCase):
    """Review round 1, F1: a persisted split is used only when it is still the one that was recorded."""

    def test_an_edited_split_is_refused_even_with_its_hash_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = fake_reference(Path(tmp))
            split = judges.make_split(settings)
            self.assertEqual(judges.load_split(settings), split)
            path = Path(tmp) / "judges" / "split.json"
            moved = dict(split, calibration=sorted([*split["calibration"], split["held_out"][0]]),
                         held_out=split["held_out"][1:])
            path.write_text(json.dumps(moved), encoding="utf-8")
            with self.assertRaisesRegex(judges.JudgeError, "recorded hash"):
                judges.load_split(settings)
            with self.assertRaisesRegex(judges.JudgeError, "recorded hash"):
                judges.make_split(settings)
            body = {k: v for k, v in moved.items() if k != "split_sha256"}  # re-hashed: still not the seeded draw
            path.write_text(json.dumps({**body, "split_sha256": judges._sha(judges.dumps(body))}), encoding="utf-8")
            with self.assertRaisesRegex(judges.JudgeError, "seeded split"):
                judges.load_split(settings)

    def test_an_edited_bridge_cache_record_is_a_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            masked = "계약기간은 [[V0]]"
            path = judges._cache_path(settings, masked)
            path.parent.mkdir(parents=True)
            record = {"masked": masked, "text": "The contract period is [[V0]]", "model": judges.MODEL,
                      "version": judges.BRIDGE_VERSION, "glossary_sha256": judges.glossary_sha()}
            path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(judges.cached_translation(settings, masked), record)
            path.write_text(json.dumps({**record, "masked": "다른 원문 [[V0]]"}, ensure_ascii=False), encoding="utf-8")
            self.assertIsNone(judges.cached_translation(settings, masked))


class JudgeSetCarryTest(unittest.TestCase):
    """Review round 1, F4 follow-through: a regenerated set reuses only judgements of byte-identical items."""

    def test_only_unchanged_items_of_the_same_configuration_are_carried(self):
        from rfp_assistant import judge_set

        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(data_dir=Path(tmp))
            old = [{"blind_id": "a-amount", "claim": "300만원"}, {"blind_id": "b-dropped_condition", "claim": "x"},
                   {"blind_id": "c", "claim": "y"}]
            new = [old[0], {"blind_id": "b-dropped_condition", "claim": "changed"}]
            body = "".join(json.dumps(i, ensure_ascii=False, sort_keys=True) + "\n" for i in old)
            old_sha = judges._sha(body)
            sets = judge_set.root(settings) / "sets"
            sets.mkdir(parents=True)
            (sets / f"{old_sha}.jsonl").write_bytes(body.encode("utf-8"))
            base = {"part": "judge_set", "model": judges.MODEL}

            def prior(run_id, config):
                d = judges.run_dir(settings, run_id)
                d.mkdir(parents=True)
                (d / "config.json").write_text(json.dumps({"run_id": run_id, **config}), encoding="utf-8")
                for i in old:
                    judges._append(settings, run_id, {"arm": "luna", "blind_id": i["blind_id"], "status": "done",
                                                      "label": "unsupported"})

            prior("J-judge_set-000000000001", {**base, "judge_set_sha256": old_sha})
            prior("J-judge_set-000000000002", {**base, "model": "other", "judge_set_sha256": old_sha})
            inputs = {"part": "judge_set", "run_id": "J-judge_set-0000000000ff", "items": new,
                      "config": {**base, "judge_set_sha256": "f" * 64}}
            self.assertEqual(judges.carry(settings, inputs), 1)
            got = judges.load_judgements(settings, inputs["run_id"])
            self.assertEqual(list(got), [("luna", "a-amount")])
            self.assertEqual(got[("luna", "a-amount")]["carried_from"], "J-judge_set-000000000001")
            self.assertEqual(judges.carry(settings, inputs), 0)  # idempotent
            (sets / f"{old_sha}.jsonl").write_bytes(body.replace("300", "400").encode("utf-8"))
            # an archived set whose bytes changed proves nothing about the items it judged
            self.assertEqual(judges.carry(settings, {**inputs, "run_id": "J-judge_set-0000000000ee"}), 0)


class CalibrationCoverageTest(unittest.TestCase):
    """Review round 1, F2: failed and untranslatable Jev items count against calibration coverage."""

    def test_coverage_counts_every_eligible_item(self):
        points = [(0.95, True)] * 50 + [(0.05, False)] * 50
        self.assertEqual(judges.fit_band(points)["coverage"], 1.0)
        band = judges.fit_band(points, total=200)  # 100 more were asked but failed or abstained
        self.assertEqual((band["coverage"], band["points"], band["meets_floor"]), (0.5, 100, False))
        self.assertFalse(judges.fit_band([], total=10)["meets_floor"])
        with self.assertRaises(ValueError):
            judges.fit_band(points, total=99)

    def test_fit_thresholds_divides_by_all_eligible_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = fake_reference(Path(tmp), 40)
            items, _ = judges.load_reference(settings)
            run_id = "J-calibration-000000000000"
            d = judges.run_dir(settings, run_id)
            d.mkdir(parents=True)
            records = [{"arm": "jev_bridged", "blind_id": i["blind_id"], "status": "done",
                        "answers": {"support": 0.9 if i["reference"] == "supporting" else 0.1}} for i in items[:30]]
            records += [{"arm": "jev_bridged", "blind_id": i["blind_id"], "status": "abstained",
                         "abstain": "api_failure: timeout"} for i in items[30:]]
            (d / "judgements.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
            band = judges.fit_thresholds(settings, run_id, items, set())["bands"]["jev_bridged"]["support"]
            self.assertEqual((band["n"], band["points"], band["coverage"], band["meets_floor"]), (40, 30, 0.75, False))


class ShutdownTest(unittest.TestCase):
    """Review round 1, F3: close() joins a running judge job before it closes the transport the job uses.
    Review round 2, F4: a start publishes its run before it returns, so progress polling can see it."""

    verifier = SimpleNamespace(member_id="v1")

    def resources(self, settings, events):
        res = object.__new__(service.Resources)
        res.settings = settings
        res.transport = SimpleNamespace(close=lambda: events.append("transport closed"))
        res.provider_note, res._runner, res._lock, res.tracing = None, None, None, None
        res._runner_lock, res._jobs, res._closed = threading.Lock(), [], False
        res._database_lifecycle = SimpleNamespace(__exit__=lambda *a: events.append("database released"))
        return res

    def test_close_joins_the_judge_thread_before_closing_the_transport(self):
        events = []
        res = self.resources(SimpleNamespace(shutdown_wait_seconds=10), events)

        def run(settings, transport, estimate_id, member, closing, begun):
            while not closing():
                time.sleep(0.01)
            time.sleep(0.2)  # the in-flight call still finishes on the transport
            events.append("judge job finished")

        begun = {"run_id": "J-held_out-000000000000"}
        with mock.patch.object(service, "_authorize", lambda res, p, role: p), \
                mock.patch.object(judges, "begin", lambda s, e, a: begun), mock.patch.object(judges, "run", run):
            service.start_judges(res, self.verifier, "E-1")
            res.close()
            self.assertEqual(events, ["judge job finished", "transport closed", "database released"])
            with self.assertRaisesRegex(service.ServiceError, "종료"):  # no new job once closing began
                service.start_judges(res, self.verifier, "E-2")

    def test_a_started_judge_run_is_listed_as_running_before_its_worker_proceeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = fake_reference(Path(tmp))
            settings.shutdown_wait_seconds, settings.generation_reasoning_effort = 10, "low"
            settings.jev_model = "jev-test"
            judges.make_split(settings)
            res = self.resources(settings, [])
            release, seen = threading.Event(), []

            def run(*a, begun, **kw):  # the worker is held before anything it does on its own
                seen.append(begun["run_id"])
                release.wait(10)

            with mock.patch.object(service, "_authorize", lambda res, p, role: p), \
                    mock.patch.object(judges, "load_estimate", lambda s, e: {"part": "calibration", "max_micro_usd": 1}), \
                    mock.patch.object(judges, "recheck", lambda s, e: None), \
                    mock.patch.object(judges, "organisations", lambda s: []), mock.patch.object(judges, "run", run):
                try:
                    run_id = service.start_judges(res, self.verifier, "E-1")
                    runs = {r["run_id"]: r for r in judges.overview(settings, service._judge_running())["runs"]}
                finally:
                    release.set()
                    res.close()
            self.assertEqual((runs[run_id]["running"], runs[run_id]["status"], runs[run_id]["progress"]["luna"]),
                             (True, "running", {"done": 0, "total": 200}))
            self.assertEqual(seen, [run_id])

    def test_a_started_answer_evaluation_is_published_before_its_worker_proceeds(self):
        from rfp_assistant import answers

        with tempfile.TemporaryDirectory() as tmp:
            res = self.resources(SimpleNamespace(shutdown_wait_seconds=10, data_dir=Path(tmp)), [])
            release, seen = threading.Event(), []

            def begin(settings, estimate_id, actor):
                d = answers.run_dir(settings, "A-000000000000")
                d.mkdir(parents=True)
                (d / "config.json").write_text("{}", encoding="utf-8")
                return {"estimate_id": estimate_id, "run_id": "A-000000000000"}

            def run(*a, begun, **kw):
                seen.append(begun["run_id"])
                release.wait(10)

            with mock.patch.object(service, "_authorize", lambda res, p, role: p), \
                    mock.patch.object(answers, "load_estimate", lambda s, e: {"action": "answer-finalists"}), \
                    mock.patch.object(answers, "begin_answers", begin), mock.patch.object(answers, "run_answers", run):
                try:
                    run_id = service.start_answer_evaluation(res, self.verifier, "E-1")
                    published = (answers.run_dir(res.settings, run_id) / "config.json").exists()
                    running = service._EVAL_JOBS[run_id].is_alive()
                finally:
                    release.set()
                    res.close()
            self.assertEqual((published, running, seen), (True, True, [run_id]))


if __name__ == "__main__":
    unittest.main()
