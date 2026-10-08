"""Updating the shared server to GitHub main from the header: the comparison with a faked GitHub, the status every
signed-in member reads, and the request that only writes the updater's marker, refused while paid work is open."""

import dataclasses
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from rfp_assistant import api
from rfp_assistant.service import service, update
from tests import fake_hub, fixtures

RUNNING, MIDDLE, LATEST = "a" * 40, "b" * 40, "c" * 40
REPO = update.REPO


class FakeGitHub:
    """Answers the two unauthenticated routes the watch reads; records every path asked."""

    def __init__(self, head: str = LATEST, compare: dict | None = None, compare_status: int = 200,
                 head_status: int = 200, down: bool = False) -> None:
        self.head, self.compare, self.compare_status, self.head_status, self.down = (
            head, compare, compare_status, head_status, down)
        self.paths: list[str] = []

    def __call__(self, path: str, accept: str) -> tuple[int, bytes]:
        self.paths.append(path)
        if self.down:
            raise OSError("unreachable")
        if path == f"/repos/{REPO}/commits/main":
            self.assertAccept(accept, "application/vnd.github.sha")
            return self.head_status, self.head.encode()
        if path.startswith(f"/repos/{REPO}/compare/"):
            return self.compare_status, json.dumps(self.compare or {}).encode()
        return 404, b"{}"

    @staticmethod
    def assertAccept(accept: str, expected: str) -> None:
        if accept != expected:
            raise AssertionError(accept)


def ahead(*commits: tuple[str, str]) -> dict:
    return {"status": "ahead", "ahead_by": len(commits), "behind_by": 0,
            "commits": [{"sha": sha, "commit": {"message": message}} for sha, message in commits]}


class CompareTest(unittest.TestCase):
    def test_main_ahead_of_the_running_commit_offers_the_titles_in_between(self):
        hub = FakeGitHub(compare=ahead((MIDDLE, "첫 번째 변경\n\n본문"), (LATEST, "두 번째 변경")))
        check = update.compare(RUNNING, hub)
        self.assertTrue(check.available)
        self.assertEqual((check.latest, check.ahead_by, check.note), (LATEST, 2, None))
        self.assertEqual(check.commits, [{"sha": MIDDLE, "title": "첫 번째 변경"}, {"sha": LATEST, "title": "두 번째 변경"}])
        self.assertEqual(hub.paths, [f"/repos/{REPO}/commits/main", f"/repos/{REPO}/compare/{RUNNING}...{LATEST}"])

    def test_the_same_commit_needs_no_comparison(self):
        hub = FakeGitHub(head=RUNNING)
        check = update.compare(RUNNING, hub)
        self.assertEqual((check.available, check.latest, check.commits, check.note), (False, RUNNING, [], None))
        self.assertEqual(len(hub.paths), 1)

    def test_no_fast_forward_no_offer(self):
        for name, hub, running in (
                ("diverged", FakeGitHub(compare={**ahead((LATEST, "x")), "status": "diverged"}), RUNNING),
                ("behind", FakeGitHub(compare={**ahead(), "status": "behind"}), RUNNING),
                ("not on GitHub", FakeGitHub(compare_status=404), RUNNING),
                ("unknown running commit", FakeGitHub(), "unknown"),
                ("main unreadable", FakeGitHub(head_status=403, head="rate limited"), RUNNING),
                ("malformed comparison", FakeGitHub(compare={"status": "ahead"}), RUNNING)):
            with self.subTest(name):
                check = update.compare(running, hub)
                self.assertFalse(check.available)
                self.assertTrue(check.note)

    def test_an_unreachable_github_keeps_the_last_check_and_says_so(self):
        hub = FakeGitHub(compare=ahead((LATEST, "변경")))
        watch = update.UpdateWatch(RUNNING, Path("r"), Path("s"), hub, poll_seconds=None)
        self.assertTrue(watch.refresh().available)
        hub.down = True
        check = watch.refresh()
        self.assertTrue(check.available)
        self.assertEqual(check.latest, LATEST)
        self.assertIn("GitHub에 연결하지 못했습니다", check.note)

    def test_the_watch_is_off_unless_both_paths_are_set(self):
        self.assertFalse(update.UpdateWatch.from_env(RUNNING, {}).configured)
        self.assertFalse(update.UpdateWatch.from_env(RUNNING, {update.REQUEST_ENV: "/x/requested"}).configured)
        watch = update.UpdateWatch.from_env(RUNNING, {update.REQUEST_ENV: "/x/requested", update.STATE_ENV: "/y"})
        self.assertTrue(watch.configured)
        self.assertEqual((watch.request, watch.state_dir, watch.repo), (Path("/x/requested"), Path("/y"), REPO))

    def test_the_updaters_result_is_read_and_a_run_that_never_finished_is_called_interrupted(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            watch = update.UpdateWatch(RUNNING, state / "requested", state, FakeGitHub(), poll_seconds=None)
            self.assertIsNone(watch.last_result())
            (state / "result.json").write_text(json.dumps({
                "state": "rolled_back", "from_commit": RUNNING, "to_commit": LATEST, "started_at": "2026-10-08T00:00:00+00:00",
                "finished_at": "2026-10-08T00:03:00+00:00", "message": "health check failed", "extra": "ignored"}))
            self.assertEqual(watch.last_result()["state"], "rolled_back")
            self.assertNotIn("extra", watch.last_result())
            self.assertFalse(watch.in_progress())
            (state / "result.json").write_text(json.dumps({"state": "running", "started_at": "2026-10-01T00:00:00+00:00"}))
            self.assertEqual(watch.last_result()["state"], "interrupted")
            (state / "result.json").write_text("not json")
            self.assertIsNone(watch.last_result())


class UpdateApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.env = fixtures.make_env(root / "env")
        self.res = service.Resources(self.env.settings)
        self.marker, self.state = root / "update-request" / "requested", root / "update-state"
        self.state.mkdir()
        self.watch = update.UpdateWatch(RUNNING, self.marker, self.state,
                                        FakeGitHub(compare=ahead((MIDDLE, "첫 번째"), (LATEST, "두 번째"))),
                                        poll_seconds=None)
        self.watch.refresh()
        self.app = api.create_app(self.res, fake_hub.login("김검토", "teammate"), self.watch)
        self.client = TestClient(self.app)
        self.client.__enter__()
        fake_hub.sign_in(self.client, "teammate")  # any allowlisted member: no extra role

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.res.close()
        self.tmp.cleanup()

    def test_every_signed_in_member_sees_the_update_and_its_titles(self):
        out = self.client.get("/api/update/status")
        self.assertEqual(out.status_code, 200, out.text)
        body = out.json()
        self.assertEqual((body["configured"], body["available"], body["in_progress"]), (True, True, False))
        self.assertEqual((body["running_commit"], body["latest_commit"], body["ahead_by"]), (RUNNING, LATEST, 2))
        self.assertEqual([c["title"] for c in body["commits"]], ["첫 번째", "두 번째"])
        self.assertEqual(body["paid_work"], {"open": False, "pending_micro_usd": 0, "open_attempts": 0,
                                             "active_requests": 0, "background_jobs": 0, "reason": None})
        self.assertIsNone(body["last_result"])

    def test_without_sign_in_nothing_is_shown_or_written(self):
        with TestClient(self.app) as stranger:
            self.assertEqual(stranger.get("/api/update/status").status_code, 401)
            self.assertEqual(stranger.post("/api/update", headers={"X-BidMate-Account": "teammate"}).status_code, 401)
        self.assertFalse(self.marker.exists())

    def test_confirming_writes_only_the_marker_and_ignores_any_branch_or_commit(self):
        out = self.client.post("/api/update", json={"branch": "evil", "commit": "d" * 40, "ref": "refs/heads/x"})
        self.assertEqual(out.status_code, 200, out.text)
        self.assertTrue(out.json()["in_progress"])
        marker = json.loads(self.marker.read_text(encoding="utf-8"))
        self.assertEqual(set(marker), {"requested_by", "requested_at", "running_commit"})
        self.assertEqual((marker["requested_by"], marker["running_commit"]), ("teammate", RUNNING))
        self.assertNotIn("evil", self.marker.read_text(encoding="utf-8"))
        self.assertNotIn("d" * 40, self.marker.read_text(encoding="utf-8"))
        with service.open_db(self.res.settings.db_path) as conn:
            audit = conn.execute("SELECT actor, target FROM audit_events WHERE action = 'request_update'").fetchall()
        self.assertEqual([(a["actor"], a["target"]) for a in audit], [("teammate", "main")])
        again = self.client.post("/api/update")
        self.assertEqual((again.status_code, again.json()["detail"]), (400, "업데이트가 이미 진행 중입니다."))

    def test_open_paid_work_refuses_the_update_until_it_ends(self):
        real = service.budget.snapshot
        release = threading.Event()
        job = threading.Thread(target=release.wait, daemon=True)

        def queued(open_: bool) -> None:
            with service.open_db(self.res.settings.db_path) as conn:
                if open_:
                    conn.execute("INSERT INTO requests(request_id, member_id, idempotency_key, input_hash, "
                                 "config_hash, scope_json, status, created_at, updated_at) "
                                 "VALUES ('r-open', 'teammate', 'k', 'h', 'c', '[]', 'running', '2026-10-08', "
                                 "'2026-10-08')")
                else:
                    conn.execute("DELETE FROM requests WHERE request_id = 'r-open'")

        def ledger(open_: bool) -> None:
            self.snapshot = (lambda db, today=None: dataclasses.replace(real(db), pending_micro_usd=1200,
                                                                       open_attempts=1)) if open_ else real

        def background(open_: bool) -> None:
            if open_:
                job.start()
                self.res._jobs.append(job)
            else:
                release.set()
                job.join(5)

        self.snapshot = real
        for name, toggle, reason in (("ledger", ledger, "유료 호출 1건 진행 중"), ("request", queued, "질문 1건 실행 중"),
                                     ("background job", background, "백그라운드 작업 1건 실행 중")):
            with self.subTest(name), mock.patch.object(service.budget, "snapshot",
                                                       side_effect=lambda *a, **k: self.snapshot(*a, **k)):
                toggle(True)
                status = self.client.get("/api/update/status").json()
                self.assertTrue(status["paid_work"]["open"])
                self.assertIn(reason, status["paid_work"]["reason"])
                refused = self.client.post("/api/update")
                self.assertEqual(refused.status_code, 400)
                self.assertIn("진행 중인 유료 작업이 끝나면 업데이트할 수 있습니다", refused.json()["detail"])
                self.assertFalse(self.marker.exists())
                toggle(False)
                self.assertFalse(self.client.get("/api/update/status").json()["paid_work"]["open"])  # on its own
        self.assertEqual(self.client.post("/api/update").status_code, 200)
        self.assertTrue(self.marker.exists())

    def test_a_server_without_the_updater_offers_nothing_and_refuses(self):
        bare = update.UpdateWatch(RUNNING, None, None, FakeGitHub(compare=ahead((LATEST, "x"))), poll_seconds=None)
        bare.refresh()
        with TestClient(api.create_app(self.res, fake_hub.login("teammate"), bare)) as client:
            fake_hub.sign_in(client, "teammate")
            self.assertFalse(client.get("/api/update/status").json()["available"])
            out = client.post("/api/update")
            self.assertEqual((out.status_code, out.json()["detail"]), (400, "이 서버에는 업데이트 장치가 설치되어 있지 않습니다."))

    def test_the_last_result_reaches_the_next_visitor(self):
        (self.state / "result.json").write_text(json.dumps({
            "state": "rolled_back", "from_commit": RUNNING, "to_commit": LATEST, "started_at": update._now(),
            "finished_at": update._now(), "message": "새 버전이 응답하지 않아 이전 커밋으로 되돌렸습니다."}), encoding="utf-8")
        with TestClient(self.app) as other:
            fake_hub.sign_in(other, "김검토")
            result = other.get("/api/update/status").json()["last_result"]
        self.assertEqual((result["state"], result["from_commit"], result["to_commit"]), ("rolled_back", RUNNING, LATEST))


if __name__ == "__main__":
    unittest.main()
