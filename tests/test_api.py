"""The HTTP API behind web/: it imports only `service` from the package, every route maps to one service call, and
the visitor name, request ownership and errors survive the trip over HTTP."""

import ast
import dataclasses
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from fastapi.testclient import TestClient

from rfp_assistant import api
from rfp_assistant.service import service
from tests import fixtures

API = Path(api.__file__)
INTERNALS = {"retrieval", "generation", "store", "budget", "answers", "gold", "drafting", "evaluation", "auth",
             "ingestion", "dense", "settings"}


class ApiBoundaryTest(unittest.TestCase):
    def test_the_api_imports_only_the_service_from_the_package(self):
        tree = ast.parse(API.read_text(encoding="utf-8"))
        package = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level:
                package |= {node.module} if node.module else {a.name for a in node.names}
        self.assertEqual(package, {"service"})
        used = {n.attr for n in ast.walk(tree)
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "service"}
        self.assertFalse(used & INTERNALS, used & INTERNALS)

    def test_the_committed_schema_matches_the_api(self):
        """web/ builds its TypeScript types from this file; regenerate it with `python tools/openapi.py`."""
        import importlib.util

        spec = importlib.util.spec_from_file_location("openapi_tool", API.parents[2] / "tools" / "openapi.py")
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        self.assertEqual(tool.TARGET.read_text(encoding="utf-8"), tool.schema_text(),
                         "web/openapi.json is stale: run python tools/openapi.py")


class ApiFlowTest(unittest.TestCase):
    def test_shared_budget_limit_is_exact_attributed_and_does_not_enable_paid(self):
        before = self.client.get("/api/budget").json()["snapshot"]
        out = self.client.put("/api/budget/limit", headers=self.headers,
                              json={"cap_micro_usd": 10_000_000, "reason": "personal API"})
        self.assertEqual(out.status_code, 200, out.text)
        snap = out.json()["snapshot"]
        self.assertEqual(snap["cap_micro_usd"], 10_000_000)
        self.assertEqual(snap["spent_micro_usd"], before["spent_micro_usd"])
        self.assertEqual(snap["paid_enabled"], before["paid_enabled"])
        for invalid in (0, -1, 0.5, True, "100"):
            self.assertEqual(self.client.put("/api/budget/limit", json={"cap_micro_usd": invalid, "reason": "test"}).status_code, 422)
        self.assertEqual(self.client.put("/api/budget/limit", json={"cap_micro_usd": 10_000_000, "reason": " "}).status_code, 400)

    def test_an_api_key_belongs_to_the_browser_that_entered_it_and_is_never_shown(self):
        secret = "sk-test-never-shown-0123456789"
        with mock.patch.object(service, "read_api_key", return_value=None), \
                mock.patch.object(service.tracing.Tracing, "from_settings", return_value=None):
            res = service.Resources(dataclasses.replace(self.env.settings, provider="openai"))
        with service.open_db(res.settings.db_path) as conn:  # a ledger configured before gpt-5-mini was selectable
            rates = json.loads(conn.execute("SELECT rates_json FROM budget_settings").fetchone()[0])
            conn.execute("UPDATE budget_settings SET rates_json = ?", (json.dumps(
                {k: v for k, v in rates.items() if k != "gpt-5-mini"}),))
        try:
            app = api.create_app(res)
            owner, teammate = TestClient(app), TestClient(app)  # two browsers: separate cookie jars
            for browser in (owner, teammate):
                browser.__enter__()
                self.addCleanup(browser.__exit__, None, None, None)
            self.assertEqual(owner.get("/api/settings/api-key", headers=self.headers).json()["configured"], False)
            with mock.patch.object(service.generation, "check_api_key", return_value=("OpenAI가 이 키를 거부했습니다.", None)):
                refused = owner.put("/api/settings/api-key", headers=self.headers,
                                    json={"api_key": secret, "model": "gpt-5-mini"})
            self.assertEqual(refused.status_code, 400)
            self.assertIsNone(res.transport)
            self.assertEqual(owner.put("/api/settings/api-key", headers=self.headers,
                                       json={"api_key": secret, "model": "gpt-4o"}).status_code, 400)
            with mock.patch.object(service.generation, "check_api_key", return_value=(None, "proj_owner")) as checked:
                out = owner.put("/api/settings/api-key", headers=self.headers,
                                json={"api_key": secret, "model": "gpt-5-mini"})
            self.assertEqual(out.status_code, 200, out.text)
            self.assertEqual(checked.call_args.args[1], "gpt-5-mini")  # the key is checked against the chosen model
            self.assertEqual((out.json()["configured"], out.json()["set_by"], out.json()["model"]),
                             (True, "김검토", "gpt-5-mini"))
            with service.open_db(res.settings.db_path) as conn:  # its price entered the ledger, audited
                rates = json.loads(conn.execute("SELECT rates_json FROM budget_settings").fetchone()[0])
                registered = conn.execute("SELECT COUNT(*) FROM audit_events "
                                          "WHERE action = 'register_generation_rate'").fetchone()[0]
            self.assertEqual((rates["gpt-5-mini"], registered), (service.budget.DEFAULT_RATES["gpt-5-mini"], 1))
            cookie = out.headers["set-cookie"]
            self.assertIn("httponly", cookie.lower())
            self.assertEqual(owner.get("/api/settings/api-key").json()["configured"], True)
            seen = teammate.get("/api/settings/api-key").json()
            self.assertEqual((seen["configured"], seen["set_by"], seen["model"]),
                             (False, None, res.settings.generation_model))
            self.assertEqual(teammate.put("/api/settings/model", json={"model": "gpt-5-nano"}).status_code, 400)
            with mock.patch.object(res.transport, "check_model", return_value=None):
                changed = owner.put("/api/settings/model", headers=self.headers, json={"model": "gpt-5-nano"})
            self.assertEqual(changed.json()["model"], "gpt-5-nano", changed.text)
            session = owner.cookies[api.KEY_COOKIE]
            reset = service.bind_request(res, session)  # a request the owner's browser submits now
            try:
                self.assertEqual((res.paid_refusal(), res.generation_model(), service.budget.BILLING_SCOPE.get()),
                                 ("", "gpt-5-nano", "proj_owner"))
                with mock.patch.object(res.transport, "check_model", return_value=None):
                    owner.put("/api/settings/model", headers=self.headers, json={"model": "gpt-5-mini"})
                self.assertEqual(res.generation_model(), "gpt-5-nano")  # a submitted request keeps its model
            finally:
                reset()
            self.assertEqual(res.generation_model(), res.settings.generation_model)
            self.assertEqual(res.paid_refusal(), service.generation.NO_API_KEY)  # no session: the teammate
            with self.assertRaises(service.generation.ProviderError) as refused_call:
                res.transport.chat(model="m", messages=[], response_format=None, max_completion_tokens=1,
                                   reasoning_effort=None)
            self.assertTrue(refused_call.exception.pre_execution)  # refused before dispatch: nothing charged
            with service.open_db(res.settings.db_path) as conn:
                rows = [dict(r) for r in conn.execute("SELECT * FROM audit_events WHERE action = 'set_api_key'")]
            self.assertEqual(len(rows), 1)
            for shown in (out.text, cookie, refused.text, owner.get("/api/settings/api-key").text, repr(rows)):
                self.assertNotIn(secret, shown)
        finally:
            res.close()

    def test_an_evaluation_answers_with_its_recorded_model_whatever_the_browser_chose(self):
        from rfp_assistant.service import answers

        pinned = answers.PinnedResources(self.env.settings, None, {"mode": "kiwi_bm25"})
        token = service.generation.REQUEST_MODEL.set("gpt-5-nano")  # the browser that started the run chose nano
        try:
            self.assertEqual(self.res.generation_model(), "gpt-5-nano")
            self.assertEqual(pinned.generation_model(), self.env.settings.generation_model)
        finally:
            service.generation.REQUEST_MODEL.reset(token)
            pinned.close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = fixtures.make_env(Path(self.tmp.name))
        self.res = service.Resources(self.env.settings)
        self.client = TestClient(api.create_app(self.res))
        self.client.__enter__()
        self.headers = {"X-Member": quote("김검토")}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.res.close()
        self.tmp.cleanup()

    def wait(self, rid: str, **params) -> dict:
        end = time.monotonic() + 10
        while time.monotonic() < end:
            out = self.client.get(f"/api/requests/{rid}", params=params, headers=self.headers).json()
            if out["view"]["status"] not in ("queued", "running"):
                return out
            time.sleep(0.05)
        raise AssertionError("request did not finish")

    def test_a_free_question_is_owned_attached_and_attributed(self):
        docs = self.client.get("/api/documents", headers=self.headers).json()
        self.assertEqual([d["indexed"] for d in docs], sorted((d["indexed"] for d in docs), reverse=True))
        doc = next(d for d in docs if d["indexed"])
        body = {"scope": [{"doc_id": doc["doc_id"], "source_hash": doc["source_hash"]}], "mode": "metadata"}
        owned = self.client.post("/api/ask", json=body, headers=self.headers).json()
        out = self.wait(owned["request_id"], generation_id=owned["generation_id"], target=owned["target"])
        self.assertTrue(out["attachable"])
        self.assertEqual((out["view"]["member_id"], out["view"]["status"]), ("김검토", "completed"))
        self.assertTrue(out["view"]["result"]["facts"])
        stale = self.wait(owned["request_id"], generation_id="another", target=owned["target"])
        self.assertFalse(stale["attachable"])
        mine = self.client.get("/api/requests", headers=self.headers).json()
        self.assertEqual([r["request_id"] for r in mine], [owned["request_id"]])
        self.assertEqual(self.client.post(f"/api/requests/{owned['request_id']}/abandon",
                                          headers=self.headers).status_code, 204)  # finished: nothing to cancel
        other = self.client.post(f"/api/requests/{owned['request_id']}/cancel", headers={"X-Member": "someone"})
        self.assertEqual(other.status_code, 403)
        missing = self.client.get(f"/api/requests/{owned['request_id']}/evidence/E9", headers=self.headers)
        self.assertEqual((missing.status_code, missing.json()["detail"]), (400, "요청에 없는 근거 ID입니다."))

    def test_an_original_downloads_under_its_managed_name(self):
        doc = next(d for d in self.client.get("/api/documents").json() if d["indexed"])
        got = self.client.get(f"/api/originals/{doc['doc_id']}/{doc['source_hash']}")
        self.assertEqual(got.status_code, 200)
        self.assertIn("filename*=UTF-8''", got.headers["content-disposition"])
        self.assertTrue(got.content)
        self.assertEqual(self.client.get(f"/api/originals/{doc['doc_id']}/bad").status_code, 400)

    def test_a_scoped_question_needs_one_or_two_documents(self):  # only mode "corpus" asks without a scope
        got = self.client.post("/api/ask", json={"question": "하자보수 기간은?", "scope": [], "mode": "single"})
        self.assertIn(got.status_code, (400, 422))

    def test_a_trace_is_frozen_generated_once_and_corrected_by_its_ids(self):
        sources = self.client.get("/api/verify/trace-sources", headers=self.headers).json()
        doc = sources["documents"][0]
        body = {"question": "하자보수 기간은 얼마인가요?", "mode": sources["modes"][0],
                "scope": [{"doc_id": doc["doc_id"], "source_hash": doc["source_hash"]}]}
        run = self.client.post("/api/verify/traces", json=body, headers=self.headers).json()
        self.assertEqual((run["member_id"], run["generation_block"]), ("김검토", None))
        self.assertEqual(self.client.get(f"/api/verify/traces/{run['run_id']}").json()["run_id"], run["run_id"])
        first, again = (self.client.post(f"/api/verify/traces/{run['run_id']}/generate", headers=self.headers)
                        for _ in range(2))
        self.assertEqual(first.json(), again.json())  # one idempotent request per run
        ev = run["retrieval"]["evidence"][0]
        quote = self.res.index().elements[(ev["extraction_id"], ev["element_ids"][0])]["raw_text"][:20]
        fix = {"run_id": run["run_id"], "evidence_id": ev["evidence_id"], "element_id": ev["element_ids"][0],
               "reason": "기대 근거 위치 수정", "quote": quote}
        wrong = self.client.post("/api/verify/corrections", json={**fix, "evidence_id": "E99"}, headers=self.headers)
        self.assertEqual(wrong.status_code, 400)
        cid = self.client.post("/api/verify/corrections", json=fix, headers=self.headers).json()["id"]
        [row] = self.client.get("/api/verify/corrections").json()
        self.assertEqual((row["reviewer"], row["run_id"]), ("김검토", run["run_id"]))
        self.assertTrue(cid)
        overview = self.client.get("/api/verify/overview").json()
        self.assertEqual(overview["evaluation"]["answer_runs"], [])

    def test_a_run_whose_configuration_changed_cannot_generate(self):
        sources = self.client.get("/api/verify/trace-sources").json()
        doc = sources["documents"][0]
        run = service.run_trace(self.res, service.visitor("v"), "하자보수", [(doc["doc_id"], doc["source_hash"])],
                                "2026-09-30", sources["modes"][0])
        with mock.patch.object(service, "resolve_verifier_config", side_effect=service.ServiceError("바뀜")):
            self.assertEqual(service.run_generation_block(self.res, service.visitor("v"), run["run_id"]), "바뀜")

    def test_the_budget_lists_only_visible_warnings(self):
        b = self.client.get("/api/budget").json()
        self.assertEqual(b["warnings"], service.visible_warnings(b["snapshot"]["warnings"]))

    def test_the_fidelity_overview_loads(self):
        # PostgreSQL refuses ungrouped columns at planning time, so this fails without HWP rows too.
        r = self.client.get("/api/verify/fidelity", headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsInstance(r.json(), list)

    def test_saved_source_review_appears_in_history_without_a_second_write(self):
        doc = next(d for d in self.client.get("/api/documents").json() if d["indexed"])
        source = doc["source_hash"]
        automatic = {"findings_json": '[{"side":"extraction","page":1}]', "method": "test"}
        with mock.patch.object(service.fidelity, "latest", return_value=automatic):
            saved = self.client.post(f"/api/verify/fidelity/{source}/confirm", json={"note": "Checked page 1"},
                                     headers=self.headers)
        self.assertEqual(saved.status_code, 200)
        history = self.client.get("/api/verify/history", headers=self.headers).json()
        event = next(e for e in history if e["event_id"] == saved.json()["id"])
        self.assertEqual((event["reviewer"], event["action"], event["note"]),
                         ("김검토", "sample_checked", "Checked page 1"))
        self.assertEqual(event["locations"], [{"side": "extraction", "page": 1, "element_id": None, "cell": None}])
        self.assertEqual(self.client.get("/api/verify/history", headers=self.headers).json(), history)
        self.assertEqual(self.client.get("/api/verify/history?limit=0").status_code, 422)

    def test_history_includes_development_decisions_but_never_sealed_questions(self):
        from rfp_assistant.storage.store import open_db

        with open_db(self.env.settings.db_path) as conn:
            for dataset in ("dev", "test"):
                conn.execute("INSERT INTO gold_candidates(candidate_id,batch_id,batch_sha256,dataset,row_json,"
                             "row_sha256,question_key,drafted_by,submitted_at) VALUES (?,?,?,?,?,?,?,?,?)",
                             (dataset, "batch", "sha", dataset, '{"question":"' + dataset + ' question"}',
                              "sha", dataset, "drafter", "2026-10-03"))
                conn.execute("INSERT INTO gold_reviews(review_id,candidate_id,reviewer,kind,decision,note,created_at) "
                             "VALUES (?,?,?,?,?,?,?)", (dataset, dataset, "reviewer", "decision", "approve",
                                                       "Reviewed original", "2026-10-03"))
            conn.execute("INSERT INTO gold_candidates(candidate_id,batch_id,batch_sha256,dataset,row_json,"
                         "row_sha256,question_key,drafted_by,submitted_at,status,decided_by,decided_at,reject_json) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", ("legacy", "batch", "sha", "dev",
                         '{"question":"Legacy question"}', "sha", "legacy", "drafter", "2026-10-03",
                         "rejected", "reviewer", "2026-10-03", '{"categories":["too_easy"],"note":""}'))
        history = self.client.get("/api/verify/history").json()
        decisions = [e for e in history if e["kind"] == "gold"]
        self.assertCountEqual([(e["target"], e["action"], e["note"]) for e in decisions],
                              [("dev question", "decision:approve", "Reviewed original"),
                               ("Legacy question", "decision:reject", "너무 쉬움 (앞부분·목차 수준)")])
        self.assertEqual(len(self.client.get("/api/verify/history?limit=1").json()), 1)


if __name__ == "__main__":
    unittest.main()
