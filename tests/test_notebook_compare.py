"""The notebook must refresh source, isolate versions and expose real regressions."""

import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tools.notebooks import compare


class NotebookSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bidmate-notebook-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        for name in compare.SOURCE_PATHS[1:]:
            (self.root / name).write_text("", encoding="utf-8")
        for name in (compare.INPUT, compare.BASELINE, "tools/notebooks/compare.py", "tools/notebooks/replay.py"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((compare.ROOT / name).read_bytes())
        self.source = self.root / "src/rfp_assistant/retrieval/retrieval.py"
        self.source.parent.mkdir(parents=True)
        self.source.write_text("def rank_lexical():\n    return 'before'\n", encoding="utf-8")
        compare.git(self.root, "add", ".")
        compare.git(self.root, "-c", "user.name=Notebook Test", "-c", "user.email=notebook@example.invalid",
                    "commit", "-qm", "baseline")
        self.ref = compare.git(self.root, "rev-parse", "HEAD").decode().strip()
        compare.write_atomic(self.root / compare.BASELINE, json.dumps({"commit": self.ref}))

    def test_sync_refreshes_source_clears_stale_output_and_preserves_unchanged_output(self):
        self.assertTrue(compare.sync(self.root))
        notebook = self.root / compare.NOTEBOOK
        data = json.loads(notebook.read_text(encoding="utf-8"))
        data["cells"][1]["outputs"] = [{"output_type": "stream", "name": "stdout", "text": ["old result"]}]
        notebook.write_text(json.dumps(data), encoding="utf-8")
        self.assertFalse(compare.sync(self.root))
        self.assertTrue(json.loads(notebook.read_text())["cells"][1]["outputs"])
        self.source.write_text("def rank_lexical():\n    return 'after'\n", encoding="utf-8")
        self.assertTrue(compare.sync(self.root))
        updated = json.loads(notebook.read_text(encoding="utf-8"))
        self.assertIn("after", "".join(next(c for c in updated["cells"] if c["id"] == "source-bm25")["source"]))
        self.assertEqual(updated["cells"][1]["outputs"], [])
        self.assertFalse(notebook.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_watch_updates_after_save_without_a_sync_command(self):
        compare.sync(self.root)
        stop = threading.Event()
        worker = threading.Thread(target=compare.watch, args=(self.root, stop), daemon=True)
        worker.start()
        try:
            self.source.write_text("def rank_lexical():\n    return 'watch-sentinel'\n", encoding="utf-8")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if "watch-sentinel" in (self.root / compare.NOTEBOOK).read_text(encoding="utf-8"):
                    break
                stop.wait(0.1)
            else:
                self.fail("the watcher did not refresh the notebook")
        finally:
            stop.set()
            worker.join(timeout=10)
        self.assertFalse(worker.is_alive())

    def test_baseline_survives_new_commits_and_current_snapshot_includes_additions_and_deletions(self):
        before_ref, before = compare.baseline_files(self.root)
        self.source.unlink()
        added = self.source.with_name("new.py")
        added.write_text("value = 2\n", encoding="utf-8")
        current = compare.current_files(self.root)
        self.assertIn("src/rfp_assistant/retrieval/new.py", current)
        self.assertNotIn("src/rfp_assistant/retrieval/retrieval.py", current)
        compare.git(self.root, "add", ".")
        compare.git(self.root, "-c", "user.name=Notebook Test", "-c", "user.email=notebook@example.invalid",
                    "commit", "-qm", "after")
        self.assertEqual(compare.baseline_files(self.root), (before_ref, before))
        self.assertNotEqual(compare.git(self.root, "rev-parse", "HEAD").decode().strip(), self.ref)

    def test_snapshot_cannot_write_outside_its_directory(self):
        with self.assertRaisesRegex(ValueError, "leaves its destination"):
            compare.materialize({"../escape.txt": b"no"}, self.root / "snapshot")
        self.assertFalse((self.root / "escape.txt").exists())


class ComparisonTest(unittest.TestCase):
    def row(self, passed=True, output="value"):
        return {"id": "question", "stage": "retriever", "input": {}, "output": output,
                "checks": {"evidence": passed}, "elapsed_ms": 1}

    def status(self, left, right):
        return compare.compare_rows({"observations": left}, {"observations": right})[0]["status"]

    def test_changed_output_is_not_automatically_an_improvement(self):
        self.assertEqual(self.status([self.row()], [self.row(output="other")]), "변경됨")
        self.assertEqual(self.status([self.row()], [self.row(False)]), "악화")
        self.assertEqual(self.status([self.row(False)], [self.row()]), "개선")
        self.assertEqual(self.status([self.row(False)], [self.row(False)]), "양쪽 실패")
        self.assertEqual(self.status([self.row()], []), "비교 불가")

    def test_report_escapes_source_and_answer_markup(self):
        output = {"evidence": [{"evidence_id": "E1", "doc_id": "A", "quote": "<script>alert(1)</script>"}],
                  "ranked_passages": [], "excluded": [], "limitations": []}
        rows = compare.compare_rows({"observations": [self.row(output=output)]},
                                    {"observations": [self.row(output=output)]})
        rendered = compare.render_stage({"rows": rows}, "retriever")
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)

    def test_bundle_rejects_duplicate_ids_and_unchecked_questions(self):
        bundle = json.loads((compare.ROOT / compare.INPUT).read_text(encoding="utf-8"))
        compare.validate_bundle(bundle)
        bundle["questions"].append(bundle["questions"][0])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            compare.validate_bundle(bundle)
        bundle["questions"].pop()
        bundle["questions"][0]["required"] = []
        with self.assertRaisesRegex(ValueError, "expected evidence"):
            compare.validate_bundle(bundle)
        bundle["questions"][0]["expect_empty"] = "false"
        with self.assertRaisesRegex(ValueError, "must be boolean"):
            compare.validate_bundle(bundle)


class ReplayIntegrationTest(unittest.TestCase):
    def test_a_real_retrieval_regression_is_visible_in_separate_processes(self):
        files = compare.current_files(compare.ROOT)
        source = "src/rfp_assistant/retrieval/retrieval.py"
        self.assertIn(b"return ranked[:k]", files[source])
        broken = {**files, source: files[source].replace(b"return ranked[:k]", b"return []", 1)}
        with tempfile.TemporaryDirectory(prefix="bidmate-replay-test-") as tmp:
            root = Path(tmp)
            results = []
            for i, snapshot in enumerate((files, broken)):
                target = root / str(i)
                compare.materialize(snapshot, target)
                result = compare.execute(target, compare.ROOT / compare.INPUT,
                    compare.ROOT / "tools/notebooks/replay.py", root / f"{i}.json")
                self.assertNotIn("error", result, result)
                results.append(result)
            rows = compare.compare_rows(*results)
            baseline_failures = [r["id"] for r in results[0]["observations"] if not all(r["checks"].values())]
            self.assertEqual(baseline_failures, [])
            regressed = {r["id"] for r in rows if r["status"] == "악화"}
            self.assertIn("search:warranty", regressed)
            self.assertIn("search:vat", regressed)
            self.assertEqual(compare.current_files(compare.ROOT)[source], files[source])


if __name__ == "__main__":
    unittest.main()
