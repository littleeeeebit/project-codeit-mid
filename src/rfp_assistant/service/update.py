"""Updating the shared server to the latest GitHub main from the page (runbook 3.2).

The process records the commit it runs at startup and polls the public repository (no token) for main's head and
the commits in between. A signed-in member's request only writes a marker file; the root-owned units
`bidmate-update.path` and `bidmate-update.service` (tools/infra/) see it and run `tools/infra/update.sh`, which
fast-forwards the checkout, rebuilds, restarts `bidmate`, health-checks it and rolls back on failure. That script
writes `result.json` and `update.log` into a root-owned state directory, which this module only reads. Nothing
here runs git or systemctl, and no request carries a branch or commit: the script only ever takes origin/main.

Enabled only when the environment names both places (on `codeit`: /etc/bidmate/server.env):
BIDMATE_UPDATE_REQUEST (the marker file, in a directory the service user writes) and BIDMATE_UPDATE_STATE_DIR
(where the script writes its result).

The service side lives here too: what the banner reads (`update_status`), whether paid work is open
(`open_paid_work`) and the request itself (`request_update`). The fence it raises is checked at every paid admission
in service.py (`Resources.update_fence`).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from ..gateway import budget
from . import service
from .service import ServiceError

REQUEST_ENV = "BIDMATE_UPDATE_REQUEST"
STATE_ENV = "BIDMATE_UPDATE_STATE_DIR"
REPO_ENV = "BIDMATE_UPDATE_REPO"
GITHUB_ENV = "BIDMATE_UPDATE_GITHUB_API"  # tools/verify.py points this at a stand-in; the default is GitHub
REPO = "littleeeeebit/project-codeit-mid"
BRANCH = "main"
GITHUB_API = "https://api.github.com"
POLL_SECONDS = 300  # unauthenticated GitHub allows 60 calls an hour per address; a poll makes at most two
TIMEOUT_SECONDS = 10
TITLES = 50  # commit titles shown; the count is always exact
STALE_RUN_SECONDS = 30 * 60  # a "running" result older than this was cut off (the script never finished)
SHA = re.compile(r"[0-9a-f]{40}")
FINISHED = ("succeeded", "up_to_date", "failed", "rolled_back", "rollback_failed")


def github_fetch(api: str, path: str, accept: str) -> tuple[int, bytes]:
    """One unauthenticated GET; (status, body). Errors other than an HTTP status raise OSError."""
    request = Request(api.rstrip("/") + path, headers={"Accept": accept, "User-Agent": "bidmate-update-check"})
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - the configured GitHub API
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()


@dataclass
class Check:
    """The latest comparison of the running commit with main."""
    latest: str | None = None
    available: bool = False
    ahead_by: int = 0
    commits: list[dict] = field(default_factory=list)  # [{sha, title}], oldest first
    note: str | None = None  # why no update is offered although main differs, or why the check failed
    checked_at: str | None = None


def compare(running: str, fetch, repo: str = REPO, branch: str = BRANCH) -> Check:
    """main's head and the commits from `running` to it. `fetch(path, accept) -> (status, body)` raises OSError
    when GitHub cannot be reached."""
    now = _now()
    status, body = fetch(f"/repos/{repo}/commits/{quote(branch)}", "application/vnd.github.sha")
    latest = body.decode("ascii", "replace").strip()
    if status != 200 or not SHA.fullmatch(latest):
        return Check(note=f"GitHub에서 {branch} 브랜치를 읽지 못했습니다 (HTTP {status}).", checked_at=now)
    if not SHA.fullmatch(running):
        return Check(latest=latest, note="실행 중인 커밋을 알 수 없어 업데이트를 제안하지 않습니다.", checked_at=now)
    if latest == running:
        return Check(latest=latest, checked_at=now)
    status, body = fetch(f"/repos/{repo}/compare/{running}...{latest}", "application/vnd.github+json")
    if status == 404:
        return Check(latest=latest, note=f"실행 중인 커밋이 GitHub에 없습니다. {branch}로 빨리 감기할 수 없습니다.",
                     checked_at=now)
    try:
        data = json.loads(body) if status == 200 else None
        state, ahead = data["status"], int(data["ahead_by"])
        commits = [{"sha": c["sha"], "title": (c["commit"]["message"] or "").splitlines()[0][:200]}
                   for c in data["commits"]]
    except (ValueError, KeyError, TypeError, IndexError):
        return Check(latest=latest, note=f"GitHub 비교 결과를 읽지 못했습니다 (HTTP {status}).", checked_at=now)
    if state != "ahead":  # behind or diverged: a fast-forward to main is impossible
        return Check(latest=latest, note=f"실행 중인 커밋이 {branch}의 조상이 아닙니다 ({state}). 빨리 감기할 수 없습니다.",
                     checked_at=now)
    return Check(latest=latest, available=True, ahead_by=ahead, commits=commits[-TITLES:], checked_at=now)


class UpdateWatch:
    """The running commit, the last GitHub check, the request marker and the updater's last result."""

    def __init__(self, running: str, request: Path | None = None, state_dir: Path | None = None,
                 fetch=None, poll_seconds: float | None = POLL_SECONDS, repo: str = REPO) -> None:
        self.running, self.request, self.state_dir = running, request, state_dir
        self.repo = repo
        self.fetch = fetch or (lambda path, accept: github_fetch(GITHUB_API, path, accept))
        self.poll_seconds = poll_seconds
        self.check = Check()
        self.accepting = False  # a request is between its paid-work check and its marker (service.request_update)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @classmethod
    def from_env(cls, running: str, environ=os.environ) -> UpdateWatch:
        request, state = environ.get(REQUEST_ENV, "").strip(), environ.get(STATE_ENV, "").strip()
        api = environ.get(GITHUB_ENV, "").strip() or GITHUB_API
        return cls(running, Path(request) if request and state else None, Path(state) if request and state else None,
                   lambda path, accept: github_fetch(api, path, accept),
                   repo=environ.get(REPO_ENV, "").strip() or REPO)

    @property
    def configured(self) -> bool:
        return self.request is not None and self.state_dir is not None

    def refresh(self) -> Check:
        try:
            check = compare(self.running, self.fetch, self.repo)
        except OSError as exc:
            with self._lock:  # keep what was last known; say that it is not current
                previous = self.check
            check = Check(previous.latest, previous.available, previous.ahead_by, previous.commits,
                          f"GitHub에 연결하지 못했습니다: {type(exc).__name__}", previous.checked_at)
        with self._lock:
            self.check = check
        return check

    def start(self) -> None:
        """Polls GitHub in the background while the server runs; only a configured watch with an interval."""
        if not self.configured or not self.poll_seconds or self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.is_set():
                self.refresh()
                self._stop.wait(self.poll_seconds)

        self._thread = threading.Thread(target=loop, name="bidmate-update-check", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def last_result(self) -> dict | None:
        """The updater's result file, as far as it is well formed; never trusted beyond display."""
        if self.state_dir is None:
            return None
        try:
            data = json.loads((self.state_dir / "result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("state"), str):
            return None
        out = {k: str(data[k])[:500] if data.get(k) is not None else None
               for k in ("state", "from_commit", "to_commit", "started_at", "finished_at", "message")}
        if out["state"] == "running" and _age(out["started_at"]) > STALE_RUN_SECONDS:
            out["state"], out["message"] = "interrupted", ("업데이트 스크립트가 끝나지 않았습니다. 다음 업데이트가 "
                                                           "이어서 마무리하거나 되돌립니다.")
        return out

    def log_tail(self, lines: int = 40) -> str:
        if self.state_dir is None:
            return ""
        try:
            text = (self.state_dir / "update.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])

    def requested(self) -> bool:
        return self.request is not None and self.request.exists()

    def in_progress(self) -> bool:
        # The marker first: update.sh writes `running` before it removes the marker, so a marker found gone means
        # the result read after it already says running (or how the run ended). The other order can miss both.
        if self.requested():
            return True
        result = self.last_result()
        return bool(result and result["state"] == "running")

    def fenced(self) -> bool:
        """No new paid work may start: an update was accepted and has not ended without a restart. Read under the
        service's `_runner_lock`, which every paid admission and the acceptance itself hold."""
        return self.accepting or self.in_progress()

    def write_request(self, member: str) -> None:
        """The marker the path unit watches. Its content is informational: the updater reads nothing from it."""
        assert self.request is not None
        self.request.parent.mkdir(parents=True, exist_ok=True)
        part = self.request.with_name(self.request.name + ".part")
        part.write_text(json.dumps({"requested_by": member, "requested_at": _now(), "running_commit": self.running},
                                   ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(part, self.request)  # the path unit sees a complete file or none


def open_paid_work(res: service.Resources) -> dict:
    """Whether a restart now would cut off paid work: an open reservation or attempt in the ledger, a queued or
    running request, or a live background job (drafting, evaluation, judge run, maintenance).

    Read in the order work leaves them: threads and request slots, then request rows, then the ledger last. Work
    writes its ledger rows before its row finishes and its thread or slot ends, so whatever ended before its count
    has already left its open attempts for the ledger read; reading the ledger first could miss both."""
    with res._runner_lock:
        jobs = sum(1 for t in res._jobs if t.is_alive())
        admitted = res._runner.admitted if res._runner is not None else 0  # counted before its queued row exists
    try:
        with service.open_db(res.settings.db_path) as conn:
            active = conn.execute("SELECT COUNT(*) FROM requests WHERE status IN ('queued', 'running')").fetchone()[0]
        snap = budget.snapshot(res.settings.db_path)
    except (*service.DATABASE_ERRORS, budget.BudgetError) as exc:
        raise ServiceError(f"ledger_unavailable: {type(exc).__name__}") from None
    active = max(active, admitted)
    reasons = []
    if active:
        reasons.append(f"질문 {active}건 실행 중")
    if jobs:
        reasons.append(f"백그라운드 작업 {jobs}건 실행 중")
    if snap.unknown_micro_usd:
        reasons.append("결과를 모르는 유료 호출이 정산을 기다리는 중")
    elif snap.open_attempts or snap.pending_micro_usd > 0:
        reasons.append(f"유료 호출 {snap.open_attempts}건 진행 중")
    return {"open": bool(reasons), "pending_micro_usd": snap.pending_micro_usd, "open_attempts": snap.open_attempts,
            "active_requests": active, "background_jobs": jobs,
            "reason": ("진행 중인 유료 작업이 끝나면 업데이트할 수 있습니다: " + ", ".join(reasons)) if reasons else None}


def update_status(res: service.Resources, principal, watch: UpdateWatch) -> dict:
    """What the header's update banner shows. Every signed-in member may read and request it."""
    service._authorize(res, principal, "consultant", "verifier", "budget_admin")
    check = watch.check
    return {"configured": watch.configured, "running_commit": watch.running, "latest_commit": check.latest,
            "ahead_by": check.ahead_by, "commits": check.commits,
            "available": watch.configured and check.available, "checked_at": check.checked_at, "note": check.note,
            "paid_work": open_paid_work(res), "in_progress": watch.in_progress(), "last_result": watch.last_result()}


def request_update(res: service.Resources, principal, watch: UpdateWatch) -> dict:
    """Asks the root updater to move this server to the latest main. It takes no branch or commit: the updater
    fetches origin/main itself. Refused while paid work is open, so a restart never cuts one off."""
    principal = service._authorize(res, principal, "consultant", "verifier", "budget_admin")
    if not watch.configured:
        raise ServiceError("이 서버에는 업데이트 장치가 설치되어 있지 않습니다.")
    # The fence goes up before paid work is counted, under the lock every paid admission holds: work admitted
    # before it is counted and refuses the update, work after it is refused. The marker then keeps it up until the
    # updater ends without a restart; a restart starts a new process, fenced while its result still says running.
    with res._runner_lock:
        if watch.fenced():
            raise ServiceError("업데이트가 이미 진행 중입니다.")
        watch.accepting = True
    try:
        work = open_paid_work(res)
        if work["open"]:
            raise ServiceError(work["reason"])
        watch.write_request(principal.member_id)
    finally:
        watch.accepting = False
    with service.open_db(res.settings.db_path) as conn, service.tx(conn):
        service._audit(conn, principal.member_id, "request_update", "main", "header update button",
                       {"running_commit": watch.running, "latest_commit": watch.check.latest})
    return update_status(res, principal, watch)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _age(stamp: str | None) -> float:
    try:
        return time.time() - datetime.fromisoformat(stamp or "").timestamp()
    except ValueError:
        return float("inf")
