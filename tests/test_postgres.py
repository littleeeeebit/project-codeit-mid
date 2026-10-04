"""Real PostgreSQL checks on an isolated database per test (tests/fixtures.py)."""

from __future__ import annotations

import json
import hashlib
import os
import tempfile
import unittest
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import psycopg

from rfp_assistant import budget, cli, dense, generation, postgres, postgres_backup, service, store, vector_store
from rfp_assistant.settings import Settings
from tests import fixtures


class PostgreSQLTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = fixtures.make_env(self.root, paid=False)
        self.settings = self.env.settings
        self.target = self.settings.db_path
        self.admin = psycopg.connect(fixtures.server_dsn(), autocommit=True)
        self.owner = None

    def tearDown(self):
        if self.owner:
            self.owner.release()
        self.admin.close()
        self.tmp.cleanup()

    def validate_with_artifacts(self, passed=True):
        """What the final import's validation recorded: the references every startup re-checks."""
        with store.open_db(self.target) as conn, store.tx(conn):
            references = postgres_backup.references(conn)
            postgres.record_validation(conn, "fixture", references, passed)
        return references

    def test_startup_requires_the_validated_marker_and_unchanged_index_payloads(self):
        postgres.require_imported_database(self.target)
        reference = next(r for r in self.validate_with_artifacts() if r["kind"] == "index")
        manifest = Path(reference["path"])
        payload = manifest.parent / next(iter(json.loads(manifest.read_text(encoding="utf-8"))["files"]))
        original = payload.read_bytes()
        try:
            payload.write_bytes(original + b"corrupt")
            with self.assertRaisesRegex(RuntimeError, "artifacts"):
                postgres.require_imported_database(self.target)
        finally:
            payload.write_bytes(original)
        postgres.require_imported_database(self.target)

    def test_failed_validation_closes_paid_admission_and_blocks_startup(self):
        from datetime import date

        from rfp_assistant.settings import DEFAULT_RATES

        budget.configure(self.target, "owner", project_start=date(2026, 9, 30), project_end=date(2026, 10, 28),
                         prior_use_micro=0, prior_use_evidence="test", rates=DEFAULT_RATES, rate_version="t",
                         enable_paid=False)  # so only the validation can refuse paid admission below
        self.validate_with_artifacts()
        with store.open_db(self.target) as conn:
            conn.execute("UPDATE database_control SET paid_admission=true WHERE id=1")
        self.validate_with_artifacts(passed=False)
        with store.open_db(self.target) as conn:
            self.assertFalse(conn.execute("SELECT paid_admission FROM database_control WHERE id=1").fetchone()[0])
            self.assertEqual(postgres.owner_guard(conn), "postgresql_recovery_or_validation")
        with self.assertRaisesRegex(RuntimeError, "no validated import"):
            postgres.require_imported_database(self.target)
        with self.assertRaisesRegex(budget.BudgetError, "validated import"):
            budget.set_paid_enabled(self.target, "owner", True, "test")
        self.validate_with_artifacts()
        postgres.require_imported_database(self.target)

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

    def test_closed_paid_admission_cannot_dispatch(self):
        self.owner = postgres.GatewayOwner(self.target)
        with store.open_db(self.target) as conn:  # the ledger flag on, PostgreSQL admission still closed
            conn.execute("UPDATE budget_settings SET paid_enabled = 1")
        self.assertEqual(self.reserve(self.request("blocked"))["reason"], "postgresql_maintenance")
        with store.open_db(self.target) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM attempts").fetchone()[0], 0)

    def test_independent_connections_cap_duplicate_dispatch_and_exactly_once_settlement(self):
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

    def test_owner_exclusion_loss_and_restart_preserve_unknown_billing(self):
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
        target = postgres.Target(self.target.dsn_env, max_connections=4)
        with postgres.lifecycle(target) as shared:
            with postgres.lifecycle(target) as again:
                self.assertIs(again, shared)
                self.assertIs(shared, postgres._pools[target][0])
                with shared.connection() as first, shared.connection() as second, shared.connection() as third, shared.connection() as fourth:
                    with self.assertRaises(postgres.PoolTimeout):
                        with shared.connection(timeout=0.1):
                            pass
                    self.assertEqual(len({connection.info.backend_pid for connection in (first, second, third, fourth)}), 4)
            self.assertFalse(shared.closed)
        self.assertTrue(shared.closed)
        with self.assertRaisesRegex(RuntimeError, "no resource owner"):
            with store.open_db(target):
                pass

    def test_failed_resource_initialization_releases_owner_even_if_transport_close_fails(self):
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

    def test_service_rejects_unvalidated_database_without_initializing_it(self):
        empty = self.settings.with_(database_dsn_env=fixtures.database(ready=False))
        with self.assertRaisesRegex(RuntimeError, "no validated import"):
            service.Resources(empty, recover=True)
        with mock.patch.object(cli, "load_settings", return_value=empty):
            self.assertEqual(cli.main(["budget-status"]), 1)
        with store.open_db(empty.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pg_tables WHERE schemaname=current_schema()").fetchone()[0], 0)
        store.init_schema(empty.db_path)
        fixtures.mark_validated(empty.db_path)
        with store.open_db(empty.db_path) as conn, store.tx(conn, immediate=True):
            conn.execute("UPDATE migration_import SET state='importing' WHERE id=1")
        with self.assertRaisesRegex(RuntimeError, "no validated import"):
            service.Resources(empty)
        with store.open_db(empty.db_path) as conn, store.tx(conn, immediate=True):
            conn.execute("UPDATE migration_import SET state='complete' WHERE id=1")
        resource = service.Resources(empty)
        resource.close()

    def test_startup_without_a_dsn_fails_with_no_fallback(self):
        from rfp_assistant.settings import SettingsError, load_settings

        environment = {k: v for k, v in os.environ.items() if k not in ("RFP_DATABASE_DSN", "RFP_CONFIG_FILE")}
        environment["RFP_DATA_DIR"] = str(self.root / "startup")
        with mock.patch.dict("os.environ", environment, clear=True):
            with self.assertRaisesRegex(SettingsError, "RFP_DATABASE_DSN is required.*no fallback"):
                load_settings()
        self.assertNotIn("backend", " ".join(Settings.__dataclass_fields__))

    def test_startup_refuses_any_embedding_identity_but_large_1536(self):
        from rfp_assistant.settings import SettingsError, load_settings

        environment = {k: v for k, v in os.environ.items() if k != "RFP_CONFIG_FILE"}
        environment.update(RFP_DATA_DIR=str(self.root / "startup"), RFP_DATABASE_DSN=self.target.dsn())
        with mock.patch.dict("os.environ", environment, clear=True):
            settings = load_settings()
            self.assertEqual((settings.embedding_model, settings.embedding_dimensions), ("text-embedding-3-large", 1536))
            for model, dims in (("text-embedding-3-small", 1536), ("text-embedding-3-large", 768),
                                ("text-embedding-3-large", 3072)):
                with self.subTest(model=model, dims=dims), \
                        self.assertRaisesRegex(SettingsError, "text-embedding-3-large at 1536"):
                    load_settings(embedding_model=model, embedding_dimensions=dims)

    def test_pgvector_cache_separation_exact_filtered_parity_and_mixed_set_rejection(self):
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
        allowed = list(range(0, len(payloads), 2))
        scores = matrix[allowed] @ query  # brute-force cosine over unit rows, ties by chunk ID
        expected = sorted(((r, float(s)) for r, s in zip(allowed, scores)),
                          key=lambda p: (-p[1], payloads[p[0]]["chunk_id"]))[:20]
        actual = index.search(query, allowed, 20)
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
        with self.assertRaisesRegex(ValueError, "non-PostgreSQL SQL"):
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
