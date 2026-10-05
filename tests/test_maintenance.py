"""The maintenance sequence: order, reuse on a rerun, a stop at a failed step or a paid estimate, serving untouched."""

import csv
import io
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import psycopg

from rfp_assistant import ingestion, maintenance, service
from rfp_assistant.generation import FakeTransport
from rfp_assistant.retrieval import KeywordIndex, build_keyword_index
from tests import fixtures


def reused(*_, **__):
    return {"status": "reused"}


class SequenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = fixtures.make_env(self.root / "live", paid=False)
        self.s = self.env.settings
        self.patch = mock.patch.dict(os.environ, {"RFP_BACKUP_DIR": str(self.root / "backups")})
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def run_with(self, **fakes):
        with mock.patch.dict(maintenance.STEP_FUNCTIONS, fakes):
            return maintenance.run(self.s, fixtures.analyzer(), None, "tester")

    def statuses(self, state):
        return {s["name"]: s["status"] for s in state["steps"]}

    def test_a_failed_step_stops_the_sequence_and_names_itself(self):
        def broken(*_, **__):
            raise maintenance.StepFailed("the dump does not restore")

        state = self.run_with(backup=reused, restore_check=broken)
        self.assertEqual((state["status"], state["stopped_at"], state["reason"]),
                         ("failed", "restore_check", "the dump does not restore"))
        self.assertEqual(self.statuses(state), {"backup": "reused", "restore_check": "failed", "ingest": "pending",
                                                "fidelity": "pending", "keyword": "pending", "embedding": "pending",
                                                "regression": "pending", "report": "pending"})
        self.assertTrue(state["serving"]["unchanged"])
        self.assertEqual(maintenance.last(self.s)["run_id"], state["run_id"])
        report = (maintenance.root(self.s) / "reports" / f"{state['run_id']}.md").read_text(encoding="utf-8")
        self.assertIn("at `restore_check`: the dump does not restore", report)

    def test_a_paid_embedding_stops_at_its_estimate(self):
        def paid(*_, **__):
            raise maintenance.NeedsApproval({"estimate_id": "e1", "total_micro_usd": 1200}, "approve estimate e1")

        state = self.run_with(backup=reused, restore_check=reused, embedding=paid, regression=reused)
        self.assertEqual((state["status"], state["stopped_at"]), ("needs_approval", "embedding"))
        step = next(s for s in state["steps"] if s["name"] == "embedding")
        self.assertEqual(step["detail"]["estimate"]["estimate_id"], "e1")
        self.assertEqual(self.statuses(state)["regression"], "pending")

    def test_new_originals_are_ingested_and_indexed_without_changing_what_serves(self):
        served = build_keyword_index(self.s, fixtures.analyzer(), include_unreviewed=True)["index_version"]
        state = self.run_with(backup=reused, restore_check=reused, regression=reused)
        self.assertEqual(state["status"], "complete", state["reason"])
        self.assertTrue(state["reused"])  # nothing changed since the fixture was ingested and indexed
        self.assertEqual(state["provider_calls"], 0)
        self.assertEqual(self.statuses(state)["embedding"], "skipped")  # the fixture serves keyword retrieval

        (self.s.source_dir / "files" / "기관F_신규 사업.pdf").write_bytes(
            fixtures.make_pdf([["신규 공고 하자보수 기간은 검수 후 24개월입니다."]]))
        buf = io.StringIO(newline="")
        csv.writer(buf).writerows([fixtures.CSV_HEADER, *fixtures.ROWS, [
            "20240006", "0.0", "신규 사업", "", "기관F", "2024-09-01", "", "", "요약", "pdf", "기관F_신규 사업.pdf", "미리보기"]])
        (self.s.source_dir / "data_list.csv").write_bytes(b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8"))
        state = self.run_with(backup=reused, restore_check=reused, regression=reused)
        self.assertEqual(state["status"], "complete", state["reason"])
        steps = {s["name"]: s for s in state["steps"]}
        self.assertEqual(steps["ingest"]["status"], "done")
        self.assertEqual([c["filename"] for c in steps["ingest"]["detail"]["changed"]], ["기관F_신규 사업.pdf"])
        self.assertEqual(steps["fidelity"]["status"], "reused")  # no HWP changed
        self.assertEqual((steps["keyword"]["status"], steps["keyword"]["detail"]["served_index_version"],
                          steps["keyword"]["detail"]["same_as_served"]), ("done", served, False))
        self.assertTrue(state["serving"]["unchanged"])
        self.assertEqual(KeywordIndex.load(self.s).version, served)  # the rebuilt index waits for an activation
        again = self.run_with(backup=reused, restore_check=reused, regression=reused)
        self.assertTrue(again["reused"])
        self.assertEqual(again["provider_calls"], 0)

    def test_a_reparse_does_not_change_what_serving_searches_before_activation(self):
        ref = self.env.refs["기관A"]
        res = service.Resources(self.s, transport=FakeTransport())
        try:
            before = service.retrieve(res, self.env.consultant, "하자보수 기간", [ref])
            corpus = service.retrieve(res, self.env.consultant, "하자보수 기간", [], all_documents=True)
        finally:
            res.close()
        self.assertTrue(before.evidence)
        key, parse = ingestion.input_key, ingestion.parse_pdf

        def next_parser(path):  # a parser change that alters one original's output
            raw, warnings, reason = parse(path)
            if ingestion.sha256_file(path) == ref.source_hash:
                raw = [{**e, "raw_text": e["raw_text"] + " (개정)"} if i == len(raw) - 1 else e for i, e in enumerate(raw)]
            return raw, warnings, reason

        with mock.patch.object(ingestion, "input_key", lambda s, src: key(s, src) + "-next"), \
                mock.patch.object(ingestion, "parse_pdf", next_parser):
            state = self.run_with(backup=reused, restore_check=reused, regression=reused)
        self.assertEqual(state["status"], "complete", state["reason"])
        steps = {s["name"]: s for s in state["steps"]}
        self.assertIn("기관A_통합 정보시스템.pdf", [c["filename"] for c in steps["ingest"]["detail"]["changed"]])
        self.assertFalse(steps["keyword"]["detail"]["same_as_served"])  # the new extraction waits in its own index
        res = service.Resources(self.s, transport=FakeTransport())
        try:
            after = service.retrieve(res, self.env.consultant, "하자보수 기간", [ref])
            corpus_after = service.retrieve(res, self.env.consultant, "하자보수 기간", [], all_documents=True)
        finally:
            res.close()
        self.assertEqual([(e.extraction_id, e.quote) for e in after.evidence],
                         [(e.extraction_id, e.quote) for e in before.evidence])
        self.assertEqual({e.doc_id for e in corpus_after.evidence}, {e.doc_id for e in corpus.evidence})
        self.assertTrue(state["serving"]["unchanged"])

    def test_backup_and_restore_check_are_reused_when_nothing_changed(self):
        rest = dict(ingest=reused, fidelity=reused, keyword=reused, embedding=reused, regression=reused)
        first = self.run_with(**rest)
        self.assertEqual(first["status"], "complete", first["reason"])
        self.assertEqual(self.statuses(first)["backup"], "done")
        self.assertEqual(self.statuses(first)["restore_check"], "done")
        second = self.run_with(**rest)  # the first backup's own audit event does not count as a change
        self.assertEqual((self.statuses(second)["backup"], self.statuses(second)["restore_check"]),
                         ("reused", "reused"))
        self.assertTrue(second["reused"])
        self.assertEqual(len(maintenance._backups(self.s)), 1)
        admin, _, _ = maintenance.scratch(self.s)
        with psycopg.connect(admin, autocommit=True) as conn:  # the scratch restore target is gone again
            self.assertIsNone(conn.execute("SELECT 1 FROM pg_database WHERE datname LIKE %s",
                                           (maintenance.SCRATCH_PREFIX + "%",)).fetchone())

    def test_an_existing_scratch_name_is_refused_not_replaced(self):
        from psycopg import sql

        admin, name, dsn = maintenance.scratch(self.s)
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("CREATE TABLE unrelated (v int)")
            with mock.patch.object(maintenance, "scratch", lambda s: (admin, name, dsn)):
                with self.assertRaisesRegex(maintenance.StepFailed, "already exists"):
                    maintenance.step_restore_check(self.s, {"backup": {"manifest": "unused"},
                                                            "backup_artifacts": "x"})
            with psycopg.connect(dsn, autocommit=True) as conn:
                self.assertIsNotNone(conn.execute("SELECT to_regclass('unrelated')").fetchone()[0])
        finally:
            with psycopg.connect(admin, autocommit=True) as conn:
                conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))

    def test_the_restore_check_never_touches_a_database_it_did_not_create(self):
        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict, make_conninfo

        live = conninfo_to_dict(os.environ[self.s.database_dsn_env])
        admin = make_conninfo(**{**live, "dbname": "postgres"})
        other = live["dbname"][:48] + "_mrestore"  # the name the first version derived and dropped
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(other)))
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(other)))
        try:
            with psycopg.connect(make_conninfo(**{**live, "dbname": other}), autocommit=True) as conn:
                conn.execute("CREATE TABLE unrelated (v int)")
                conn.execute("INSERT INTO unrelated VALUES (7)")
            state = self.run_with(ingest=reused, fidelity=reused, keyword=reused, embedding=reused,
                                  regression=reused)
            self.assertEqual(self.statuses(state)["restore_check"], "done", state["reason"])
            with psycopg.connect(make_conninfo(**{**live, "dbname": other}), autocommit=True) as conn:
                self.assertEqual(conn.execute("SELECT v FROM unrelated").fetchone()[0], 7)
            with psycopg.connect(admin, autocommit=True) as conn:  # and the run's own scratch database is gone
                left = [r[0] for r in conn.execute("SELECT datname FROM pg_database WHERE datname LIKE %s",
                                                   (maintenance.SCRATCH_PREFIX + "%",))]
            self.assertEqual(left, [])
        finally:
            with psycopg.connect(admin, autocommit=True) as conn:
                conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(other)))

    def test_a_damaged_backup_is_neither_reused_nor_trusted(self):
        rest = dict(ingest=reused, fidelity=reused, keyword=reused, embedding=reused, regression=reused)
        first = self.run_with(**rest)
        self.assertEqual(first["status"], "complete", first["reason"])
        manifest = Path(maintenance._backups(self.s)[-1]["manifest"])
        with open(manifest.parent / "database.dump", "ab") as f:
            f.write(b"\0")  # the manifest stays intact, the dump no longer matches it
        second = self.run_with(**rest)
        self.assertEqual((self.statuses(second)["backup"], self.statuses(second)["restore_check"]), ("done", "done"))
        newest = Path(maintenance._backups(self.s)[-1]["manifest"])
        self.assertNotEqual(newest, manifest)
        copied = [p for p in (newest.parent / "files").rglob("*") if p.is_file()]
        self.assertTrue(copied, "the fixture backup copies runtime files")
        copied[0].write_bytes(copied[0].read_bytes() + b"x")  # a copied runtime file changed after the check
        third = self.run_with(**rest)
        self.assertEqual((self.statuses(third)["backup"], self.statuses(third)["restore_check"]), ("done", "done"))

    def test_the_button_runs_the_same_sequence_in_the_serving_process(self):
        res = service.Resources(self.s, transport=FakeTransport())
        try:
            done = threading.Event()
            with mock.patch.dict(maintenance.STEP_FUNCTIONS, backup=reused, restore_check=reused, ingest=reused,
                                 fidelity=reused, keyword=reused, embedding=reused,
                                 regression=lambda *_, **__: done.wait(5) and {"status": "reused"}):
                run_id = service.start_maintenance(res, self.env.verifier)
                status = service.maintenance_status(res, self.env.verifier)
                self.assertEqual((status["running"], status["run"]["run_id"]), (True, run_id))
                with self.assertRaises(service.ServiceError):
                    service.start_maintenance(res, self.env.verifier)
                done.set()
                service._MAINTENANCE_JOB[0].join(10)
            status = service.maintenance_status(res, self.env.verifier)
            self.assertEqual((status["running"], status["run"]["status"]), (False, "complete"))
        finally:
            res.close()


if __name__ == "__main__":
    unittest.main()
