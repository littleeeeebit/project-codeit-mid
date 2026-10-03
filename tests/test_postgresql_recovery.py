"""Native pg_dump/pg_restore acceptance with isolated databases and no provider calls."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from rfp_assistant import budget, migration, postgres, postgres_backup, store
from tests import fixtures


@unittest.skipUnless(os.environ.get("RFP_POSTGRES_TEST_DSN"), "isolated PostgreSQL DSN is required")
class PostgreSQLRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = fixtures.make_env(self.root / "live")
        self.admin = psycopg.connect(os.environ["RFP_POSTGRES_TEST_DSN"], autocommit=True)
        self.databases = []
        self.keys = []
        self.original_restore = os.environ.get("RFP_RESTORE_DATABASE_DSN")
        self.source, self.settings = self.new_database("source")
        self.lease = store.database_lifecycle(self.source)
        self.lease.__enter__()
        plan = self.root / "plan.json"
        migration.plan(self.env.settings.db_path, plan)
        migration.import_snapshot(plan, self.source)
        self.assertTrue(migration.validate(plan, self.source)["pass"])
        with store.open_db(self.source) as conn:
            conn.execute("UPDATE database_control SET paid_admission=true WHERE id=1")
        self.manifest = Path(postgres_backup.backup(self.settings, self.root / "backup", "test")["manifest"])
        self.target, _ = self.new_database("restore")
        os.environ["RFP_RESTORE_DATABASE_DSN"] = self.target.dsn()

    def new_database(self, purpose):
        name = "bidmate_recovery_test_" + purpose + "_" + uuid.uuid4().hex[:12]
        self.admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        self.databases.append(name)
        key = "RFP_RECOVERY_TEST_" + uuid.uuid4().hex
        self.keys.append(key)
        os.environ[key] = make_conninfo(os.environ["RFP_POSTGRES_TEST_DSN"], dbname=name)
        with psycopg.connect(os.environ[key], autocommit=True) as conn:
            conn.execute("CREATE EXTENSION vector")
        return postgres.Target(key), self.env.settings.with_(database_backend="postgresql", database_dsn_env=key)

    def tearDown(self):
        self.lease.__exit__(None, None, None)
        if self.original_restore is None:
            os.environ.pop("RFP_RESTORE_DATABASE_DSN", None)
        else:
            os.environ["RFP_RESTORE_DATABASE_DSN"] = self.original_restore
        for key in self.keys:
            os.environ.pop(key, None)
        for name in reversed(self.databases):
            assert name.startswith("bidmate_recovery_test_")
            self.admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        self.admin.close()
        self.tmp.cleanup()

    def cli_recovery(self, primary_dsn):
        config = self.root / "config.json"
        config.write_text(json.dumps({"database_backend": "postgresql", "provider": "fake",
                                      "database_timeout_seconds": 1}), encoding="utf-8")
        environment = os.environ.copy()
        environment["RFP_CONFIG_FILE"] = str(config)
        environment["RFP_DATA_DIR"] = str(self.settings.data_dir)
        environment["RFP_SOURCE_DIR"] = str(self.settings.source_dir)
        if primary_dsn is None:
            environment.pop("RFP_DATABASE_DSN", None)
        else:
            environment["RFP_DATABASE_DSN"] = primary_dsn
        result = subprocess.run([sys.executable, "-m", "rfp_assistant.cli", "restore-check", "--backup",
                                 str(self.manifest)], env=environment, capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["passed"])
        with store.database_lifecycle(self.target), store.open_db(self.target) as conn:
            postgres.require_imported_database(self.target)
            self.assertFalse(conn.execute("SELECT paid_admission FROM database_control WHERE id=1").fetchone()[0])

    def test_cli_recovers_with_unreachable_primary_database(self):
        self.cli_recovery("postgresql://bidmate@127.0.0.1:1/lost_database")

    def test_cli_recovers_without_primary_dsn(self):
        self.cli_recovery(None)

    def test_backup_of_verified_recovery_excludes_its_control_fence(self):
        with store.open_db(self.source) as conn:
            conn.execute("CREATE SCHEMA bidmate_recovery")
            conn.execute("CREATE TABLE bidmate_recovery.control (id bigint PRIMARY KEY, state text NOT NULL)")
            conn.execute("INSERT INTO bidmate_recovery.control VALUES (1,'verified')")
        manifest = Path(postgres_backup.backup(self.settings, self.root / "backup-again", "test")["manifest"])
        report = postgres_backup.restore_check(self.settings, manifest)
        self.assertTrue(report["passed"])
        with store.database_lifecycle(self.target), store.open_db(self.target) as conn:
            postgres.require_imported_database(self.target)
            self.assertFalse(conn.execute("SELECT paid_admission FROM database_control WHERE id=1").fetchone()[0])

    def test_failed_artifact_check_keeps_restore_target_fenced(self):
        artifact = Path(json.loads(self.manifest.read_text(encoding="utf-8"))["references"][0]["path"])
        artifact.unlink()
        report = postgres_backup.restore_check(self.settings, self.manifest)
        self.assertFalse(report["passed"])
        with store.database_lifecycle(self.target), store.open_db(self.target) as conn:
            self.assertFalse(conn.execute("SELECT paid_admission FROM database_control WHERE id=1").fetchone()[0])
            with self.assertRaisesRegex(RuntimeError, "recovery"):
                postgres.require_imported_database(self.target)

    def test_restore_owner_loss_during_validation_cannot_publish_ready(self):
        receipt = self.manifest.parent / "postgresql-restore-check.json"
        receipt.write_text(json.dumps({"passed": True}), encoding="utf-8")
        original = migration.references_valid
        terminated = False
        def lose_owner(references):
            nonlocal terminated
            if not terminated:
                with psycopg.connect(self.target.dsn(), autocommit=True) as other:
                    with self.assertRaises(store.LockHeld):
                        postgres.GatewayOwner(self.target)
                    for lock in (postgres.GATEWAY_LOCK, migration.IMPORT_LOCK):
                        pid = other.execute("SELECT pid FROM pg_locks WHERE locktype='advisory' "
                                            "AND classid=%s AND objid=%s AND granted",
                                            (lock >> 32, lock & 0xFFFFFFFF)).fetchone()[0]
                        self.assertIsNotNone(pid)
                    other.execute("SELECT pg_terminate_backend(%s)", (pid,))
                terminated = True
            return original(references)
        with mock.patch.object(migration, "references_valid", side_effect=lose_owner):
            with self.assertRaises(psycopg.Error):
                postgres_backup.restore_check(self.settings, self.manifest)
        self.assertFalse(receipt.exists(), "failed restore must not leave a success receipt")
        with store.database_lifecycle(self.target), store.open_db(self.target) as conn:
            self.assertTrue(postgres.recovery_blocked(conn))
            self.assertFalse(conn.execute("SELECT paid_admission FROM database_control WHERE id=1").fetchone()[0])
            with self.assertRaisesRegex(RuntimeError, "recovery"):
                postgres.require_imported_database(self.target)

    def test_native_restore_error_rolls_back_dump_but_keeps_durable_fence(self):
        original_run = postgres_backup._run
        def conflicting_restore(program, target, args, log_dir):
            # A real DDL conflict occurs inside pg_restore, after the empty-target check.
            with psycopg.connect(target.dsn(), autocommit=True) as other:
                other.execute("CREATE TABLE attempts (conflict text)")
            return original_run(program, target, args, log_dir)
        with mock.patch.object(postgres_backup, "_run", side_effect=conflicting_restore):
            with self.assertRaisesRegex(RuntimeError, "pg_restore failed"):
                postgres_backup.restore_check(self.settings, self.manifest)
        with psycopg.connect(self.target.dsn(), autocommit=True) as other:
            self.assertIsNone(other.execute("SELECT to_regclass('public.database_control')").fetchone()[0])
            self.assertTrue(postgres.recovery_blocked(other))

    def test_success_receipt_requires_committed_readiness(self):
        receipt = self.manifest.parent / "postgresql-restore-check.json"
        original_write = store.write_text_atomic
        writes = []
        def check_committed(path, text):
            if path == receipt:
                with psycopg.connect(self.target.dsn(), autocommit=True) as other:
                    state = other.execute("SELECT c.paid_admission,m.state,v.passed,r.state "
                                          "FROM database_control c JOIN migration_import m USING(id) "
                                          "JOIN migration_validation v USING(id) "
                                          "JOIN bidmate_recovery.control r USING(id)").fetchone()
                self.assertEqual(state, (False, "complete", True, "verified"))
                writes.append(json.loads(text)["passed"])
            return original_write(path, text)
        with mock.patch.object(store, "write_text_atomic", side_effect=check_committed):
            self.assertTrue(postgres_backup.restore_check(self.settings, self.manifest)["passed"])
        self.assertEqual(writes, [True])
        self.assertTrue(json.loads(receipt.read_text(encoding="utf-8"))["passed"])

    def test_receipt_write_failure_blocks_target_and_leaves_no_success(self):
        receipt = self.manifest.parent / "postgresql-restore-check.json"
        original_write = store.write_text_atomic
        def failed_write(path, text):
            if path == receipt:
                raise OSError("injected receipt write failure")
            return original_write(path, text)
        with mock.patch.object(store, "write_text_atomic", side_effect=failed_write):
            with self.assertRaisesRegex(OSError, "receipt write failure"):
                postgres_backup.restore_check(self.settings, self.manifest)
        self.assertFalse(receipt.exists())
        with store.database_lifecycle(self.target), store.open_db(self.target) as conn:
            self.assertFalse(conn.execute("SELECT paid_admission FROM database_control WHERE id=1").fetchone()[0])
            with self.assertRaisesRegex(RuntimeError, "recovery"):
                postgres.require_imported_database(self.target)

    def test_restore_failure_after_copied_control_rows_never_admits_paid_work(self):
        original_run = postgres_backup._run
        def interrupted(program, *args):
            original_run(program, *args)
            if program == "pg_restore":
                raise RuntimeError("injected failure after copied control rows")
        with mock.patch.object(postgres_backup, "_run", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                postgres_backup.restore_check(self.settings, self.manifest)
        with store.database_lifecycle(self.target), store.open_db(self.target) as conn:
            self.assertFalse(conn.execute("SELECT paid_admission FROM database_control WHERE id=1").fetchone()[0])
            with self.assertRaisesRegex(RuntimeError, "recovery|restore|validation"):
                postgres.require_imported_database(self.target)
            # Copied admission/complete flags alone must never override the durable recovery fence.
            conn.execute("UPDATE database_control SET paid_admission=true WHERE id=1")
            conn.execute("UPDATE migration_import SET state='complete' WHERE id=1")
            owner = postgres.GatewayOwner(self.target)
            try:
                with self.assertRaisesRegex(RuntimeError, "recovery|restore|validation"):
                    postgres.require_owner(self.target)
            finally:
                owner.release()
