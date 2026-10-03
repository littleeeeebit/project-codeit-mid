"""Real PostgreSQL checks; opt in with RFP_POSTGRES_TEST_DSN pointing at an isolated database."""

from __future__ import annotations

import json
import hashlib
import os
import tempfile
import unittest
import uuid
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from rfp_assistant import budget, dense, generation, migration, postgres, service, store, vector_store
from tests import fixtures


@unittest.skipUnless(os.environ.get("RFP_POSTGRES_TEST_DSN"), "isolated PostgreSQL DSN is required")
class PostgreSQLTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = fixtures.make_env(self.root)
        self.schema = "pgtest_" + uuid.uuid4().hex
        self.admin = psycopg.connect(os.environ["RFP_POSTGRES_TEST_DSN"], autocommit=True)
        if self.admin.execute("SELECT 1 FROM pg_tables WHERE schemaname='public' LIMIT 1").fetchone():
            self.admin.close()
            self.tmp.cleanup()
            raise RuntimeError("integration checks require an isolated database with no public application tables")
        self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.key = "RFP_TEST_" + uuid.uuid4().hex
        os.environ[self.key] = make_conninfo(os.environ["RFP_POSTGRES_TEST_DSN"],
                                            options=f"-csearch_path={self.schema},public")
        self.target = postgres.Target(self.key, max_connections=4)
        self.settings = self.env.settings.with_(database_backend="postgresql", database_dsn_env=self.key,
                                                database_pool_max=4)
        self.plan = self.root / "plan.json"
        migration.plan(self.env.settings.db_path, self.plan)
        self.owner = None
        self.lease = store.database_lifecycle(self.target)
        self.lease.__enter__()

    def tearDown(self):
        if self.owner:
            self.owner.release()
        self.lease.__exit__(None, None, None)
        self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))
        self.admin.close()
        os.environ.pop(self.key, None)
        self.tmp.cleanup()

    def imported(self):
        return migration.import_snapshot(self.plan, self.target)

    def allow_fake_paid(self):
        self.owner = postgres.GatewayOwner(self.target)
        with store.open_db(self.target) as conn:
            conn.execute("UPDATE database_control SET paid_admission=true WHERE id=1")
            conn.execute("UPDATE budget_settings SET paid_enabled=1,cap_micro_usd=100,allowance_micro_usd=100,"
                         "envelopes_json=? WHERE id=1", (store.dumps({"interactive": 100}),))

    def request(self, key):
        return dense.ensure_job_request(self.settings, "member", key, {"test": key})

    def reserve(self, request):
        return budget.reserve(self.target, request_id=request, member_id="member", stage="generation",
                              purpose="interactive", model=self.settings.generation_model, input_tokens=800,
                              max_output_tokens=0, count_method="test")

    def test_snapshot_import_resume_repeat_preserves_all_records_and_rejects_changes(self):
        first = migration.import_snapshot(self.plan, self.target, batch_size=1, max_batches=3)
        self.assertEqual(first["status"], "checkpointed")
        self.assertEqual(self.imported()["status"], "complete")
        self.assertTrue(migration.validate(self.plan, self.target)["pass"])
        self.assertEqual(self.imported()["status"], "already_complete")
        with store.open_db(self.target) as conn:
            conn.execute("INSERT INTO app_settings VALUES ('newer','do not overwrite')")
        self.assertEqual(self.imported()["overwritten_rows"], 0)
        with store.open_db(self.target) as conn:
            self.assertEqual(store.get_app_setting(conn, "newer"), "do not overwrite")
        changed = self.root / "changed.json"
        manifest = json.loads(self.plan.read_text(encoding="utf-8"))
        manifest["created_at"] = "changed"
        store.write_text_atomic(changed, json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "changed plan"):
            migration.import_snapshot(changed, self.target)

    def test_unrelated_target_is_rejected_and_rehearsal_cannot_dispatch(self):
        with store.open_db(self.target) as conn:
            conn.execute("CREATE TABLE unrelated (id bigint)")
        with self.assertRaisesRegex(ValueError, "unrelated"):
            self.imported()
        with store.open_db(self.target) as conn:
            conn.execute("DROP TABLE unrelated")
        self.imported()
        self.owner = postgres.GatewayOwner(self.target)
        with self.assertRaisesRegex(RuntimeError, "disabled for rehearsal"):
            self.reserve(self.request("blocked"))

    def test_independent_connections_cap_duplicate_dispatch_and_exactly_once_settlement(self):
        self.imported()
        self.allow_fake_paid()
        requests = [self.request(str(i)) for i in range(12)]
        with ThreadPoolExecutor(max_workers=6) as executor:
            admissions = list(executor.map(self.reserve, requests))
        admitted = [a for a in admissions if a["admitted"]]
        self.assertEqual(len(admitted), 1)
        attempt = admitted[0]["attempt_id"]
        budget.mark_dispatching(self.target, attempt)
        with self.assertRaises(budget.BudgetError):
            budget.mark_dispatching(self.target, attempt)
        usage = {"prompt_tokens": 800, "completion_tokens": 0}
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: budget.settle(self.target, attempt, usage, "response"), range(2)))
        self.assertEqual(sorted(r["duplicate"] for r in results), [False, True])
        self.assertEqual(budget.snapshot(self.target).spent_micro_usd, 80)
        self.assertEqual(budget.snapshot(self.target).pending_micro_usd, 0)
        self.assertEqual(budget.snapshot(self.env.settings.db_path).spent_micro_usd, 0)

    def test_owner_exclusion_loss_and_restart_preserve_unknown_billing(self):
        self.imported()
        self.allow_fake_paid()
        with self.assertRaises(store.LockHeld):
            postgres.GatewayOwner(self.target)
        attempt = self.reserve(self.request("before-loss"))["attempt_id"]
        budget.mark_dispatching(self.target, attempt)
        self.admin.execute("SELECT pg_terminate_backend(%s)", (self.owner.conn.info.backend_pid,))
        transport = generation.OpenAITransport.__new__(generation.OpenAITransport)
        transport._owner_check = self.owner.check
        transport._client = mock.Mock()
        with self.assertRaises(generation.ProviderError) as stopped:
            transport.embed(model="text-embedding-3-large", inputs=["test"], dimensions=768)
        self.assertTrue(stopped.exception.pre_execution)
        transport._client.embeddings.create.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "ownership was lost"):
            self.reserve(self.request("after-loss"))
        self.owner.release()
        self.owner = postgres.GatewayOwner(self.target)
        budget.recover(self.target)
        snapshot = budget.snapshot(self.target)
        self.assertEqual(snapshot.unknown_micro_usd, 100)
        self.assertEqual(snapshot.pending_micro_usd, 100)

    def test_registered_vector_storage_and_index_limits(self):
        with store.open_db(self.target) as conn:
            conn.execute("CREATE TABLE native_vectors (v vector(3072))")
            vector = np.ones(3072, dtype=np.float32)
            vector /= np.linalg.norm(vector)
            conn.execute("INSERT INTO native_vectors VALUES (?)", (vector,))
            self.assertEqual(conn.execute("SELECT vector_dims(v) FROM native_vectors").fetchone()[0], 3072)
            with self.assertRaises(psycopg.errors.ProgramLimitExceeded):
                conn.execute("CREATE INDEX native_hnsw ON native_vectors USING hnsw (v vector_cosine_ops)")
            with self.assertRaises(psycopg.errors.ProgramLimitExceeded):
                conn.execute("CREATE INDEX native_ivf ON native_vectors USING ivfflat (v vector_cosine_ops)")
            conn.execute("CREATE TABLE storage_limit (v vector(16000))")
            conn.execute("CREATE TABLE reduced_vectors (v vector(2000))")
            conn.execute("CREATE INDEX reduced_hnsw ON reduced_vectors USING hnsw (v vector_cosine_ops)")
            conn.execute("CREATE INDEX reduced_ivf ON reduced_vectors USING ivfflat (v vector_cosine_ops)")

    def test_pool_is_bounded_shared_and_closed_with_last_resource_owner(self):
        with postgres.lifecycle(self.target) as shared:
            self.assertIs(shared, postgres._pools[self.target][0])
            with shared.connection() as first, shared.connection() as second, shared.connection() as third, shared.connection() as fourth:
                with self.assertRaises(postgres.PoolTimeout):
                    with shared.connection(timeout=0.1):
                        pass
                self.assertEqual(len({connection.info.backend_pid for connection in (first, second, third, fourth)}), 4)
        self.assertFalse(shared.closed)
        self.lease.__exit__(None, None, None)
        self.assertTrue(shared.closed)
        with self.assertRaisesRegex(RuntimeError, "no resource owner"):
            with store.open_db(self.target):
                pass
        # Re-enter the fixture's lease so tearDown owns and closes exactly one resource.
        self.lease = store.database_lifecycle(self.target)
        self.lease.__enter__()

    def test_failed_resource_initialization_releases_owner_even_if_transport_close_fails(self):
        self.imported()
        def fail(resource, settings, transport, recover):
            resource._lock = postgres.GatewayOwner(settings.db_path)
            resource.transport = mock.Mock()
            resource.transport.close.side_effect = RuntimeError("close failed")
            raise ValueError("initialization failed")
        with mock.patch.object(service.Resources, "_initialize", fail):
            with self.assertRaisesRegex(RuntimeError, "close failed"):
                service.Resources(self.settings)
        self.assertEqual(postgres._pools[self.target][2], 1)
        self.owner = postgres.GatewayOwner(self.target)
        self.owner.check()

    def test_service_uses_postgresql_for_search_evidence_and_review_writes(self):
        self.imported()
        res = service.Resources(self.settings, recover=True)
        try:
            projects = service.search_projects(res, self.env.consultant, {}, "하자보수")
            self.assertTrue(projects)
            self.assertIsInstance(res.settings.db_path, postgres.Target)
            index = res.index()
            self.assertIsNotNone(index)
            with store.open_db(self.target) as conn:
                self.assertEqual(index.version, store.get_app_setting(conn, "active_index"))
            ref = self.env.refs[fixtures.ROWS[0][10][:3]]
            result = service.retrieve(res, self.env.consultant, "하자보수 기간", [ref])
            self.assertTrue(result.evidence)
            original = service.original_download(res, self.env.consultant, ref.doc_id, ref.source_hash)
            self.assertEqual(original.data, fixtures.PDF_A)
            run = service.verifier_trace(res, self.env.verifier, "하자보수 기간", [ref], "2026-09-30")
            evidence = run["retrieval"]["evidence"][0]
            element_id = evidence["element_ids"][0]
            quote = index.elements[(evidence["extraction_id"], element_id)]["raw_text"][:20]
            correction = service.record_correction(
                res, self.env.verifier, reason="PostgreSQL citation check", quote=quote,
                evidence={"doc_id": ref.doc_id, "source_hash": ref.source_hash,
                          "extraction_id": evidence["extraction_id"], "element_id": element_id},
                proposal={}, run_id=run["run_id"])
            self.assertEqual(service.list_corrections(res, self.env.verifier)[0]["correction_id"], correction)
        finally:
            res.close()

    def test_pgvector_cache_separation_exact_filtered_parity_and_mixed_set_rejection(self):
        self.imported()
        settings = self.settings.with_(embedding_dimensions=64)
        with store.open_db(self.target) as conn:
            version = store.get_app_setting(conn, "active_index")
        row, payloads = dense.index_payloads(settings, version)
        rng = np.random.default_rng(20261003)
        vectors = {}
        for item in payloads:
            vector = vectors.setdefault(item["payload_hash"], dense.unit_vector(rng.normal(size=64), 64))
            dense.cache_put(settings, item["payload_hash"], vector,
                            {"construction": "synthetic-storage-parity", "normalized_payload_sha256":
                             hashlib.sha256(item["text"].encode()).hexdigest()})
        large = settings.with_(embedding_model="text-embedding-3-large")
        first = payloads[0]
        self.assertIsNone(dense.cache_get(large, dense.payload_hash(first["text"], large.embedding_model, 64)))
        result = dense.publish_dense(settings, row, payloads)
        index = dense.DenseIndex.load(settings, result["dense_version"])
        self.assertIsInstance(index, vector_store.PgDenseIndex)
        query = dense.unit_vector(rng.normal(size=64), 64)
        matrix = np.stack([vectors[p["payload_hash"]] for p in payloads])
        baseline = dense.DenseIndex(index.version, version, settings.embedding_model, 64, matrix,
                                    [p["chunk_id"] for p in payloads])
        allowed = list(range(0, len(payloads), 2))
        expected, actual = baseline.search(query, allowed, 20), index.search(query, allowed, 20)
        self.assertEqual([p[0] for p in actual], [p[0] for p in expected])
        np.testing.assert_allclose([p[1] for p in actual], [p[1] for p in expected], atol=1e-6)
        self.assertTrue(all(p[0] in allowed for p in actual))
        self.assertEqual(index.search(query, [], 20), [])
        index.settings = large
        with self.assertRaisesRegex(dense.DenseError, "mixed model"):
            index.search(query, allowed, 20)
        with store.open_db(self.target) as conn:
            conn.execute("UPDATE embedding_payloads SET model='text-embedding-3-large' WHERE payload_hash=?",
                         (first["payload_hash"],))
        with self.assertRaisesRegex(dense.DenseError, "mixes model"):
            dense.DenseIndex.load(settings, index.version)

    def test_large_query_explicit_dimensions_normalization_and_pool_release(self):
        from rfp_assistant.generation import FakeTransport

        self.imported()
        self.allow_fake_paid()
        settings = self.settings.with_(embedding_model="text-embedding-3-large", embedding_dimensions=1536)
        transport = FakeTransport()
        request = self.request("large-query")
        vector, info = dense.query_vector(settings, transport, "dimension dispatch", request_id=request,
                                         member_id="member", purpose="interactive", allow_paid=True)
        self.assertIsNotNone(vector)
        self.assertAlmostEqual(float(np.linalg.norm(vector)), 1, places=6)
        self.assertEqual((transport.embed_calls[0]["model"], transport.embed_calls[0]["dimensions"]),
                         ("text-embedding-3-large", 1536))
        self.assertEqual(info["status"], "ok")
        cached, _ = dense.query_vector(settings, transport, "dimension dispatch", allow_paid=False,
                                      request_id=None, member_id="member", purpose="interactive")
        np.testing.assert_array_equal(cached, vector)
        self.assertEqual(len(transport.embed_calls), 1)
        with store.open_db(self.target) as conn:
            self.assertFalse(conn.raw.info.transaction_status)

    def test_envelope_reallocation_keeps_money_and_open_reservations(self):
        self.imported()
        self.allow_fake_paid()
        self.reserve(self.request("reserved"))
        before = budget.snapshot(self.target)
        with self.assertRaisesRegex(budget.BudgetError, "open reservations"):
            budget.set_envelopes(self.target, "owner", {"embedding": 40, "gold_eval": 0, "interactive": 60}, "test")
        budget.set_envelopes(self.target, "owner", {"embedding": 0, "gold_eval": 0, "interactive": 100}, "test")
        after = budget.snapshot(self.target)
        self.assertEqual((after.spent_micro_usd, after.pending_micro_usd, after.cap_micro_usd),
                         (before.spent_micro_usd, before.pending_micro_usd, before.cap_micro_usd))

    def test_historical_activation_cannot_override_selected_large_dimensions(self):
        self.imported()
        settings = self.settings.with_(embedding_model="text-embedding-3-large", embedding_dimensions=1536)
        res = service.Resources(settings)
        try:
            for model, dimensions in (("text-embedding-3-small", 1536), ("text-embedding-3-large", 768)):
                with store.open_db(self.target) as conn, store.tx(conn, immediate=True):
                    store.set_app_setting(conn, "active_run", store.dumps({"mode": "hybrid", "dense_version": "historical",
                        "embedding": {"model": model, "dims": dimensions}}))
                serving = res.serving()
                self.assertEqual(serving["mode"], "kiwi_bm25")
                self.assertEqual(serving["fallback_reason"], "activated_embedding_identity_requires_migration")
                self.assertEqual((res.run_settings().embedding_model, res.run_settings().embedding_dimensions),
                                 ("text-embedding-3-large", 1536))
                self.assertFalse(res.transport.embed_calls)
        finally:
            res.close()

    def test_limit_change_preserves_unknown_reservations_and_price_history(self):
        self.imported()
        self.allow_fake_paid()
        aid = self.reserve(self.request("limit-reservation"))["attempt_id"]
        budget.mark_dispatching(self.target, aid)
        budget.mark_unknown(self.target, aid, "timeout")
        with store.open_db(self.target) as conn:
            original = dict(conn.execute("SELECT * FROM attempts WHERE attempt_id=?", (aid,)).fetchone())
        with self.assertRaisesRegex(budget.BudgetError, "open reservations"):
            budget.set_limit(self.target, "owner", 99, "too low")
        budget.set_limit(self.target, "owner", 10_000_000, "personal API approval")
        with store.open_db(self.target) as conn:
            previous_rates = json.loads(conn.execute("SELECT rates_json FROM budget_settings").fetchone()[0])
        budget.register_large_rate(self.target, "owner", "approved paid embedding work")
        snap = budget.snapshot(self.target)
        self.assertEqual((snap.cap_micro_usd, snap.pending_micro_usd, snap.unknown_micro_usd),
                         (10_000_000, 100, 100))
        with store.open_db(self.target) as conn:
            self.assertEqual(dict(conn.execute("SELECT * FROM attempts WHERE attempt_id=?", (aid,)).fetchone()), original)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM audit_events WHERE action='set_limit'").fetchone()[0], 1)
            rates = json.loads(conn.execute("SELECT rates_json FROM budget_settings").fetchone()[0])
            self.assertEqual({key: rates[key] for key in previous_rates}, previous_rates)
            self.assertEqual(rates["text-embedding-3-large"]["input"], "0.13")


class SQLBoundaryTests(unittest.TestCase):
    def test_placeholders_literals_percent_and_row_contract(self):
        self.assertEqual(postgres.bind_sql("SELECT '?' AS q WHERE key=? AND key LIKE 'a%'"),
                         "SELECT '?' AS q WHERE key=%s AND key LIKE 'a%%'")
        row = postgres.Row(["id", "value"], (1, "é 한국어"))
        self.assertEqual(row[0], 1)
        self.assertEqual(dict(row), {"id": 1, "value": "é 한국어"})
        self.assertEqual(list(row), [1, "é 한국어"])
        with self.assertRaisesRegex(ValueError, "SQLite-only"):
            postgres.bind_sql("PRAGMA user_version")

    def test_native_large_shortening_preserves_source_and_rejects_small(self):
        original = dense.unit_vector(np.arange(1, 3073), 3072)
        preserved = original.copy()
        reduced, provenance = dense.shorten_large_reference(original, 768, model="text-embedding-3-large")
        self.assertEqual(reduced.shape, (768,))
        self.assertAlmostEqual(float(np.linalg.norm(reduced)), 1, places=6)
        np.testing.assert_array_equal(original, preserved)
        self.assertEqual(provenance["source_dimensions"], 3072)
        with self.assertRaises(dense.DenseError):
            dense.shorten_large_reference(original, 768, model="text-embedding-3-small")
