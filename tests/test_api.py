"""The HTTP API behind web/: it imports only `service` from the package, every route maps to one service call, and
the visitor name, request ownership and errors survive the trip over HTTP."""

import ast
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient

from rfp_assistant import api, service
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

    def test_a_question_needs_one_or_two_documents(self):
        self.assertEqual(self.client.post("/api/ask", json={"scope": [], "mode": "single"}).status_code, 422)

    def test_the_budget_lists_only_visible_warnings(self):
        b = self.client.get("/api/budget").json()
        self.assertEqual(b["warnings"], service.visible_warnings(b["snapshot"]["warnings"]))


if __name__ == "__main__":
    unittest.main()
