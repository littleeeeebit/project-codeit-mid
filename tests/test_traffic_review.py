"""Traffic review: the read-only window read, pseudonyms at the run folder's exit, the explained sample, and the run
in the review app beside gold runs (notes, sample additions, taxonomy sources)."""

from __future__ import annotations

import contextlib
import gzip
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rfp_assistant.evaluation import error_analysis as ea
from rfp_assistant.evaluation import similar_cases as sc
from rfp_assistant.evaluation import traffic_review as tr
from rfp_assistant.evaluation.stage_review import ReviewError
from tests.test_error_analysis import make_run
from tests.test_stage_review import server

MEMBERS = ("spai1303", "teammate07")
DOCS = {"D1": "가 사업", "D2": "나 사업"}


def request(member: str, question: str, mode: str = "single", docs=("D1",), status: str = "completed",
            result: dict | None = None, trace: dict | None = None, key: str | None = None) -> dict:
    key = key or str(uuid.uuid4())
    scope = [{"doc_id": d, "source_hash": "h"} for d in docs]
    result = result or {"status": "answered", "summary": f"{question}의 답", "claims": [
        {"text": "보증금은 10%다", "kind": "source_fact", "doc_id": docs[0] if docs else "D1", "evidence_ids": ["E1"]}],
        "evidence": {"E1": {"doc_id": docs[0] if docs else "D1", "quote": "보증금 10%",
                            "location": {"section_path": ["제1장", "계약"]}}}, "missing_fields": [], "conflicts": []}
    return {"request_id": str(uuid.uuid4()), "member_id": member, "idempotency_key": key, "generation_id": key,
            "status": status, "mode": mode, "scope_json": json.dumps(scope),
            "request_json": json.dumps({"question": question, "scope": scope, "mode": mode, "idempotency_key": key,
                                        "verifier_run_id": "", "capabilities": ["consultant"]}, ensure_ascii=False),
            "result_json": json.dumps(result, ensure_ascii=False),
            "trace_json": json.dumps(trace or {"billing": [{"settled_micro_usd": 700}]}, ensure_ascii=False),
            "created_at": f"2026-10-0{len(question) % 9 + 1}T01:00:00+00:00"}


def window() -> list[dict]:
    refused = {"status": "technical_error", "summary": "모델 응답을 검증하지 못했습니다.",
               "error": "claim_without_evidence: claim 0", "evidence": {}}
    raw = json.dumps({"status": "answered", "summary": "거부된 요약", "claims": [
        {"text": "근거 없는 주장", "kind": "source_fact", "doc_id": "D1", "evidence_ids": []}],
        "missing_fields": [], "conflicts": [], "next_action": None}, ensure_ascii=False)
    rows = [request(MEMBERS[i % 2], f"질문 {i}", mode="corpus", docs=()) for i in range(6)]
    rows += [request(MEMBERS[0], "두 문서 비교", mode="compare", docs=("D1", "D2")),
             request(MEMBERS[1], "검증에 걸린 질문", status="failed", result=refused, trace={"raw_output": raw}),
             # member IDs planted where pseudonymisation must reach: the question, a trace value and a trace key
             request(MEMBERS[0], f"{MEMBERS[1]}님이 물은 것과 같나요", trace={"note": f"by {MEMBERS[0]}",
                                                                          MEMBERS[1]: {"x": 1}}),
             # not 질문하기 traffic: an evaluation request and a verifier generation never enter the window
             request(MEMBERS[0], "평가 질문", key="A-run:main:q1:1"),
             request(MEMBERS[0], "검증 생성", key="vgen-R1")]
    return rows


class FakeConnection:
    """psycopg's connection as read_window uses it; records the read-only settings it was opened with."""

    def __init__(self, rows: list[dict], options: str):
        self.rows, self.options, self.read_only = rows, options, False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @contextlib.contextmanager
    def transaction(self):
        assert self.read_only and "default_transaction_read_only=on" in self.options
        yield

    def execute(self, sql: str, params=()):
        if sql.startswith("SELECT request_id"):
            since = params[1]
            return mock.Mock(fetchall=lambda: [r for r in self.rows if r["created_at"] >= since])
        if "DISTINCT member_id" in sql:
            return iter([{"member_id": m} for m in MEMBERS])
        return iter([{"doc_id": d, "normalized_metadata_json": json.dumps({"title": t})} for d, t in DOCS.items()])


def run_traffic(root: Path, rows: list[dict], sample: int = 4) -> tuple[dict, list]:
    opened = []

    def connect(dsn, **kw):
        opened.append(kw)
        return FakeConnection(rows, kw["options"])
    with mock.patch("psycopg.connect", connect):
        record = tr.run(SimpleNamespace(data_dir=root), "2026-10-01", sample, "CODEIT_DATABASE_DSN", "postgresql://x")
    return record, opened


def folder_bytes(folder: Path) -> bytes:
    """Every file of the run folder, gzip members decompressed."""
    return b"".join(gzip.decompress(p.read_bytes()) if p.suffix == ".gz" else p.read_bytes()
                    for p in sorted(folder.rglob("*")) if p.is_file())


class TrafficRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def view(self, run_id: str) -> dict:
        return server.read_view(self.root, run_id)

    def test_no_member_id_reaches_the_run_folder(self):
        record, opened = run_traffic(self.root, window())
        self.assertEqual(opened[0]["options"], "-c default_transaction_read_only=on")
        folder = server.runs_dir(self.root) / record["run_id"]
        data = folder_bytes(folder)
        for m in MEMBERS:
            self.assertNotIn(m.encode(), data)
        self.assertNotIn(b"postgresql://", data)  # the DSN itself is never recorded, only its variable's name
        frozen = [json.loads(line) for line in gzip.decompress(
            (folder / "inputs" / "requests.jsonl.gz").read_bytes()).decode().splitlines()]
        key = tr.pseudonym_key(SimpleNamespace(data_dir=self.root))
        planted = next(f for f in frozen if f["request"]["question"].endswith("물은 것과 같나요"))
        self.assertEqual(planted["member"], tr.pseudonym(key, MEMBERS[0]))
        self.assertTrue(planted["request"]["question"].startswith(tr.pseudonym(key, MEMBERS[1])))
        self.assertIn(tr.pseudonym(key, MEMBERS[1]), planted["trace"])  # the key too
        self.assertEqual([p.name for p in folder.rglob("*.key")], [])  # the key stays outside the run folder

    def test_pseudonyms_are_stable_across_runs_and_differ_per_member(self):
        first, _ = run_traffic(self.root, window())
        second, _ = run_traffic(self.root, window())
        members = [{q["member"] for q in self.view(r["run_id"])["questions"]} for r in (first, second)]
        self.assertEqual(members[0], members[1])
        self.assertEqual(len(members[0]), 2)

    def test_the_window_keeps_only_asked_requests_and_all_of_them(self):
        record, _ = run_traffic(self.root, window())
        view = self.view(record["run_id"])
        questions = {q["question"] for q in view["questions"]}
        self.assertEqual(len(view["questions"]), 9)
        self.assertNotIn("평가 질문", questions)
        self.assertNotIn("검증 생성", questions)
        self.assertEqual(sum(len(g["requests"]) for g in view["groups"]), 9)  # the map holds the whole window

    def test_refused_answer_is_shown_from_the_trace(self):
        record, _ = run_traffic(self.root, window())
        q = next(q for q in self.view(record["run_id"])["questions"] if q["question"] == "검증에 걸린 질문")
        a = q["candidates"]["traffic"]
        self.assertEqual((a["validation"], a["summary"], a["rejected_shown"]), ("claim_without_evidence", "거부된 요약", True))
        self.assertEqual(a["claims"][0]["text"], "근거 없는 주장")
        cut = tr.answer({"status": "technical_error", "error": "schema_invalid: x"},
                        {"raw_output": '{"summary": "잘린 요약", "claims": [{"text": "반쯤'}, {})
        self.assertEqual((cut["summary"], cut["rejected_cut"]), ("잘린 요약", True))
        failed = tr.answer({"status": "technical_error", "error": "InternalServerError: 503"}, {}, {})
        self.assertEqual((failed["validation"], failed["failure"]), (None, "InternalServerError: 503"))

    def test_exit_gate_refuses_a_surviving_member_id(self):
        with self.assertRaisesRegex(ReviewError, "member ID survived"):
            tr.write_out(self.root / "x.json", {"who": MEMBERS[0]}, list(MEMBERS))
        self.assertFalse((self.root / "x.json").exists())

    def test_traffic_answers_take_notes_and_hand_added_samples_like_gold(self):
        record, _ = run_traffic(self.root, window())
        run_id = record["run_id"]
        view = self.view(run_id)
        self.assertEqual(server.list_runs(self.root)[0]["window"]["requests"], 9)
        q = view["questions"][0]
        text = q["candidates"]["traffic"]["claims"][0]["text"]
        state = server.add_annotation(self.root, view, {"op": "note", "question": q["key"], "candidate": "traffic",
                                                        "field": "claim.0", "start": 0, "end": 3, "text": text[:3],
                                                        "note": "근거가 다른 조항"}, "person")
        outside = next(x["key"] for x in view["questions"] if x["key"] not in {s["key"] for s in view["sample"]})
        state = server.add_annotation(self.root, view, {"op": "sample", "question": outside, "value": True,
                                                        "reason": "지도에서"}, "person")
        self.assertEqual([s["question"] for s in state["sampled"]], [outside])
        with self.assertRaisesRegex(server.ReviewInputError, "traffic run"):
            server.add_annotation(self.root, view, {"op": "sample", "question": "nope", "value": True}, "person")
        with self.assertRaisesRegex(server.ReviewInputError, "no candidates"):
            server.write_decision(self.root, view, None, {}, {})
        # the same taxonomy and counts as a gold run, labelled by source
        gold = make_run(self.root)
        server.add_annotation(self.root, gold, {"op": "reviewed", "question": "q1", "candidate": "c1", "value": True},
                              "person")
        sources = {n["source"] for n in ea.live_notes(self.root).values()}
        self.assertEqual(sources, {"traffic"})
        self.assertEqual(ea.progress(self.root)["reviewed_by_source"], {"gold": 1, "traffic": 1})
        self.assertIn(run_id, {a["run_id"] for a in sc._stored_answers(self.root)})
        exported = server.export_run(self.root, run_id)
        with tempfile.TemporaryDirectory() as other:
            self.assertEqual(server.import_run(Path(other), exported), run_id)
            self.assertEqual(len(ea.annotation_state(ea.read_annotations(Path(other), run_id))["sampled"]), 1)


def items(groups: dict[str, int]) -> list[dict]:
    out = []
    for name, size in groups.items():
        mode, outcome, validation = name.split("/")
        g = tr.group_of(mode, outcome, None if validation == "none" else validation, None, ["D1"])
        out += [{"key": f"{name}#{i}", "group": g} for i in range(size)]
    return out


class DrawTest(unittest.TestCase):
    def test_failures_first_then_non_answers_then_size(self):
        groups, picks = tr.draw(items({"single/answered/none": 9, "single/insufficient_evidence/none": 2,
                                       "single/technical_error/claim_without_evidence": 1}), 2, seed=7)
        self.assertEqual([g["outcome"] for g in groups], ["technical_error", "insufficient_evidence", "answered"])
        self.assertEqual([p["kind"] for p in picks], ["representative", "representative"])
        self.assertEqual({p["group"] for p in picks}, {groups[0]["key"], groups[1]["key"]})

    def test_extra_picks_follow_group_size_and_reasons_explain_them(self):
        groups, picks = tr.draw(items({"corpus/answered/none": 30, "single/answered/none": 3}), 8, seed=1)
        per = {g["key"]: sum(p["group"] == g["key"] for p in picks) for g in groups}
        self.assertEqual(list(per.values()), [7, 1])
        self.assertEqual(len({p["key"] for p in picks}), 8)
        reason = tr.reason(picks[-1], 1)
        self.assertIn("큰 묶음 추가 표본", reason)
        self.assertIn("30건", reason)
        self.assertIn("시드 1", tr.reason(picks[0], 1))
        self.assertEqual(tr.draw(items({"corpus/answered/none": 30}), 8, seed=1)[1],
                         tr.draw(items({"corpus/answered/none": 30}), 8, seed=1)[1])  # a seed redraws the same

    def test_a_sample_larger_than_the_window_takes_everything_once(self):
        _, picks = tr.draw(items({"single/answered/none": 2, "compare/answered/none": 1}), 10, seed=3)
        self.assertEqual(len(picks), 3)


if __name__ == "__main__":
    unittest.main()
