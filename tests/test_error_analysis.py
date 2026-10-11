"""Failure types from the person's answer notes: export, proposal validation, decisions and counts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from rfp_assistant.evaluation import error_analysis as ea
from tests.test_stage_review import server

REPO = Path(__file__).resolve().parents[1]
RUN = "generation-20261011T000000Z-abc123"
ANSWER = "보증금은 계약금액의 10%이며 30일 안에 낸다"


def make_run(root: Path, questions: int = 4) -> dict:
    """A generation run with `questions` questions answered by a baseline and one candidate."""
    cands = [{"id": cid, "label": cid, "ref": cid, "role": role, "commit": c * 40, "status": "complete"}
             for cid, role, c in (("base", "baseline", "a"), ("c1", "candidate", "b"))]
    view = {"schema": server.RUN_SCHEMA, "run_id": RUN, "stage": "generation", "candidates": cands,
            "questions": [{"key": f"q{i}", "question": f"질문 {i}", "candidates": {
                "base": {"summary": ANSWER, "cost_micro_usd": 0}, "c1": {"summary": ANSWER, "cost_micro_usd": 0}}}
                for i in range(1, questions + 1)]}
    folder = root / "review" / "runs" / RUN
    folder.mkdir(parents=True)
    for name in ("run.json", "view.json"):
        (folder / name).write_text(json.dumps({**view, "created_at": "2026-10-11T00:00:00+00:00"}, ensure_ascii=False),
                                   encoding="utf-8")
    return view


def note(root: Path, view: dict, q: str, c: str, text: str, what: str) -> str:
    start = ANSWER.index(text)
    state = server.add_annotation(root, view, {"op": "note", "question": q, "candidate": c, "field": "summary",
                                               "start": start, "end": start + len(text), "text": text, "note": what},
                                  "person")
    return next(n["id"] for n in state["notes"] if n["question"] == q and n["candidate"] == c and n["text"] == text)


def proposal(*types) -> dict:
    return {"schema": ea.PROPOSAL_SCHEMA, "bundle": "export-x",
            "types": [{"name": n, "definition": d, "notes": ids} for n, d, ids in types]}


class ProposeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.view = make_run(self.root)
        self.a = note(self.root, self.view, "q1", "base", "10%", "비율이 아니라 금액이어야 함")
        self.b = note(self.root, self.view, "q2", "c1", "30일", "기한이 원문과 다름")

    def tearDown(self):
        self.tmp.cleanup()

    def refused(self, body, why):
        with self.assertRaisesRegex(ea.ReviewInputError, why):
            ea.propose(self.root, body)
        self.assertFalse((ea.taxonomy_dir(self.root) / "proposals").exists())

    def test_each_type_needs_a_korean_name_one_sentence_and_a_human_note(self):
        ok = "답변이 원문과 다른 숫자를 적는다."
        self.refused(proposal(("Wrong number", ok, [self.a])), "Korean")
        self.refused(proposal(("숫자\n오류", ok, [self.a])), "Korean")
        self.refused(proposal(("숫자 오류", "", [self.a])), "one-sentence")
        self.refused(proposal(("숫자 오류", "숫자가 틀린다. 단위도 틀린다.", [self.a])), "one sentence")
        self.refused(proposal(("숫자 오류", "숫자가 틀린다\n그리고 단위도", [self.a])), "one sentence")
        # a type no note stands on is the agent's own taste, not the person's
        self.refused(proposal(("숫자 오류", ok, [])), "at least one of the person's notes")
        self.refused(proposal(("숫자 오류", ok, ["0" * 32])), "does not exist")
        self.refused(proposal(("숫자 오류", ok, [self.a]), ("기한 오류", "기한이 틀린다.", [self.a])), "in both")
        self.refused(proposal(("숫자 오류", ok, [self.a]), ("숫자 오류", ok, [self.b])), "already exists")
        self.refused({**proposal(("숫자 오류", ok, [self.a])), "extra": 1}, "unknown keys")
        self.refused({"schema": ea.PROPOSAL_SCHEMA, "types": [{"name": "숫자 오류", "definition": ok,
                                                             "notes": [self.a], "examples": ["지어낸 사례"]}]},
                     "only name, definition and notes")
        self.refused({"types": []}, "not a")

        server.add_annotation(self.root, self.view, {"op": "delete", "target": self.b}, "person")
        self.refused(proposal(("기한 오류", "기한이 틀린다.", [self.b])), "deleted")

        out = ea.propose(self.root, proposal(("숫자 오류", "답변이 원문과 다른 숫자(3.5%)를 적는다.", [self.a])))
        self.assertEqual((out["types"], out["notes"]), (1, 1))
        # a note already proposed is not proposed again
        with self.assertRaisesRegex(ea.ReviewInputError, "already belongs to 숫자 오류"):
            ea.propose(self.root, proposal(("다른 유형", "다르다.", [self.a])))

    def test_export_writes_only_notes_in_no_type_and_a_prompt_pointing_at_the_bundle(self):
        out = ea.export(self.root)
        bundle = Path(out["bundle"])
        rows = [json.loads(line) for line in (bundle / "notes.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual({r["id"] for r in rows}, {self.a, self.b})
        self.assertEqual({r["question_text"] for r in rows}, {"질문 1", "질문 2"})
        self.assertIn(str(bundle / "prompt.md"), out["prompt"])
        self.assertIn("error-analysis propose --file", (bundle / "prompt.md").read_text(encoding="utf-8"))

        ea.propose(self.root, proposal(("숫자 오류", "답변이 원문과 다른 숫자를 적는다.", [self.a])))
        rows = (Path(ea.export(self.root)["bundle"]) / "notes.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(r)["id"] for r in rows], [self.b])
        ea.propose(self.root, proposal(("기한 오류", "답변이 원문과 다른 기한을 적는다.", [self.b])))
        with self.assertRaisesRegex(ea.ReviewInputError, "no notes left"):
            ea.export(self.root)

    def test_cli_exports_and_proposes_without_a_database(self):
        env = {k: v for k, v in os.environ.items() if k != "RFP_DATABASE_DSN"}
        env.update(RFP_DATA_DIR=str(self.root), PYTHONPATH=os.pathsep.join([str(REPO / "src"), str(REPO)]),
                   PYTHONIOENCODING="utf-8")

        def run(*argv):
            done = subprocess.run([sys.executable, "-B", "-m", "rfp_assistant.cli", "error-analysis", *argv], env=env,
                                  cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=120)
            return done.returncode, done.stdout

        code, out = run("export")
        self.assertEqual(code, 0)
        bundle = Path(json.loads(out[:out.rindex("}") + 1])["bundle"])
        (bundle / "proposal.json").write_text(json.dumps(proposal(
            ("숫자 오류", "답변이 원문과 다른 숫자를 적는다.", [self.a]), ("기한 오류", "답변이 기한을 틀린다.", [self.b])),
            ensure_ascii=False), encoding="utf-8")
        code, out = run("propose", "--file", str(bundle / "proposal.json"))
        self.assertEqual((code, json.loads(out)["types"]), (0, 2))
        self.assertEqual(run("propose", "--file", str(bundle / "proposal.json"))[0], 1)  # notes already proposed


class DecisionTest(unittest.TestCase):
    def test_accept_rename_merge_split_reject_are_versioned_and_counted_over_reviewed_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            view = make_run(root)
            n1 = note(root, view, "q1", "base", "10%", "비율이 아니라 금액")
            n2 = note(root, view, "q2", "base", "10%", "보증금 비율 틀림")
            n3 = note(root, view, "q3", "c1", "30일", "기한 틀림")
            n4 = note(root, view, "q3", "c1", "보증금은", "주어가 다른 항목")
            for q, c in (("q4", "base"), ("q4", "c1")):
                server.add_annotation(root, view, {"op": "reviewed", "question": q, "candidate": c, "value": True}, "p")
            ea.propose(root, proposal(("금액 오류", "금액을 틀린다.", [n1, n2]), ("기한 오류", "기한을 틀린다.", [n3]),
                                      ("주어 오류", "다른 항목을 말한다.", [n4])))
            refs = [t["ref"] for t in ea.taxonomy_state(root)["pending"]]

            state = ea.decide(root, {"op": "accept", "ref": refs[0], "name": "보증금 비율 오류"}, "person")
            self.assertEqual((state["version"], [t["name"] for t in state["accepted"]]), (1, ["보증금 비율 오류"]))
            money = state["accepted"][0]
            self.assertEqual([n["text"] for n in money["notes"]], ["10%", "10%"])  # the quoted spans behind it
            with self.assertRaisesRegex(ea.ReviewInputError, "already decided"):
                ea.decide(root, {"op": "accept", "ref": refs[0]}, "person")
            state = ea.decide(root, {"op": "reject", "ref": refs[2], "reason": "한 번뿐"}, "person")
            self.assertEqual((state["version"], state["unassigned"]), (1, 1))  # rejecting a proposal keeps the version
            state = ea.decide(root, {"op": "merge", "refs": [money["id"], refs[1]], "name": "값 오류",
                                     "definition": "금액이나 기한 같은 값을 원문과 다르게 적는다."}, "person")
            merged = state["accepted"][0]
            self.assertEqual((state["version"], len(state["accepted"]), merged["answers"]), (2, 1, 3))
            with self.assertRaisesRegex(ea.ReviewInputError, "every note"):
                ea.decide(root, {"op": "split", "ref": merged["id"], "parts": [
                    {"name": "금액 오류", "definition": "금액을 틀린다.", "notes": [n1]},
                    {"name": "기한 오류", "definition": "기한을 틀린다.", "notes": [n3]}]}, "person")
            state = ea.decide(root, {"op": "split", "ref": merged["id"], "parts": [
                {"name": "금액 오류", "definition": "금액을 틀린다.", "notes": [n1, n2]},
                {"name": "기한 오류", "definition": "기한을 틀린다.", "notes": [n3]}]}, "person")
            self.assertEqual(sorted(t["name"] for t in state["accepted"]), ["금액 오류", "기한 오류"])
            deadline = next(t for t in state["accepted"] if t["name"] == "기한 오류")
            state = ea.decide(root, {"op": "rename", "ref": deadline["id"], "name": "납부 기한 오류",
                                     "definition": "납부 기한을 원문과 다르게 적는다."}, "person")
            self.assertEqual(state["version"], 4)
            versions = sorted(p.name for p in (ea.taxonomy_dir(root) / "versions").iterdir())
            self.assertEqual(versions, ["v1.json", "v2.json", "v3.json", "v4.json"])
            self.assertEqual(len(ea.read_jsonl(ea.taxonomy_dir(root) / "history.jsonl")), 5)  # nothing rewritten

            # reviewed answers: 3 noted + 2 marked fine; n4's answer is q3/c1, already counted with n3
            progress = ea.progress(root, last=2)
            self.assertEqual((progress["reviewed"], progress["breadth"]), (5, 2))
            self.assertEqual([(t["name"], t["answers"], t["thin"]) for t in progress["types"]],
                             [("금액 오류", 2, True), ("납부 기한 오류", 1, True)])
            # q4's two fine answers were reviewed last: no type first appeared in them
            self.assertEqual(progress["new_in_last"], 0)
            self.assertEqual(ea.progress(root, last=3)["new_in_last"], 1)

            counts = ea.run_counts(root, RUN)
            self.assertEqual(counts["reviewed"], {"base": 3, "c1": 2})
            self.assertEqual({t["name"]: t["by_candidate"] for t in counts["types"]},
                             {"금액 오류": {"base": 2}, "납부 기한 오류": {"c1": 1}})

            summary = ea.release_summary(root)
            self.assertEqual((summary["sealed_metric"], summary["gate"]), (False, False))
            lines = "\n".join(ea.release_lines(summary))
            self.assertIn("## Human error analysis (not a sealed metric)", lines)
            self.assertIn(f"| 금액 오류 | 금액을 틀린다. | 2/5 (fewer than 3 cases) | `{RUN}` 2/5 |", lines)

            # a deleted note no longer counts, and a type whose last note is gone can only be rejected
            server.add_annotation(root, view, {"op": "delete", "target": n3}, "person")
            state = ea.taxonomy_state(root)
            gone = next(t for t in state["accepted"] if t["name"] == "납부 기한 오류")
            self.assertEqual((gone["answers"], gone["deleted"]), (0, 1))
            with self.assertRaisesRegex(ea.ReviewInputError, "no live note"):
                ea.decide(root, {"op": "rename", "ref": gone["id"], "name": "기한", "definition": "기한."}, "person")


if __name__ == "__main__":
    unittest.main()
