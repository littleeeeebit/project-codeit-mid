"""Browser regressions for screen-owned requests, using only isolated fixtures and a fake provider.

Build web/ first, then run with the verification environment's Python. These checks establish request
lifecycle behaviour, not actual-corpus answer quality or user acceptance.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "src"), str(REPO)]

import uvicorn
from playwright.sync_api import expect, sync_playwright

from rfp_assistant import api
from rfp_assistant.gateway.generation import FakeTransport
from rfp_assistant.service import service
from rfp_assistant.storage.store import open_db
from tests import fake_hub, fixtures

QUESTION = "하자보수 기간은 얼마인가요?"
MEMBERS = [f"alice-{i}" for i in range(20)]  # one fresh hub user per test, all on the allowlist


class AskOwnershipTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not api.WEB.is_dir():
            raise AssertionError("Run npm run build in web/ before this browser check")
        cls.tmp = tempfile.TemporaryDirectory(prefix="ask-ownership-")
        cls.env = fixtures.make_env(Path(cls.tmp.name))
        cls.gate, cls.entered = threading.Event(), threading.Event()
        cls.gate.set()
        cls.transport = FakeTransport()
        echo = cls.transport.responder

        def reply(messages):
            cls.entered.set()
            if not cls.gate.wait(30):
                raise AssertionError("fake provider was not released")
            return echo(messages)

        cls.transport.responder = reply
        cls.hold, cls.streamed = threading.Event(), threading.Event()
        cls.hold.set()
        chat = cls.transport.chat

        def held_chat(**kw):  # streams the whole answer, then holds the call open while `hold` is clear
            response = chat(**kw)
            if kw.get("on_delta"):
                cls.streamed.set()
                if not cls.hold.wait(30):
                    raise AssertionError("held provider was not released")
            return response

        cls.transport.chat = held_chat
        cls.res = service.Resources(cls.env.settings.with_(request_workers=1, request_admission=16),
                                    transport=cls.transport)
        cls.listener = socket.socket()
        cls.listener.bind(("127.0.0.1", 0))
        cls.origin = f"http://127.0.0.1:{cls.listener.getsockname()[1]}"
        cls.members = iter(MEMBERS)
        app = api.create_app(cls.res, service.Login.from_env(
            fake_hub.hub().env(f"{cls.origin}/api/auth/callback", MEMBERS)))
        cls.post_delay = 0

        @app.middleware("http")
        async def delay_ownership_response(request, call_next):
            response = await call_next(request)
            if request.url.path == "/api/ask":
                await asyncio.sleep(cls.post_delay)
            return response

        cls.server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        cls.thread = threading.Thread(target=cls.server.run, kwargs={"sockets": [cls.listener]}, daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 15
        while not cls.server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        if not cls.server.started:
            raise AssertionError("isolated fixture API did not start")
        cls.playwright = sync_playwright().start()
        chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
        cls.browser = cls.playwright.chromium.launch(**({"executable_path": str(chrome)} if chrome.exists() else {}))

    @classmethod
    def tearDownClass(cls):
        cls.gate.set()
        cls.hold.set()
        cls.browser.close()
        cls.playwright.stop()
        cls.server.should_exit = True
        cls.thread.join(timeout=15)
        cls.res.close()
        cls.listener.close()
        cls.tmp.cleanup()

    def setUp(self):
        self.gate.set()
        self.hold.set()
        self.streamed.clear()
        self.entered.clear()
        self.__class__.post_delay = 0
        self.member = next(self.members)
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        fake_hub.browser_sign_in(self.page, self.origin, self.member)
        self.pick_document()

    def tearDown(self):
        self.gate.set()
        self.hold.set()
        self.context.close()
        if self.res._runner is not None:
            for future in list(self.res._runner._futures.values()):
                future.result(timeout=15)

    def pick_document(self):
        self.page.get_by_role("checkbox").first.check()
        self.page.locator("#question").fill(QUESTION)

    def rows(self):
        with open_db(self.env.settings.db_path) as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM requests WHERE member_id = ? ORDER BY created_at",
                                                  (self.member,))]

    def wait_for(self, condition, message):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if condition():
                return
            self.page.wait_for_timeout(50)
        self.fail(message)

    def occupy_worker(self):
        self.gate.clear()
        self.entered.clear()
        doc = next(d for d in service.find_documents(self.res, service.visitor("worker")) if d["indexed"])
        service.ask(self.res, service.visitor("worker"), [(doc["doc_id"], doc["source_hash"])],
                    QUESTION, "single", "2026-09-30")
        self.assertTrue(self.entered.wait(15))

    def test_double_submit_during_the_initial_post_creates_one_request(self):
        self.gate.clear()
        self.__class__.post_delay = 1
        self.page.locator("main form").evaluate("form => { form.requestSubmit(); form.requestSubmit(); }")
        self.wait_for(lambda: len(self.rows()) >= 1, "initial request was not created")
        self.page.wait_for_timeout(1500)
        self.assertEqual(len(self.rows()), 1)

    def test_cancel_and_poll_ride_on_the_session_without_a_name_header(self):
        self.gate.clear()
        self.page.locator("main button[type=submit]").click()
        cancel = self.page.get_by_role("button", name="요청 취소", exact=True)
        cancel.wait_for()
        headers = []
        def observe(request):
            if "/api/requests/" in request.url:
                headers.append((request.method, request.headers.get("x-member")))
        self.page.on("request", observe)
        cancel.click()
        self.wait_for(lambda: self.rows()[0]["cancel_requested"] == 1, "the signed-in member could not cancel")
        self.wait_for(lambda: any(method == "GET" for method, _ in headers), "ownership was not polled")
        self.page.remove_listener("request", observe)
        self.assertTrue(headers and all(name is None for _, name in headers), headers)

    def test_a_tab_whose_cookie_another_tab_replaced_reloads_as_that_account_instead_of_acting_for_it(self):
        other = next(self.members)
        tab = self.context.new_page()  # the same browser: one cookie jar for both tabs
        tab.goto(self.origin + "/api/auth/login")
        tab.get_by_label("Username").fill(other)
        tab.get_by_role("button", name="Sign in").click()
        expect(tab.locator("header").get_by_text(other, exact=True)).to_be_visible(timeout=60000)
        # The first tab's next call (its budget poll, or a submit) names the old account, is refused with 409, and
        # the page reloads as the new one: the picked document and typed question are gone with the old screen.
        expect(self.page.locator("header").get_by_text(other, exact=True)).to_be_visible(timeout=15000)
        expect(self.page.get_by_role("checkbox").first).not_to_be_checked()
        expect(self.page.locator("#question")).to_have_count(0)
        with open_db(self.env.settings.db_path) as conn:
            made = conn.execute("SELECT COUNT(*) FROM requests WHERE member_id IN (?, ?)",
                                (self.member, other)).fetchone()[0]
        self.assertEqual(made, 0)  # refused before the service: neither account asked anything

    def test_cancelling_a_streaming_answer_hides_its_provisional_text_while_the_provider_still_runs(self):
        self.hold.clear()
        self.page.locator("main button[type=submit]").click()
        provisional = self.page.get_by_text("검증 전 임시 답변입니다", exact=False)
        expect(provisional).to_be_visible(timeout=15000)
        self.page.get_by_role("button", name="요청 취소", exact=True).click()
        self.wait_for(lambda: self.rows()[0]["cancel_requested"] == 1, "cancel was not recorded")
        expect(provisional).to_have_count(0, timeout=5000)  # the provider is still held
        expect(self.page.get_by_text("작성 중 · 검증 전")).to_have_count(0)
        self.assertEqual(self.rows()[0]["status"], "running")
        self.hold.set()
        self.wait_for(lambda: self.rows()[0]["status"] == "cancelled", "the held call did not finish as cancelled")
        expect(provisional).to_have_count(0)

    def test_requirement_rows_and_their_citation_numbers_follow_the_grouped_table(self):
        idx = self.res.index()
        with open_db(self.env.settings.db_path) as conn:
            for x in {xx for (xx, _) in idx.elements}:  # whichever document the screen picks first
                els = sorted((e for (xx, _), e in idx.elements.items() if xx == x), key=lambda e: e["source_order"])
                if len(els) < 3 or conn.execute("SELECT 1 FROM requirements WHERE extraction_id = ?", (x,)).fetchone():
                    continue
                for code, el in (("SFR-001", els[0]), ("PER-001", els[1]), ("SFR-002", els[2])):  # source order
                    conn.execute("INSERT INTO requirements VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 (idx.version, x, code, code, "detail", el["element_id"], code + " 이름"))
        self.page.get_by_role("radio", name="요구사항 목록").click()
        self.page.get_by_role("button", name="요구사항 목록 보기").click()
        table = self.page.get_by_role("table", name="요구사항 목록")
        expect(table).to_be_visible(timeout=15000)
        codes = table.get_by_role("button", name=re.compile(r" 원문 보기$"))
        self.assertEqual([c.inner_text() for c in codes.all()], ["SFR-001", "SFR-002", "PER-001"])
        codes.nth(2).click()
        pane = self.page.get_by_role("complementary", name="대화 근거", exact=True)
        expect(pane.get_by_role("heading", name="근거 3 원문 인용")).to_be_visible()

    def test_navigation_abandons_queued_work(self):
        self.occupy_worker()
        self.page.locator("main button[type=submit]").click()
        self.page.get_by_role("button", name="요청 취소", exact=True).wait_for()
        self.assertEqual(self.rows()[0]["status"], "queued")
        self.page.get_by_role("link", name="검증", exact=True).click()
        self.wait_for(lambda: self.rows()[0]["status"] == "cancelled", "navigation left a queued paid request")

    def test_navigation_before_the_initial_post_returns_abandons_its_late_ownership(self):
        self.occupy_worker()
        self.__class__.post_delay = 1
        self.page.locator("main button[type=submit]").click()
        self.wait_for(lambda: bool(self.rows()), "initial request was not created")
        self.page.get_by_role("link", name="검증", exact=True).click()
        self.wait_for(lambda: self.rows()[0]["status"] == "cancelled", "late initial response escaped navigation cleanup")

    def test_changing_the_documents_starts_a_new_conversation_and_detaches_its_answers(self):
        self.page.locator("main button[type=submit]").click()
        answer = self.page.locator('section[aria-label="답변"]')
        expect(answer.get_by_text(re.compile(r"^요청 [0-9a-f]{8} · "))).to_be_visible(timeout=15000)
        self.page.locator("#question").fill("사업 예산과 부가가치세 조건은 무엇인가요?")
        expect(answer).to_have_count(1)  # typing the next question keeps the conversation
        self.page.get_by_role("checkbox").first.uncheck()
        expect(answer).to_have_count(0)
        self.page.get_by_role("button", name="내 최근 요청").click()
        expect(self.page.get_by_role("button", name=QUESTION, exact=False)).to_be_visible()

    def test_a_follow_up_shows_its_standalone_question_and_opens_markers_in_the_shared_pane(self):
        self.page.locator("main button[type=submit]").click()
        latest = self.page.get_by_role("region", name="답변", exact=True)
        expect(latest.get_by_text(re.compile(r"^요청 [0-9a-f]{8} · "))).to_be_visible(timeout=15000)
        self.page.locator("#question").fill("그 기간은 언제부터 계산하나요?")
        self.page.get_by_role("button", name="이어서 질문 · 유료 2회").click()
        expect(self.page.get_by_role("region", name="질문 1의 답변")).to_be_visible()
        expect(self.page.get_by_text(re.compile("^검색에 쓴 질문 · "))).to_be_visible(timeout=15000)
        marker = latest.get_by_role("button", name=re.compile(r"^근거 \d+ 원문 보기$")).first
        marker.click()
        pane = self.page.get_by_role("complementary", name="대화 근거", exact=True)
        expect(pane.get_by_role("heading", name=re.compile(r"^근거 \d+ 원문 인용$"))).to_be_visible()
        expect(pane.locator("blockquote")).to_be_visible()
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(json.loads(rows[1]["request_json"])["previous_request_id"], rows[0]["request_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
