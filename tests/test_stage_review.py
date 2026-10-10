"""The stage review contract: the run folder the runner writes and the review app reads, and the decision files."""

import gzip
import importlib.util
import io
import json
import os
import tempfile
import unittest
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import date
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

from rfp_assistant.corpus import ingestion, ocr
from rfp_assistant.evaluation import stage_review
from rfp_assistant.evaluation import stage_review_paid as paid
from rfp_assistant.evaluation import stage_review_worker as worker
from rfp_assistant.gateway import budget
from rfp_assistant.retrieval import chunking
from rfp_assistant.settings import Settings
from rfp_assistant.storage import postgres, store
from tests import fixtures

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
            self.assertEqual((second["state"], second["candidates"][1]["reason"]), ("complete", None))
            self.assertEqual(ran, ["c1.json", "c1.json"])  # the finished baseline is never paid again

    def test_reprice_never_touches_a_completed_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = paid_folder(Path(tmp), "ocr", {"state": "needs_approval"})
            settings = SimpleNamespace(data_dir=Path(tmp))
            with mock.patch.object(paid, "estimate", return_value={"estimate_id": "e2"}) as priced:
                self.assertEqual(stage_review.reprice(settings, folder.name)["estimate"], "e2")
                record = json.loads((folder / "run.json").read_text(encoding="utf-8"))
                (folder / "run.json").write_text(json.dumps({**record, "state": "complete"}), encoding="utf-8")
                with self.assertRaisesRegex(stage_review.ReviewError, "not waiting for an estimate"):
                    stage_review.reprice(settings, folder.name)  # a completed run has nothing left to price
            self.assertEqual(priced.call_count, 1)

    def test_a_replayed_answer_has_no_measured_latency(self):
        def conn(created):
            return SimpleNamespace(execute=lambda sql, args: SimpleNamespace(fetchone=lambda: (created,)))
        row = {"question_id": "q1", "request_id": "r1", "answered_at": "2026-10-10T06:00:00+00:00", "latency_ms": 1500.0}
        self.assertEqual(paid._replayed(conn("2026-10-10T05:00:00+00:00"), [row]), ["q1"])  # stored an hour earlier
        self.assertEqual(paid._replayed(conn("2026-10-10T05:59:59+00:00"), [row]), [])  # created by this call

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
                "c1": {"rows": [done("q1", "technical_error", "", error="InternalServerError: Error code: 503"),
                                {"question_id": "q2", "status": "blocked", "reason": "envelope"}]}})
            index = SimpleNamespace(chunks=[{"chunk_id": "k1", "section_path": ["2. 보안"], "body": "보안확약서를 제출한다"}],
                                    elements={})
            record = json.loads((folder / "run.json").read_text(encoding="utf-8"))
            record["candidates"][1]["replayed"] = ["q1"]
            with mock.patch("rfp_assistant.retrieval.retrieval.KeywordIndex.load", return_value=index):
                view = paid.score_generation(SimpleNamespace(), folder, record)
            base, c1 = view["summary"]["base"], view["summary"]["c1"]
            self.assertEqual((base["rows"], base["answered"], base["required_correct"], base["required"]), (2, 2, 1, 2))
            self.assertEqual((base["claims_supported"], base["claims"], base["validation_failures"]),
                             (1, 1, {"claim_without_evidence": 1}))
            self.assertEqual((base["cost_micro_usd"], base["paid_answers"], base["latency_ms"]["n"]), (1800, 2, 2))
            self.assertEqual((c1["answered"], c1["not_done"], c1["claims_supported"]), (1, 1, 0))
            self.assertEqual(c1["latency_ms"]["n"], 0)  # its one answer was replayed, so no latency was measured
            # a provider 503 failed the request; it is no verdict of the answer validator
            self.assertEqual((c1["technical_failures"], c1["validation_failures"]), (1, {}))
            self.assertEqual(paid._claim_text({"match": {"type": "number", "value": 3, "unit": "초"},
                                               "qualifiers": [["최대"], ["이내"]]}), "3초 (최대 · 이내)")
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


U = ocr.REMOTE_MAX_OUTPUT  # one worst-case gpt-5-mini read when its output costs 1 micro-USD a token


def png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, "PNG")
    return buf.getvalue()


class Reads:
    """gpt-5-mini as the ledger sees it: each call settles its planned completion tokens, or the worker dies mid-call."""

    def __init__(self, *plan):
        self.plan, self.calls = list(plan), 0

    def chat(self, **_):
        self.calls += 1
        step = self.plan.pop(0)
        if step == "crash":
            raise KeyboardInterrupt
        return SimpleNamespace(usage={"prompt_tokens": 0, "completion_tokens": step}, response_id=str(uuid.uuid4()),
                               refusal=None, finish_reason="stop", content="글")

    def close(self):
        pass


class PaidLedgerTest(unittest.TestCase):
    """Recovery, pricing and admission of paid runs on a real ledger: an approval is a maximum across every resume."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.env = fixtures.database()
        self.db = postgres.Target(self.env)
        budget.ensure_budget_row(self.db)
        self.rate(1, "r1")
        budget.set_paid_enabled(self.db, "owner", True, "test")
        with store.open_db(self.db) as conn:  # both paid purposes funded
            conn.execute("UPDATE budget_settings SET envelopes_json = ?",
                         (json.dumps({"gold_eval": 10 ** 8, "ocr": 10 ** 8}),))
        self.settings = Settings(source_dir=self.root, data_dir=self.root, hwp_converter=None,
                                 database_dsn_env=self.env)
        # The worker opens its own pool on the ledger, as a separate process would.
        self.worker_env = f"{self.env}_WORKER"
        os.environ[self.worker_env] = os.environ[self.env]
        self.addCleanup(os.environ.pop, self.worker_env)

    def rate(self, output: int, version: str) -> None:
        budget.configure(self.db, "owner", project_start=date(2026, 9, 30), project_end=date(2026, 10, 28),
                         prior_use_micro=0, prior_use_evidence="test", rate_version=version, enable_paid=True,
                         rates={"gpt-5-mini": {"input": "0", "cached_input": "0", "output": str(output)}})

    def attempt(self, key: str, state: str, reserved: int, settled: int | None = None, purpose: str = "gold_eval"):
        with store.open_db(self.db) as conn:
            row = conn.execute("SELECT request_id FROM requests WHERE idempotency_key = ?", (key,)).fetchone()
            rid = row[0] if row else str(uuid.uuid4())
            if row is None:
                conn.execute("INSERT INTO requests(request_id, member_id, idempotency_key, input_hash, config_hash, "
                             "scope_json, status, created_at, updated_at) VALUES (?, 'm', ?, 'h', 'c', '[]', "
                             "'running', 't', 't')", (rid, key))
            conn.execute("INSERT INTO attempts(attempt_id, request_id, member_id, stage, purpose, model, state, "
                         "reserved_micro_usd, settled_micro_usd, estimated_input_tokens, max_output_tokens, "
                         "count_method, price_json, created_at) VALUES (?, ?, 'm', 's', ?, 'gpt-5-mini', ?, ?, ?, 0, 0, "
                         "'t', '{}', 't')", (str(uuid.uuid4()), rid, purpose, state, reserved, settled))
        return rid

    def settled(self) -> int:
        with store.open_db(self.db) as conn:
            return conn.execute("SELECT COALESCE(SUM(settled_micro_usd), 0) FROM attempts "
                                "WHERE state = 'settled'").fetchone()[0]

    def folder(self, stage: str, state: str) -> Path:
        return paid_folder(self.root, stage, {"state": state, "ledger_env": self.env, "input_sha256": {},
                                              "worker_sha256": "w"})

    def ocr_folder(self, images: int, state: str = "needs_approval") -> Path:
        folder = self.folder("ocr", state)
        (folder / "inputs" / "images").mkdir()
        items = [{"n": n, "kind": "png", "digest": f"d{n}", "reasons": ["loop"], "sample": False} for n in range(images)]
        for n in range(images):
            (folder / "inputs" / "images" / f"{n}.bin").write_bytes(png())
        (folder / "inputs" / "images.json").write_text(json.dumps(
            {"ocr_version": "v", "flagged": images, "sample": 0, "seed": 1, "images": items}), encoding="utf-8")
        (folder / "candidates" / "c1.estimate.json").write_text(json.dumps(
            {"status": "complete", "per_read_micro_usd": U, "input_tokens": 0, "max_output_tokens": U,
             "model": "gpt-5-mini", "images": [{"n": n, "status": "local", "text": "", "reasons": ["loop"]}
                                               for n in range(images)]}), encoding="utf-8")
        return folder

    def ocr_config(self, folder: Path, est: dict) -> dict:
        record = json.loads((folder / "run.json").read_text(encoding="utf-8"))
        return {**paid.paid_config(folder, record, est, record["candidates"][1]), "ledger_env": self.worker_env}

    def read(self, folder: Path, config: dict, transport: Reads) -> dict:
        real = ocr.RemoteReader
        with mock.patch.object(ocr, "RemoteReader", lambda s, job: real(s, transport=transport, job=job)):
            return worker.ocr_stage(folder / "inputs", config, "paid")

    def test_resumed_ocr_never_repays_a_read_or_passes_the_approval(self):
        folder = self.ocr_folder(2)
        est = {"candidates": {"c1": {"max_micro_usd": 2 * U, "per_read_micro_usd": U, "flagged": [0, 1]}}}
        crashed = Reads(U * 8 // 10, "crash")
        with self.assertRaises(KeyboardInterrupt):  # the worker dies after one paid read, before writing its output
            self.read(folder, self.ocr_config(folder, est), crashed)
        with store.open_db(self.db) as conn:  # the lost call is billed in full and the owner settles it so
            lost = conn.execute("SELECT attempt_id FROM attempts WHERE state = 'dispatching'").fetchone()[0]
        budget.settle(self.db, lost, {"completion_tokens": U}, None)
        resumed = Reads(U * 8 // 10, U * 8 // 10)
        out = self.read(folder, self.ocr_config(folder, est), resumed)
        self.assertEqual(resumed.calls, 0)  # the first read is kept, and the billed second one is not paid again
        self.assertEqual([i["status"] for i in out["images"]], ["remote", "unresolved"])
        self.assertIn("lost", out["images"][1]["error"])
        self.assertLessEqual(self.settled(), 2 * U)

    def test_a_read_whose_progress_could_not_be_written_is_never_paid_again(self):
        folder = self.ocr_folder(2)
        est = {"candidates": {"c1": {"max_micro_usd": 2 * U, "per_read_micro_usd": U, "flagged": [0, 1]}}}
        config = self.ocr_config(folder, est)
        first = self.read(folder, {**config, "progress": str(folder / "gone" / "p.jsonl")}, Reads(U * 8 // 10))
        self.assertEqual((first["status"], first["images"][0]["status"]), ("stopped", "remote"))
        # Its output is lost too (the disk is full): only the ledger knows image 0 was paid.
        resumed = Reads(U * 8 // 10)
        out = self.read(folder, config, resumed)
        self.assertEqual(resumed.calls, 1)
        self.assertEqual([i["status"] for i in out["images"]], ["unresolved", "remote"])
        self.assertLessEqual(self.settled(), 2 * U)
        with store.open_db(self.db) as conn:
            self.assertEqual(paid._done(conn, folder, json.loads((folder / "run.json").read_text()), "c1"), {0, 1})

    def test_a_stored_answer_whose_output_was_lost_replays_and_is_not_priced_again(self):
        folder = self.folder("generation", "stopped")
        record = json.loads((folder / "run.json").read_text(encoding="utf-8"))
        rid = self.attempt(f"{folder.name}:c1:q1:1", "settled", 10, 8)
        with store.open_db(self.db) as conn:  # the answer finished in the ledger; c1.json never got written
            conn.execute("UPDATE requests SET status = 'completed', result_json = ? WHERE request_id = ?",
                         (json.dumps({"status": "answered"}), rid))
        calls = []

        def answer_row(_s, _p, _run, _cid, row, _rec):
            calls.append(row["question_id"])
            return {"question_id": row["question_id"], "status": "done", "request_id": rid}
        (folder / "inputs" / "rows.json").write_text(json.dumps([{"question_id": "q1"}]), encoding="utf-8")
        (folder / "inputs" / "activation.json").write_text("{}", encoding="utf-8")
        from rfp_assistant.service import answers, service
        owner = SimpleNamespace(transport=object(), tracing=None, close=lambda: None)
        with mock.patch.object(worker, "_generation_settings", return_value=self.settings), \
                mock.patch.object(service, "Resources", return_value=owner), \
                mock.patch.object(answers, "PinnedResources", return_value=SimpleNamespace(close=lambda: None)), \
                mock.patch.object(answers, "_answer_row", answer_row):
            out = worker.generation(folder / "inputs", {"run_key": folder.name, "candidate": "c1",
                                                        "cap_micro_usd": 10, "ledger_key": f"{folder.name}:c1:",
                                                        "prices": {"q1": 10}}, "paid")
        self.assertEqual((calls, out.get("status")), (["q1"], None))  # 8 held + 10 priced > 10, yet it replays
        with store.open_db(self.db) as conn:  # and a re-price leaves it out
            self.assertEqual(paid._done(conn, folder, record, "c1"), {"q1"})

    def test_a_rate_change_is_repriced_and_never_reserved_above_the_approval(self):
        folder = self.ocr_folder(1)
        record = json.loads((folder / "run.json").read_text(encoding="utf-8"))
        gen = self.folder("generation", "needs_approval")
        (gen / "candidates" / "c1.estimate.json").write_text(json.dumps(
            {"status": "complete", "model": "gpt-5-mini", "max_output_tokens": U, "rate_version": "r1",
             "prices": [{"question_id": "q1", "max_micro_usd": U, "input_tokens": 0, "extra_micro_usd": 0}]}),
            encoding="utf-8")
        self.rate(3, "r2")
        self.assertEqual(paid.estimate(self.settings, folder, record)["candidates"]["c1"]["max_micro_usd"], 3 * U)
        gen_record = json.loads((gen / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(paid.estimate(self.settings, gen, gen_record)["candidates"]["c1"]["prices"], {"q1": 3 * U})
        # An approval made at the old rate reserves nothing at the new one.
        reads = Reads(U)
        out = self.read(folder, self.ocr_config(folder, {"candidates": {"c1": {
            "max_micro_usd": U, "per_read_micro_usd": U, "flagged": [0]}}}), reads)
        self.assertEqual((reads.calls, out["status"]), (0, "stopped"))

    def test_the_read_the_approval_refuses_is_unresolved_with_its_reason(self):
        folder = self.ocr_folder(2)
        reads = Reads(U * 8 // 10, U)
        out = self.read(folder, self.ocr_config(folder, {"candidates": {"c1": {
            "max_micro_usd": U * 3 // 2, "per_read_micro_usd": U, "flagged": [0, 1]}}}), reads)
        self.assertEqual((reads.calls, out["status"]), (1, "stopped"))
        self.assertEqual(out["images"][1]["status"], "unresolved")  # never shown as a resolved local read
        self.assertIn("above_consented_maximum", out["images"][1]["error"])
        (folder / "candidates" / "c1.json").write_text(json.dumps(out), encoding="utf-8")
        record = json.loads((folder / "run.json").read_text(encoding="utf-8"))
        record["candidates"][1]["status"] = "stopped"
        self.assertEqual(paid.score_ocr(folder, record)["summary"]["c1"]["unresolved"], 1)

    def test_a_stopped_run_whose_approval_expired_is_priced_again_for_what_is_left(self):
        folder = self.ocr_folder(2, state="stopped")
        (folder / "estimate.json").write_text(json.dumps(
            {"estimate_id": "old", "run_id": folder.name, "approved_by": "kim", "approved_at": "2026-10-09T00:00:00",
             "expires_at": "2026-10-09T00:00:00+00:00", "candidates": {}}), encoding="utf-8")
        record = json.loads((folder / "run.json").read_text(encoding="utf-8"))
        record["candidates"][1]["status"] = "stopped"
        (folder / "run.json").write_text(json.dumps(record), encoding="utf-8")
        (folder / "candidates" / "c1.json").write_text(json.dumps(
            {"status": "stopped", "images": [{"n": 0, "status": "remote", "text": "글", "reasons": ["loop"]},
                                             {"n": 1, "status": "unresolved", "text": "", "reasons": ["loop"]}]}),
            encoding="utf-8")
        self.attempt(f"review:{folder.name}:c1:{ocr.OCR_VERSION}", "settled", U, U * 8 // 10, purpose="ocr")
        with self.assertRaisesRegex(stage_review.ReviewError, "expired"):
            paid.require_approved(paid.load_estimate(folder))
        stage_review.reprice(self.settings, folder.name)
        fresh = paid.approve(self.settings, folder.name, "kim")
        paid.require_approved(fresh)
        self.assertEqual({k: fresh["candidates"]["c1"][k] for k in ("max_micro_usd", "committed_micro_usd")},
                         {"max_micro_usd": U, "committed_micro_usd": U * 8 // 10})  # only image 1 is left to pay

    def test_resume_admits_what_is_left_of_a_candidate_not_its_whole_maximum(self):
        folder = self.folder("generation", "stopped")
        (folder / "worker.py").write_text("", encoding="utf-8")
        record = json.loads((folder / "run.json").read_text(encoding="utf-8"))
        record.update(input_sha256=stage_review.input_hashes(folder / "inputs"),
                      worker_sha256=stage_review._sha(b""))
        (folder / "candidates" / "c1.json").write_text(json.dumps({"status": "stopped", "rows": [
            {"question_id": q, "status": "done"} for q in ("q1", "q2")]}), encoding="utf-8")
        for q in ("q1", "q2"):
            self.attempt(f"{folder.name}:c1:{q}:1", "settled", 4, 3)
        with store.open_db(self.db) as conn:  # 10 in all: 6 spent, 4 left
            conn.execute("UPDATE budget_settings SET cap_micro_usd = 10")
        est = {"rate_version": "r1", "purpose": "gold_eval", "candidates": {"c1": {
            "max_micro_usd": 10, "prices": {"q1": 4, "q2": 4, "q3": 2}}}}
        paid.recheck(self.settings, folder, record, est)  # q3's 2 fits the 4 left

    def test_a_stopped_ocr_candidate_reports_its_ledger(self):
        folder = self.ocr_folder(2, state="stopped")
        record = json.loads((folder / "run.json").read_text(encoding="utf-8"))
        record["candidates"][1]["status"] = "stopped"
        key = f"review:{folder.name}:c1:{ocr.OCR_VERSION}"
        rid = self.attempt(key, "settled", U, 900, purpose="ocr")
        self.attempt(key, "unknown", U, purpose="ocr")
        (folder / "candidates" / "c1.json").write_text(json.dumps(
            {"status": "stopped", "request_id": rid, "images": []}), encoding="utf-8")
        paid.ledger_spend(self.settings, folder, record)
        self.assertEqual({k: record["candidates"][1]["ledger"][k] for k in ("settled_micro_usd", "attempts", "open")},
                         {"settled_micro_usd": 900, "attempts": 2, "open": 1})


class MetadataAnswerTest(unittest.TestCase):
    def test_metadata_answers_keep_their_verdict_and_facts(self):
        row = {"question_id": "m", "question": "금액", "question_type": "fact", "answerability": "answerable",
               "expected_status": "answered", "mode": "metadata", "scope": [{"doc_id": "d", "source_hash": "h"}],
               "expected_states": {"amount_krw": "known", "bid_close": "unknown"}}
        facts = [{"doc_id": "d", "field": "amount_krw", "state": "known"},
                 {"doc_id": "d", "field": "bid_close", "state": "unknown"}]

        def answered(found):
            return {"finalist": "c1", "question_id": "m", "status": "done", "outcome": "answered",
                    "answer": {"facts": found}}
        good, bad = (paid._answer(row, answered(f), None, {}, {}) for f in (facts, facts[:1]))
        self.assertEqual((good["metadata_correct"], bad["metadata_correct"]), (True, False))
        self.assertEqual((good["facts"], bad["facts"]), (facts, facts[:1]))
        summary = paid._generation_summary([good, bad])
        self.assertEqual({k: summary[k] for k in ("metadata_correct", "metadata_rows", "passed", "passage_rows")},
                         {"metadata_correct": 1, "metadata_rows": 2, "passed": 0, "passage_rows": 0})
        self.assertIn("passed 0/0; metadata questions correct 1/2", server._measure("generation", summary))


if __name__ == "__main__":
    unittest.main()
