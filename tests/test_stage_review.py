"""The stage review contract: the run folder the runner writes and the review app reads, and the decision files."""

import gzip
import importlib.util
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from rfp_assistant.corpus import ingestion
from rfp_assistant.evaluation import stage_review
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


class StageReviewContractTest(unittest.TestCase):
    def test_run_folder_and_decision_files(self):
        self.assertEqual(stage_review.SCHEMA, server.RUN_SCHEMA)
        elements = ingestion.finalize_elements(ingestion.walk_hwp(ET.fromstring(DOC)), "x1")
        kept = chunking.build_chunks(elements, "x1")[0]
        lost = [{**c, "payload": c["payload"].replace("□ 조직별 역할", "")} for c in kept]
        run_id = "chunking-20261010T000000Z-abc123"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
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


if __name__ == "__main__":
    unittest.main()
