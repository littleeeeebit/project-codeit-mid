"""Phase 3 operational checks and the phase-3 handoff report.

`load_check` runs N concurrent members against one temporary ledger and index with a delayed (and optionally
failing) fake transport. It never touches production state and never needs a key.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from ..contracts import AnswerRequest, Principal
from ..gateway import budget
from ..gateway.generation import FakeTransport, ProviderError
from ..settings import REPO_ROOT, Settings
from ..storage import store
from . import service

PHASE3_DIR = ("releases", "phase-3")


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))], 1)


def load_check(users: int = 6, requests_per_user: int = 2, delay_seconds: float = 0.5, fail_every: int = 0,
               affordable: int = 4) -> dict:
    """Six (or `users`) members submit concurrently against a cap that admits only `affordable` maximum
    reservations at once. Checks: the cap is never exceeded by spent + pending (sampled continuously by a
    read-only poller), each request has at most one generation attempt, no attempt is left open, and polling
    stays responsive while calls are in flight."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from tests import fixtures  # the same temporary corpus the automated gate uses

    calls = {"n": 0}
    lock = threading.Lock()

    class Transport(FakeTransport):
        def chat(self, **kw):
            with lock:
                calls["n"] += 1
                n = calls["n"]
            if fail_every and n % fail_every == 0:
                time.sleep(delay_seconds)
                raise ProviderError("APITimeoutError (injected)", pre_execution=False)
            return super().chat(**kw)

    with tempfile.TemporaryDirectory() as tmp:
        env = fixtures.make_env(Path(tmp))
        settings: Settings = env.settings.with_(request_workers=6, request_admission=max(12, users * 2))
        transport = Transport(delay_seconds=delay_seconds)
        res = service.Resources(settings, transport=transport, recover=True)
        try:
            ref = env.refs["기관A"]
            question = "하자보수 기간은 얼마인가요?"
            est = service.prepare_answer(res, env.consultant, question, [ref], "2026-09-30")["estimate_micro_usd"]
            cap = affordable * est + est // 2
            with store.open_db(settings.db_path) as conn:
                conn.execute("UPDATE budget_settings SET cap_micro_usd = ?", (cap,))
            members = [Principal(f"member-{i + 1}", frozenset({"consultant"})) for i in range(users)]
            violations: list[dict] = []
            poll_ms: list[float] = []
            stop = threading.Event()

            def poller():  # what every open browser's budget strip does, read-only
                while not stop.is_set():
                    t0 = time.perf_counter()
                    snap = service.budget_snapshot(res, members[0])
                    poll_ms.append((time.perf_counter() - t0) * 1000)
                    if snap.spent_micro_usd + snap.pending_micro_usd > snap.cap_micro_usd:
                        violations.append({"spent": snap.spent_micro_usd, "pending": snap.pending_micro_usd})
                    time.sleep(0.05)

            submitted: list[tuple[Principal, str, float]] = []
            submit_ms: list[float] = []
            barrier = threading.Barrier(users)

            def member(p: Principal):
                barrier.wait()
                for _ in range(requests_per_user):
                    key = str(uuid.uuid4())
                    t0 = time.perf_counter()
                    rid = service.submit_answer(res, p, AnswerRequest(key, key, question, [ref], as_of="2026-09-30"))
                    service.submit_answer(res, p, AnswerRequest(key, key, question, [ref], as_of="2026-09-30"))
                    submit_ms.append((time.perf_counter() - t0) * 1000)
                    with lock:
                        submitted.append((p, rid, time.perf_counter()))

            poll_thread = threading.Thread(target=poller, daemon=True)
            poll_thread.start()
            started = time.perf_counter()
            threads = [threading.Thread(target=member, args=(p,)) for p in members]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            views = {}
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                views = {rid: service.request_status(res, p, rid) for p, rid, _ in submitted}
                if all(v.status not in ("queued", "running") for v in views.values()):
                    break
                time.sleep(0.05)
            wall = time.perf_counter() - started
            stop.set()
            poll_thread.join(2)
            snap = budget.snapshot(settings.db_path)
            with store.open_db(settings.db_path) as conn:
                per_request = conn.execute("SELECT request_id, COUNT(*) FROM attempts WHERE stage = 'generation' "
                                           "GROUP BY request_id HAVING COUNT(*) > 1").fetchall()
                states = dict(conn.execute("SELECT state, COUNT(*) FROM attempts GROUP BY state").fetchall())
            outcomes: dict[str, int] = {}
            for v in views.values():
                key = f"{v.status}/{v.result.status if v.result else '-'}"
                outcomes[key] = outcomes.get(key, 0) + 1
            unfinished = [rid for rid, v in views.items() if v.status in ("queued", "running")]
            checks = {
                "cap_never_exceeded": not violations,
                "one_generation_attempt_per_request": not per_request,
                "no_open_reservation_left": snap.pending_micro_usd == snap.unknown_micro_usd,
                "all_requests_finished": not unfinished,
                "duplicate_submit_reused_request": len(submitted) == users * requests_per_user
                and len({rid for _, rid, _ in submitted}) == users * requests_per_user,
            }
            return {
                "kind": "fake_provider_load_check", "synthetic": True, "users": users,
                "requests_per_user": requests_per_user, "fake_delay_seconds": delay_seconds,
                "fail_every": fail_every, "cap_micro_usd": cap, "max_reservation_micro_usd": est,
                "affordable_at_once": affordable, "outcomes": outcomes, "attempt_states": states,
                "provider_calls": calls["n"], "spent_micro_usd": snap.spent_micro_usd,
                "pending_unknown_micro_usd": snap.unknown_micro_usd,
                "submit_ms": {"p50": _pct(submit_ms, 0.5), "p95": _pct(submit_ms, 0.95)},
                "poll_ms": {"samples": len(poll_ms), "p50": _pct(poll_ms, 0.5), "p95": _pct(poll_ms, 0.95),
                            "max": round(max(poll_ms), 1) if poll_ms else None},
                "wall_seconds": round(wall, 2), "violations": violations[:5], "checks": checks,
                "passed": all(checks.values()),
            }
        finally:
            res.close()


def phase3_dir(settings: Settings) -> Path:
    return settings.data_dir.joinpath(*PHASE3_DIR)


def save_json(path: Path, data: dict) -> None:
    store.write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=1))


def write_phase3_report(settings: Settings) -> Path:
    """Report from recorded state only. Missing evidence (no load check, no browser run, no paid smoke) is
    written as missing, never as a pass."""
    out = phase3_dir(settings)
    out.mkdir(parents=True, exist_ok=True)
    budget.ensure_budget_row(settings.db_path)  # idempotent, paid-disabled; never resets an existing ledger

    def load(name: str) -> dict | None:
        p = out / name
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    with store.open_db(settings.db_path) as conn:
        schema = store.schema_version(conn)
        requests = dict(conn.execute("SELECT status, COUNT(*) FROM requests WHERE request_json IS NOT NULL "
                                     "GROUP BY status").fetchall())
        attempts = [dict(r) for r in conn.execute(
            "SELECT purpose, stage, state, COUNT(*) AS n, COALESCE(SUM(settled_micro_usd), 0) AS settled, "
            "COALESCE(SUM(reserved_micro_usd), 0) AS reserved FROM attempts GROUP BY purpose, stage, state")]
        active_index = store.get_app_setting(conn, "active_index")
        envelopes = json.loads(conn.execute("SELECT envelopes_json FROM budget_settings WHERE id = 1").fetchone()[0])
        used = {p: budget._purpose_used(conn, p) for p in envelopes}
    snap = budget.snapshot(settings.db_path)
    load_result, browser, smoke = load("load-check.json"), load("browser-results.json"), load("paid-smoke.json")
    host = load("team-host.json")  # written by the owner on the team host (runbook section 3)
    usd = lambda m: f"${m / 1_000_000:,.6f}"  # noqa: E731
    lines = ["# Phase 3 report", "",
             f"Generated {store.utcnow()} from `{settings.data_dir.name}` state (schema {schema}). Only recorded "
             "results appear below; a missing section means the check has not been run on this host.", "",
             "## Configuration", "",
             f"- Serving: {service.describe_serving(service.active_serving(settings))}; "
             f"active keyword index `{active_index}`.",
             "- Access: no login (owner decision); the sidebar name only attributes requests and actions.",
             (f"- Team host: {host['host']}, recorded {host['recorded_at']}. Reach: {host['reach']}. Tunnel: "
              f"`{host['tunnel']}`. Members: {', '.join(host['members'])}." if host else
              "- Team host: not recorded (`team-host.json`, runbook section 3)."),
             f"- Executor: {settings.request_workers} workers, {settings.request_admission} admitted unfinished "
             "requests.", "",
             "## Budget", "",
             f"- Spent {usd(snap.spent_micro_usd)} of {usd(snap.allowance_micro_usd)} ({snap.spent_percent:.2f}%); "
             f"pending {usd(snap.pending_micro_usd)} (unknown {usd(snap.unknown_micro_usd)}); cap "
             f"{usd(snap.cap_micro_usd)}; paid enabled {snap.paid_enabled}; warnings {snap.warnings or 'none'}.",
             "- Remaining envelopes: " + ", ".join(f"{k} {usd(v - used[k])} of {usd(v)}" for k, v in envelopes.items()),
             "", "| purpose | stage | state | attempts | settled | reserved |", "| --- | --- | --- | --- | --- | --- |"]
    lines += [f"| {a['purpose']} | {a['stage']} | {a['state']} | {a['n']} | {usd(a['settled'])} | {usd(a['reserved'])} |"
              for a in attempts] or ["| - | - | - | 0 | - | - |"]
    lines += ["", f"Requests by status: {requests or 'none'}.", "", "## Fake-provider concurrency and recovery", ""]
    if load_result:
        lines += [f"- Synthetic load check ({load_result['users']} users × {load_result['requests_per_user']}, delay "
                  f"{load_result['fake_delay_seconds']} s, fail every {load_result['fail_every'] or 'never'}): "
                  f"{'PASSED' if load_result['passed'] else 'FAILED'}; checks {load_result['checks']}.",
                  f"- Outcomes {load_result['outcomes']}; provider calls {load_result['provider_calls']}; "
                  f"budget poll p95 {load_result['poll_ms']['p95']} ms over {load_result['poll_ms']['samples']} reads "
                  f"while calls were in flight; submit p95 {load_result['submit_ms']['p95']} ms."]
    else:
        lines.append("- Not run on this host: `python -m rfp_assistant.cli load-check --users 6 --provider fake --save`.")
    lines += ["- `check --phase 3 --provider fake` covers the scenario table (see its output; it is not stored here).",
              "", "## Browser verification", ""]
    if browser:
        lines += [f"- {browser.get('summary', '')}", "", "| step | role | viewport | outcome |",
                  "| --- | --- | --- | --- |"]
        lines += [f"| {s['step']} | {s['role']} | {s['viewport']} | {s['outcome']} |" for s in browser.get("steps", [])]
        lines += [f"- Screenshots: {', '.join(browser.get('screenshots', []))}"]
    else:
        lines.append("- No browser results recorded on this host.")
    lines += ["", "## Paid smoke", ""]
    lines.append(f"- {smoke}" if smoke else "- Not run: a real consultant smoke through the gateway needs an explicit "
                                             "owner estimate and approval.")
    lines += ["", "## Open items", "",
              *([] if host else ["- The team host and who may reach it are owner decisions (runbook); without login, "
                                 "network reach is the only access control."]),
              "- Measured warm/cold latency on the team host and six real browsers remain to be recorded there.", ""]
    path = out / "report.md"
    store.write_text_atomic(path, "\n".join(lines))
    return path
