"""Offline checks for the handoff collector's read-only and source-text boundaries."""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from rfp_assistant import postgres, store
from tests import fixtures

spec = importlib.util.spec_from_file_location("retrieval_snapshot", ROOT / "tools" / "export" / "retrieval_snapshot.py")
kit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kit)


class HandoffTest(unittest.TestCase):
    def test_inventory_preserves_database_and_never_exports_element_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            runtime.mkdir()
            db = postgres.Target(fixtures.database())
            with store.open_db(db) as conn, store.tx(conn):
                conn.execute("INSERT INTO sources(source_hash, format, original_path, active_extraction_id, "
                             "parse_status) VALUES ('hash','hwp','PRIVATE_PATH','extraction','parsed')")
                conn.execute("INSERT INTO documents VALUES ('doc',1,'example.hwp','hash','{}','{}','{}')")
                conn.execute("INSERT INTO extractions VALUES ('extraction','hash','parser','PRIVATE_PATH','today',NULL)")
                conn.execute("INSERT INTO elements VALUES ('extraction','element',1,'paragraph',NULL,"
                             "'PRIVATE_SOURCE_BODY','PRIVATE_SOURCE_BODY','{}',NULL)")
                conn.execute("INSERT INTO app_settings VALUES ('active_index','index')")
                conn.execute("INSERT INTO fidelity_checks VALUES ('extraction','native-print','hash','auto_flagged',"
                             "'{}',?, 'render-hash','today')", (json.dumps([
                                 {"page": 3, "element_id": "element", "text": "PRIVATE_SOURCE_BODY"}]),))

            def state():
                with store.open_db(db) as conn:
                    return [conn.execute(f"SELECT md5(string_agg(t::text, '|' ORDER BY t::text)) FROM {name} t"
                                         ).fetchone()[0] for name in ("sources", "documents", "elements", "app_settings",
                                                                      "fidelity_checks", "schema_migrations")]

            before = state()
            out = root / "out"
            kit.snapshot(os.environ[db.dsn_env], runtime, out)
            self.assertEqual(before, state())
            manifest = kit.read_json(out / "manifest.json")
            self.assertEqual(manifest["status"], "inventory_only")
            self.assertEqual(manifest["steps"], [])
            inventory = kit.read_json(out / "manifest-report.json")
            self.assertEqual(inventory["sources"][0]["active_extraction_id"], "extraction")
            self.assertEqual(inventory["sources"][0]["navigation_anchors"][0]["element_id"], "element")
            fidelity = kit.read_json(out / "fidelity-summary.json")
            self.assertEqual(fidelity[0]["finding_counts"], {"total": 1})
            self.assertTrue(fidelity[0]["is_current"])
            self.assertEqual(fidelity[0]["locations"], [{"element_id": "element", "page": 3}])
            combined = "".join(p.read_text(encoding="utf-8") for p in out.iterdir())
            self.assertNotIn("PRIVATE_SOURCE_BODY", combined)
            self.assertNotIn("PRIVATE_PATH", combined)
            for path in out.iterdir():
                self.assertFalse(path.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_export_keeps_metrics_and_questions_but_drops_bodies_and_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "runtime" / "runs" / "H-example"
            run.mkdir(parents=True)
            kit.write_json(run / "config.json", {"mode": "hybrid", "eval_version": "retrieval-eval-2",
                "embedding": {"model": "test", "dims": 2, "api_key": "PRIVATE_CREDENTIAL"}})
            kit.write_json(run / "scores.json", {"status": "complete", "latency_ms": {"hr_under_load_p95": 400}, "aggregate": {
                "ndcg@5": 0.75, "critical_failures": ["q1"], "payload": "PRIVATE_SOURCE_BODY"}})
            (run / "traces.jsonl").write_text(json.dumps({"id": "q1", "question": "VAT included?",
                "ranking": ["c1"], "packed": [], "metrics": {"packed_complete": 0},
                "payload": "PRIVATE_SOURCE_BODY", "evidence": "PRIVATE_SOURCE_BODY",
                "candidates": [{"chunk_id": "c1", "rank": 1, "payload": "PRIVATE_SOURCE_BODY"}],
                "limitations": ["sk-fakeabcdefghijklmnopqrstuvwxyz"]}) + "\n", encoding="utf-8")
            out = root / "out"
            kit.export_run(root / "runtime", out, "H-example")
            text = "".join(p.read_text(encoding="utf-8") for p in (out / "runs" / "H-example").iterdir())
            self.assertIn("VAT included?", text)
            self.assertIn('"packed_complete": 0', text)
            self.assertIn('"ndcg@5": 0.75', text)
            self.assertIn('"hr_under_load_p95": 400', text)
            self.assertNotIn("PRIVATE_SOURCE_BODY", text)
            self.assertNotIn("PRIVATE_CREDENTIAL", text)
            self.assertNotIn("sk-fake", text)
            with self.assertRaises(ValueError):
                kit.export_run(root / "runtime", out, "../escape")

    def test_export_keeps_the_evaluated_population_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for run_id, population in (("D-a", "a" * 64), ("D-b", "b" * 64)):
                run = root / "runtime" / "runs" / run_id
                run.mkdir(parents=True)
                kit.write_json(run / "config.json", {"mode": "dense", "eval_version": "retrieval-eval-3",
                                                     "population_sha256": population, "population_size": 24})
                kit.write_json(run / "scores.json", {"status": "complete"})
                (run / "traces.jsonl").write_text("", encoding="utf-8")
                kit.export_run(root / "runtime", root / "out", run_id)
            exported = {r: kit.read_json(root / "out" / "runs" / r / "config.json") for r in ("D-a", "D-b")}
            self.assertEqual((exported["D-a"]["population_sha256"], exported["D-a"]["population_size"]), ("a" * 64, 24))
            self.assertNotEqual(exported["D-a"]["population_sha256"], exported["D-b"]["population_sha256"])


if __name__ == "__main__":
    unittest.main()
