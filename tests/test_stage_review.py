"""The stage review contract: the run folder the runner writes and the review app reads, and the decision files."""

import gzip
import importlib.util
import io
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

from rfp_assistant.corpus import ingestion
from rfp_assistant.evaluation import stage_review
from rfp_assistant.evaluation import stage_review_paid as paid
from rfp_assistant.retrieval import chunking

SERVER = Path(__file__).resolve().parents[1] / "review" / "server.py"
_spec = importlib.util.spec_from_file_location("review_server", SERVER)
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)

DOC = """<HwpDoc><BodyText><SectionDef><ColumnSet>
<Paragraph><LineSeg><Text>□ 조직별 역할</Text></LineSeg></Paragraph>
<Paragraph><LineSeg><TableControl><TableBody rows="3" cols="2">
 <TableRow><TableCell row="0" col="0"><Paragraph><LineSeg><Text>조직</Text></LineSeg></Paragraph></TableCell>
  <TableCell row="0" col="1"><Paragraph><LineSeg><Text>역할</Text></LineSeg></Paragraph></TableCell></TableRow>
 <TableRow><TableCell row="1" col="0"><Paragraph><LineSeg><Text>PMO</Text></LineSeg></Paragraph></TableCell>
  <TableCell row="1" col="1"><Paragraph><LineSeg><Text>일정 관리</Text></LineSeg></Paragraph></TableCell></TableRow>
 <TableRow><TableCell row="2" col="0"><Paragraph><LineSeg><Text>개발팀</Text></LineSeg></Paragraph></TableCell>
  <TableCell row="2" col="1"><Paragraph><LineSeg><Text>구현과 시험</Text></LineSeg></Paragraph></TableCell></TableRow>
</TableBody></TableControl></LineSeg></Paragraph>
</ColumnSet></SectionDef></BodyText></HwpDoc>"""


def make_run(root: Path) -> tuple[str, dict]:
    """A chunking run as the runner writes it: a kept, a title-losing and a refused candidate. The macOS
    launcher workflow imports it, since real runs carry corpus text that must not leave the machine."""
    elements = ingestion.finalize_elements(ingestion.walk_hwp(ET.fromstring(DOC)), "x1")
    kept = chunking.build_chunks(elements, "x1")[0]
    lost = [{**c, "payload": c["payload"].replace("□ 조직별 역할", "")} for c in kept]
    run_id = "chunking-20261010T000000Z-abc123"
    folder = root / "review" / "runs" / run_id
    (folder / "inputs").mkdir(parents=True)
    (folder / "candidates").mkdir()
    with gzip.open(folder / "inputs" / "documents.json.gz", "wt", encoding="utf-8") as f:
        json.dump({"profile": "structural", "documents": [
            {"extraction_id": "x1", "doc_id": "d1", "title": "사업", "elements": elements}]}, f)
    for cid, chunks in (("base", kept), ("c1", lost)):
        (folder / "candidates" / f"{cid}.json").write_text(json.dumps({"status": "complete", "documents": [
            {"extraction_id": "x1", "chunks": chunks}]}, ensure_ascii=False), encoding="utf-8")
    cand = {"ref": "r", "commit": "a" * 40, "working_tree": False, "status": "complete", "host": {"cuda": False}}
    record = {"schema": stage_review.SCHEMA, "run_id": run_id, "stage": "chunking",
              "created_at": "2026-10-10T00:00:00+00:00", "candidates": [
                  {**cand, "id": "base", "label": "main", "role": "baseline"},
                  {**cand, "id": "c1", "label": "lost", "role": "candidate"},
                  {**cand, "id": "c2", "label": "gpu", "role": "candidate", "status": "refused",
                   "reason": "needs a CUDA GPU"}]}
    view = stage_review.score_chunking(folder, record)
    (folder / "view.json").write_text(json.dumps(view, ensure_ascii=False), encoding="utf-8")
    (folder / "run.json").write_text(json.dumps(record), encoding="utf-8")
    return run_id, view


class StageReviewContractTest(unittest.TestCase):
    def test_run_folder_and_decision_files(self):
        self.assertEqual(stage_review.SCHEMA, server.RUN_SCHEMA)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id, view = make_run(root)
            folder = root / "review" / "runs" / run_id

            # What the app reads: every candidate listed (the refused one with its reason, never ranked), the
            # title check, aligned boundaries per document.
            self.assertEqual([c["status"] for c in view["candidates"]], ["complete", "complete", "refused"])
            self.assertEqual(view["candidates"][2]["reason"], "needs a CUDA GPU")
            self.assertEqual(set(view["summary"]), {"base", "c1"})
            self.assertEqual([(s["tables_kept"], s["tables_titled"]) for s in view["summary"].values()], [(1, 1), (0, 1)])
            doc = json.loads((folder / "docs" / "0.json").read_text(encoding="utf-8"))
            self.assertTrue(all(r["differs"] for r in doc["rows"]))
            self.assertTrue(all(cell["title_lost"] for r in doc["rows"] for cid, cell in r["cells"].items() if cid == "c1"))
            self.assertEqual(server.list_runs(root)[0]["candidates"][2]["status"], "refused")

            # An exported run imports into another checkout, once.
            other = Path(tmp) / "other"
            self.assertEqual(server.import_run(other, server.export_run(root, run_id)), run_id)
            self.assertIsNotNone(server.list_runs(other)[0]["imported"])
            with self.assertRaises(server.ReviewInputError):
                server.import_run(other, server.export_run(root, run_id))
            # A view naming another run would send this run's decision to that run's files.
            exported = zipfile.ZipFile(io.BytesIO(server.export_run(root, run_id)))
            tampered = io.BytesIO()
            with zipfile.ZipFile(tampered, "w") as z:
                for name in exported.namelist():
                    data = exported.read(name)
                    if name.endswith("/view.json"):
                        data = json.dumps({**json.loads(data), "run_id": "chunking-20261010T000000Z-bbbbbb"}).encode()
                    z.writestr(name, data)
            with self.assertRaisesRegex(server.ReviewInputError, "view.json is not"):
                server.import_run(Path(tmp) / "third", tampered.getvalue())

            # A candidate that drops a titled table entirely loses its title; the table still counts.
            dropped = json.loads((folder / "candidates" / "c1.json").read_text(encoding="utf-8"))
            for d in dropped["documents"]:
                d["chunks"] = [c for c in d["chunks"] if not any("rows" in s for s in c["spans"])]
            (folder / "candidates" / "c1.json").write_text(json.dumps(dropped, ensure_ascii=False), encoding="utf-8")
            rescored = stage_review.score_chunking(folder, json.loads((folder / "run.json").read_text()))
            self.assertEqual((rescored["summary"]["c1"]["tables_kept"], rescored["summary"]["c1"]["tables_titled"]), (0, 1))

            with self.assertRaises(server.ReviewInputError):
                server.write_decision(root, view, "c2", {}, {})  # a refused candidate cannot be chosen
            with self.assertRaises(server.ReviewInputError):
                server.write_decision(root, view, None, {}, {})  # rejecting c1 needs a reason
            saved = server.write_decision(root, view, None, {"c1": "loses the table title"}, {"0": "check row 2"})
            record = json.loads(Path(saved["json"]).read_text(encoding="utf-8"))
            self.assertEqual(Path(saved["json"]).name, f"{run_id}.json")
            self.assertEqual((record["schema"], record["chosen"]), ("review-decision-1", None))
            self.assertEqual([(c["outcome"], c["reason"]) for c in record["candidates"]],
                             [("baseline", None), ("rejected", "loses the table title"), ("failed", "needs a CUDA GPU")])
            self.assertEqual(record["notes"], [{"item": "0", "label": "사업", "note": "check row 2"}])
            markdown = Path(saved["markdown"]).read_text(encoding="utf-8")
            for line in ("- Outcome: no candidate accepted; the baseline stays", "loses the table title",
                         "table titles kept 0/1", "### `0` 사업", "check row 2", "Merge no candidate."):
                self.assertIn(line, markdown)
            self.assertIn(saved["markdown"], saved["prompt"])

    def test_gold_rank_beyond_shown_passages_and_bad_variant_sections(self):
        # The first fully correct chunk at rank 15 of the 20 scored is rank 15, not "outside the top 20".
        chunks = [{"chunk_id": f"k{i}", "extraction_id": "x", "payload": "p"} for i in range(20)]
        index = SimpleNamespace(chunks=chunks, row_of={c["chunk_id"]: i for i, c in enumerate(chunks)}, elements={})
        answer = {"sides": [{"ranking": [c["chunk_id"] for c in chunks], "packed": []}]}
        with mock.patch.object(stage_review.ev, "score_row", return_value={}),                 mock.patch.object(stage_review.ev, "group_grade", lambda c, g, e: 2 if c["chunk_id"] == "k14" else 0):
            scored = stage_review._score_question(index, {}, {}, [{}], {"side_docs": [None]}, answer)
        self.assertEqual((scored["gold_rank"], len(scored["sides"][0])), (15, stage_review.TOP_K))

        # A variant section of the wrong type fails that candidate with its reason instead of crashing the run.
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ('{"chunking": 1}', '{"retriever": {"limits": 5}}', '{"generation": {"max_output_tokens": "9"}}'):
                (Path(tmp) / stage_review.VARIANT_FILE).write_text(bad, encoding="utf-8")
                with self.assertRaises(stage_review.ReviewError):
                    stage_review.read_variant(Path(tmp))


def paid_folder(root: Path, stage: str, record: dict) -> Path:
    run_id = f"{stage}-20261010T000000Z-abc123"
    folder = root / "review" / "runs" / run_id
    (folder / "inputs").mkdir(parents=True)
    (folder / "candidates").mkdir()
    cand = {"ref": "r", "commit": "a" * 40, "working_tree": False, "status": "complete", "host": {"cuda": True}}
    record.update(schema=stage_review.SCHEMA, run_id=run_id, stage=stage, created_at="2026-10-10T00:00:00+00:00",
                  candidates=[{**cand, "id": "base", "label": "main", "role": "baseline"},
                              {**cand, "id": "c1", "label": "minimal", "role": "candidate"}])
    (folder / "run.json").write_text(json.dumps(record), encoding="utf-8")
    return folder


def write_outputs(folder: Path, outputs: dict) -> None:
    for cid, out in outputs.items():
        (folder / "candidates" / f"{cid}.json").write_text(json.dumps({"status": "complete", **out}, ensure_ascii=False),
                                                           encoding="utf-8")


class PaidStageTest(unittest.TestCase):
    def test_a_paid_run_never_starts_without_an_approved_estimate(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = paid_folder(Path(tmp), "generation", {"state": "needs_approval"})
            (folder / "worker.py").write_text("", encoding="utf-8")
            estimate = {"estimate_id": "e1", "run_id": folder.name, "stage": "generation", "fingerprint": "f",
                        "expires_at": "2999-01-01T00:00:00+00:00", "approved_by": None, "candidates": {}}
            (folder / "estimate.json").write_text(json.dumps(estimate), encoding="utf-8")
            with mock.patch.object(stage_review.subprocess, "run") as launched, \
                    mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
                with self.assertRaisesRegex(stage_review.ReviewError, "not approved"):
                    stage_review.resume(SimpleNamespace(data_dir=Path(tmp)), folder.name)
                with self.assertRaisesRegex(stage_review.ReviewError, "not approved"):
                    stage_review.execute(SimpleNamespace(data_dir=Path(tmp), source_dir=Path(tmp)),
                                         folder / "worker.py", Path(tmp), "generation", folder / "inputs",
                                         folder / "c.json", folder / "o.json", "paid", estimate)
            launched.assert_not_called()

    def test_a_candidate_failing_midway_leaves_the_run_resumable(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = paid_folder(Path(tmp), "generation", {"state": "needs_approval"})
            write_outputs(folder, {"base": {"rows": []}})
            estimate = {"candidates": {"base": {}, "c1": {}}}
            settings = SimpleNamespace(data_dir=Path(tmp))
            ran = []

            def execute(*args):
                ran.append(args[6].name)
                if len(ran) == 1:
                    raise RuntimeError("DiskFull: No space left on device")
                return {"status": "complete"}

            with mock.patch.object(paid, "load_estimate", return_value=estimate), \
                    mock.patch.object(paid, "require_approved"), mock.patch.object(paid, "recheck"), \
                    mock.patch.object(paid, "paid_config", return_value={}), \
                    mock.patch.object(paid, "ledger_spend"), mock.patch.object(paid, "score_generation", return_value={}), \
                    mock.patch.object(stage_review, "resolve", return_value={"commit": "a" * 40}), \
                    mock.patch.object(stage_review, "checkout"), mock.patch.object(stage_review, "remove_worktree"), \
                    mock.patch.object(stage_review, "execute", side_effect=execute):
                first = stage_review.resume(settings, folder.name)
                self.assertEqual((first["state"], first["candidates"][1]["status"]), ("stopped", "failed"))
                second = stage_review.resume(settings, folder.name)
            self.assertEqual(second["state"], "complete")
            self.assertEqual(ran, ["c1.json", "c1.json"])  # the finished baseline is never paid again

    def test_generation_view_and_decision(self):
        claim = {"claim_id": "c1", "criticality": "critical", "match": {"type": "text", "patterns": ["보안확약서"]},
                 "qualifiers": [], "support_groups": ["g1"]}
        rows = [{"question_id": q, "question": f"질문 {q}", "question_type": "fact", "answerability": "answerable",
                 "expected_status": "answered", "mode": "single", "scope": [{"doc_id": "d1", "source_hash": "h"}],
                 "required_claims": [claim], "evidence_groups": []} for q in ("q1", "q2")]

        def done(qid, outcome, text, error=None, **kw):
            return {"finalist": "f", "question_id": qid, "status": "done", "outcome": outcome, "request_id": f"r-{qid}",
                    "settled_micro_usd": 900, "latency_ms": 1000.0, "link_validity": {"E1": True},
                    "evidence": {"E1": {"doc_id": "d1", "chunk_id": "k1", "quote": "보안확약서를 제출한다"}},
                    "answer": {"summary": text, "error": error, "claims": [
                        {"text": text, "doc_id": "d1", "evidence_ids": ["E1"], "kind": "fact"}] if text else []}, **kw}
        with tempfile.TemporaryDirectory() as tmp:
            folder = paid_folder(Path(tmp), "generation", {"state": "complete"})
            for name, value in (("rows", rows), ("documents", {"d1": "보안 사업"}),
                                ("dataset", {"dataset": "dev", "rows": 2, "skipped": []}),
                                ("activation", {"index_version": "i1", "mode": "hybrid"})):
                (folder / "inputs" / f"{name}.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            write_outputs(folder, {
                "base": {"rows": [done("q1", "answered", "보안확약서를 제출한다", trace_url="http://lf/t/1"),
                                  done("q2", "technical_error", "", error="claim_without_evidence")]},
                "c1": {"rows": [done("q1", "answered", "확약서를 낸다"),
                                {"question_id": "q2", "status": "blocked", "reason": "envelope"}]}})
            index = SimpleNamespace(chunks=[{"chunk_id": "k1", "section_path": ["2. 보안"], "body": "보안확약서를 제출한다"}],
                                    elements={})
            record = json.loads((folder / "run.json").read_text(encoding="utf-8"))
            with mock.patch("rfp_assistant.retrieval.retrieval.KeywordIndex.load", return_value=index):
                view = paid.score_generation(SimpleNamespace(), folder, record)
            base, c1 = view["summary"]["base"], view["summary"]["c1"]
            self.assertEqual((base["rows"], base["answered"], base["required_correct"], base["required"]), (2, 2, 1, 2))
            self.assertEqual((base["claims_supported"], base["claims"], base["validation_failures"]),
                             (1, 1, {"claim_without_evidence": 1}))
            self.assertEqual((base["cost_micro_usd"], base["paid_answers"], base["latency_ms"]["n"]), (1800, 2, 2))
            self.assertEqual((c1["answered"], c1["not_done"], c1["claims_supported"]), (1, 1, 0))
            q1 = view["questions"][0]["candidates"]
            self.assertEqual(q1["base"]["evidence"]["E1"]["section"], "2. 보안")
            self.assertEqual((q1["base"]["trace_url"], q1["c1"]["trace_url"]), ("http://lf/t/1", None))
            self.assertTrue(all(q["differs"] for q in view["questions"]))

            (folder / "view.json").write_text(json.dumps(view, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(server.read_view(Path(tmp), folder.name)["stage"], "generation")
            saved = server.write_decision(Path(tmp), view, None, {"c1": "paraphrases the obligation"}, {"q2": "재확인"})
            markdown = Path(saved["markdown"]).read_text(encoding="utf-8")
            for line in ("passed 0/2; required claims correct 1/2", "claim_without_evidence 1", "### `q2` 질문 q2",
                         "Merge no candidate."):
                self.assertIn(line, markdown)

    def test_ocr_view_counts_and_character_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = paid_folder(Path(tmp), "ocr", {"state": "complete"})
            items = [{"n": 0, "title": "사업", "doc_id": "d1", "bindata": "BIN0001.png", "kind": "hwp", "sample": False,
                      "reasons": ["low_confidence"]},
                     {"n": 1, "title": "사업", "doc_id": "d1", "page": 3, "kind": "png", "sample": True, "reasons": []},
                     {"n": 2, "title": "사업", "doc_id": "d1", "bindata": "BIN0002.wmf", "kind": "hwp", "sample": False,
                      "reasons": ["unavailable:format"]}]
            (folder / "inputs" / "images.json").write_text(json.dumps(
                {"ocr_version": "v", "flagged": 2, "sample": 1, "seed": 1, "images": items}), encoding="utf-8")
            unreadable = {"n": 2, "status": "unreadable", "text": "", "reasons": []}
            write_outputs(folder, {
                "base": {"images": [{"n": 0, "status": "remote", "text": "가나다라", "reasons": ["low_confidence"],
                                     "micro_usd": 600}, {"n": 1, "status": "local", "text": "표 1", "reasons": []},
                                    unreadable]},
                "c1": {"images": [{"n": 0, "status": "remote", "text": "가나다마", "reasons": ["low_confidence"],
                                   "micro_usd": 700}, {"n": 1, "status": "unresolved", "text": "표 1", "reasons": ["loop"]},
                                  unreadable]}})
            view = paid.score_ocr(folder, json.loads((folder / "run.json").read_text(encoding="utf-8")))
            self.assertEqual({k: view["summary"]["base"][k] for k in ("images", "read", "flagged", "reread",
                                                                      "unreadable", "spent_micro_usd")},
                             {"images": 3, "read": 2, "flagged": 1, "reread": 1, "unreadable": 1, "spent_micro_usd": 600})
            self.assertEqual({k: view["summary"]["c1"][k] for k in ("flagged", "reread", "unresolved")},
                             {"flagged": 2, "reread": 1, "unresolved": 1})
            first = next(i for i in view["images"] if i["n"] == 0)
            self.assertEqual(first["candidates"]["c1"]["diff"], [["equal", "가나다", "가나다"], ["replace", "라", "마"]])
            self.assertEqual([i["n"] for i in view["images"]], [0, 1, 2])  # differing images first

            (folder / "view.json").write_text(json.dumps(view, ensure_ascii=False), encoding="utf-8")
            saved = server.write_decision(Path(tmp), view, "c1", {}, {"0": "마가 맞음"})
            markdown = Path(saved["markdown"]).read_text(encoding="utf-8")
            for line in ("re-read 1/2", "not OCR'd (unreadable) 1/3", "### `0` 사업 · BIN0001.png", "chose `r`"):
                self.assertIn(line, markdown)


if __name__ == "__main__":
    unittest.main()
