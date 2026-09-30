"""Reservation, settlement, billing recovery, adjustments and snapshots over one shared SQLite ledger.

State machine (docs/plan/end-to-end/implementation-contracts.md):
    reserved -> dispatching -> settled
                         \\-> unknown -> settled | reconciled
    reconciled -> settled (late usage, compensating adjustment)
    reserved -> released; dispatching -> released only on confirmed pre-execution rejection
"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from .contracts import BudgetSnapshot
from .settings import DEFAULT_RATES, RATE_VERSION
from .store import dumps, get_app_setting, open_db, set_app_setting, tx, utcnow

MICRO = 1_000_000
ENVELOPE_SHARES = {"embedding": 1, "gold_eval": 3, "interactive": 12}  # sixteenths of the operating cap
DEFAULT_ENVELOPES = {k: v * MICRO for k, v in ENVELOPE_SHARES.items()}
OPEN_STATES = ("reserved", "dispatching", "unknown")
TRACKING_SCOPE = "이 앱의 게이트웨이를 거친 호출과 기록된 조정만 포함합니다. 제공자 잔액이 아닙니다."


class BudgetError(RuntimeError):
    pass


def micro_cost(rates: dict, input_tokens: int, cached_tokens: int, output_tokens: int,
               cache_write_tokens: int = 0) -> int:
    """Integer microdollars, rounded up. Rates are USD per 1M tokens, so tokens * rate is micro-USD."""
    cached_tokens = min(cached_tokens, input_tokens)
    cache_write_tokens = min(cache_write_tokens, input_tokens - cached_tokens)
    total = (Decimal(input_tokens - cached_tokens - cache_write_tokens) * Decimal(rates["input"])
             + Decimal(cached_tokens) * Decimal(rates.get("cached_input", rates["input"]))
             + Decimal(cache_write_tokens) * Decimal(rates.get("cache_write", rates["input"]))
             + Decimal(output_tokens) * Decimal(rates.get("output", "0")))
    return math.ceil(total)


def max_cost(rates: dict, input_tokens: int, max_output_tokens: int) -> int:
    """Reservation upper bound: every input token billed at the dearest input rate (a cache write)."""
    return micro_cost(rates, input_tokens, 0, max_output_tokens, cache_write_tokens=input_tokens)


def _bump(conn: sqlite3.Connection) -> None:
    set_app_setting(conn, "ledger_revision", str(int(get_app_setting(conn, "ledger_revision") or 0) + 1))


def ensure_budget_row(db: Path) -> None:
    """Creates the single allowance row paid-disabled. Never touches an existing row."""
    with open_db(db) as conn, tx(conn, immediate=True):
        conn.execute(
            "INSERT OR IGNORE INTO budget_settings(id, allowance_micro_usd, cap_micro_usd, envelopes_json, rates_json, "
            "rate_version) VALUES (1, ?, ?, ?, ?, ?)",
            (20 * MICRO, 16 * MICRO, dumps(DEFAULT_ENVELOPES), dumps(DEFAULT_RATES), RATE_VERSION),
        )


def _settings_row(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM budget_settings WHERE id = 1").fetchone()
    if row is None:
        raise BudgetError("budget is not initialized; run init")
    return row


def envelopes_for(cap_micro: int) -> dict[str, int]:
    """Splits the cap by ENVELOPE_SHARES; rounding remainder goes to interactive so the sum equals the cap."""
    total = sum(ENVELOPE_SHARES.values())
    out = {k: cap_micro * v // total for k, v in ENVELOPE_SHARES.items()}
    out["interactive"] += cap_micro - sum(out.values())
    return out


def configure(db: Path, actor: str, *, project_start: date, project_end: date, prior_use_micro: int,
              prior_use_evidence: str, rates: dict, rate_version: str, enable_paid: bool,
              allowance_micro: int | None = None, cap_micro: int | None = None) -> dict:
    """Owner-only. Records the prior-use baseline once, as an append-only adjustment. A new allowance/cap
    re-splits the purpose envelopes."""
    if project_end <= project_start:
        raise BudgetError("project_end must be after project_start")
    if (allowance_micro is None) != (cap_micro is None):
        raise BudgetError("set allowance and cap together")
    if allowance_micro is not None and not 0 < cap_micro <= allowance_micro:
        raise BudgetError("cap must be positive and not exceed the allowance")
    if prior_use_micro < 0 or not prior_use_evidence.strip():
        raise BudgetError("prior use requires a nonnegative amount and evidence")
    for model, card in rates.items():
        if "input" not in card:
            raise BudgetError(f"incomplete rate card for {model}")
    with open_db(db) as conn, tx(conn, immediate=True):
        row = _settings_row(conn)
        existing = conn.execute(
            "SELECT amount_micro_usd FROM adjustments WHERE correction_key = 'prior-use-baseline'").fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO adjustments(adjustment_id, correction_key, amount_micro_usd, scope, evidence, reason, actor, "
                "created_at) VALUES (?, 'prior-use-baseline', ?, 'allowance', ?, 'prior use before gateway', ?, ?)",
                (str(uuid.uuid4()), prior_use_micro, prior_use_evidence, actor, utcnow()),
            )
        elif existing["amount_micro_usd"] != prior_use_micro:
            raise BudgetError("a different prior-use baseline is already recorded; add an owner correction instead")
        history = json.loads(row["history_json"])
        history.append({"at": utcnow(), "actor": actor, "start": project_start.isoformat(),
                        "end": project_end.isoformat(), "rate_version": rate_version, "paid_enabled": enable_paid,
                        "allowance_micro_usd": allowance_micro, "cap_micro_usd": cap_micro})
        if allowance_micro is not None:
            conn.execute("UPDATE budget_settings SET allowance_micro_usd = ?, cap_micro_usd = ?, envelopes_json = ? "
                         "WHERE id = 1", (allowance_micro, cap_micro, dumps(envelopes_for(cap_micro))))
        conn.execute(
            "UPDATE budget_settings SET project_start = ?, project_end = ?, rates_json = ?, rate_version = ?, "
            "paid_enabled = ?, prior_use_recorded = 1, revision = revision + 1, history_json = ? WHERE id = 1",
            (project_start.isoformat(), project_end.isoformat(), dumps(rates), rate_version, int(enable_paid),
             dumps(history)),
        )
        _bump(conn)
    return {"paid_enabled": enable_paid}


def set_paid_enabled(db: Path, actor: str, enabled: bool, reason: str) -> None:
    with open_db(db) as conn, tx(conn, immediate=True):
        row = _settings_row(conn)
        if enabled and not (row["prior_use_recorded"] and row["project_start"] and row["project_end"]):
            raise BudgetError("configure-budget must record dates and prior use first")
        history = json.loads(row["history_json"]) + [{"at": utcnow(), "actor": actor, "paid_enabled": enabled,
                                                      "reason": reason}]
        conn.execute("UPDATE budget_settings SET paid_enabled = ?, frozen_reason = NULL, history_json = ? WHERE id = 1",
                     (int(enabled), dumps(history)))


def _totals(conn: sqlite3.Connection) -> dict:
    spent = conn.execute("SELECT COALESCE(SUM(settled_micro_usd), 0) FROM attempts WHERE state = 'settled'").fetchone()[0]
    adjust = conn.execute("SELECT COALESCE(SUM(amount_micro_usd), 0) FROM adjustments").fetchone()[0]
    pending = conn.execute(
        f"SELECT COALESCE(SUM(reserved_micro_usd), 0) FROM attempts WHERE state IN {OPEN_STATES}").fetchone()[0]
    return {"spent": spent + adjust, "pending": pending}


def _purpose_used(conn: sqlite3.Connection, purpose: str) -> int:
    return conn.execute(
        f"SELECT COALESCE(SUM(CASE WHEN state = 'settled' THEN settled_micro_usd "
        f"WHEN state IN {OPEN_STATES} THEN reserved_micro_usd ELSE 0 END), 0) FROM attempts WHERE purpose = ?",
        (purpose,)).fetchone()[0]


def estimate(db: Path, model: str, input_tokens: int, max_output_tokens: int) -> int:
    with open_db(db) as conn:
        rates = json.loads(_settings_row(conn)["rates_json"]).get(model)
    if rates is None:
        raise BudgetError(f"no configured rate for {model}")
    return max_cost(rates, input_tokens, max_output_tokens)


def reserve(db: Path, *, request_id: str, member_id: str, stage: str, purpose: str, model: str, input_tokens: int,
            max_output_tokens: int, count_method: str) -> dict:
    """Atomic admission. Returns {'admitted': bool, 'attempt_id', 'reserved_micro_usd', 'reason'}."""
    attempt_id = str(uuid.uuid4())
    try:
        with open_db(db) as conn, tx(conn, immediate=True):
            row = _settings_row(conn)
            if not row["paid_enabled"]:
                return {"admitted": False, "reason": "paid_disabled", "attempt_id": None}
            if row["frozen_reason"]:
                return {"admitted": False, "reason": "frozen", "attempt_id": None}
            rates = json.loads(row["rates_json"]).get(model)
            if rates is None:
                return {"admitted": False, "reason": "unknown_rate", "attempt_id": None}
            envelopes = json.loads(row["envelopes_json"])
            if purpose not in envelopes:
                return {"admitted": False, "reason": "unknown_purpose", "attempt_id": None}
            amount = max_cost(rates, input_tokens, max_output_tokens)
            totals = _totals(conn)
            available = row["cap_micro_usd"] - totals["spent"] - totals["pending"]
            if available < amount:
                return {"admitted": False, "reason": "cap_exhausted", "attempt_id": None,
                        "reserved_micro_usd": amount, "available_micro_usd": available}
            if envelopes[purpose] - _purpose_used(conn, purpose) < amount:
                return {"admitted": False, "reason": "envelope_exhausted", "attempt_id": None,
                        "reserved_micro_usd": amount}
            conn.execute(
                "INSERT INTO attempts(attempt_id, request_id, member_id, stage, purpose, model, state, reserved_micro_usd, "
                "estimated_input_tokens, max_output_tokens, count_method, price_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?, ?, ?)",
                (attempt_id, request_id, member_id, stage, purpose, model, amount, input_tokens, max_output_tokens,
                 count_method, dumps({"model": model, "rates": rates, "rate_version": row["rate_version"]}), utcnow()),
            )
            _bump(conn)
    except sqlite3.OperationalError as exc:  # lock wait exceeded or database unavailable: fail closed
        return {"admitted": False, "reason": f"ledger_unavailable:{exc}", "attempt_id": None}
    return {"admitted": True, "attempt_id": attempt_id, "reserved_micro_usd": amount, "reason": None}


def _transition(db: Path, attempt_id: str, from_states: tuple, to_state: str, **fields) -> bool:
    sets = ", ".join([f"{k} = ?" for k in fields] + ["state = ?"])
    with open_db(db) as conn, tx(conn, immediate=True):
        cur = conn.execute(
            f"UPDATE attempts SET {sets} WHERE attempt_id = ? AND state IN ({','.join('?' * len(from_states))})",
            (*fields.values(), to_state, attempt_id, *from_states))
        if cur.rowcount:
            _bump(conn)
        return cur.rowcount == 1


def mark_dispatching(db: Path, attempt_id: str) -> None:
    """Durable marker before the network call."""
    if not _transition(db, attempt_id, ("reserved",), "dispatching", dispatched_at=utcnow()):
        raise BudgetError("attempt is not in reserved state")


def release(db: Path, attempt_id: str, reason: str, confirmed_pre_execution: bool = False) -> bool:
    states = ("reserved", "dispatching") if confirmed_pre_execution else ("reserved",)
    return _transition(db, attempt_id, states, "released", finished_at=utcnow(),
                       error_json=dumps({"reason": reason}))


def mark_unknown(db: Path, attempt_id: str, error: str) -> bool:
    return _transition(db, attempt_id, ("dispatching",), "unknown", finished_at=utcnow(),
                       error_json=dumps({"error": error[:500]}))


def settle(db: Path, attempt_id: str, usage: dict, response_id: str | None) -> dict:
    """Exactly once. usage: prompt_tokens, completion_tokens, cached_tokens, cache_write_tokens
    (Chat Completions field meanings)."""
    with open_db(db) as conn, tx(conn, immediate=True):
        row = conn.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        if row is None:
            raise BudgetError("unknown attempt")
        if row["state"] == "settled":
            return {"settled_micro_usd": row["settled_micro_usd"], "duplicate": True}
        if row["state"] not in ("dispatching", "unknown", "reconciled"):
            raise BudgetError(f"cannot settle an attempt in state {row['state']}")
        rates = json.loads(row["price_json"])["rates"]
        cost = micro_cost(rates, int(usage.get("prompt_tokens", 0)), int(usage.get("cached_tokens", 0)),
                          int(usage.get("completion_tokens", 0)), int(usage.get("cache_write_tokens", 0)))
        conn.execute(
            "UPDATE attempts SET state = 'settled', settled_micro_usd = ?, raw_usage_json = ?, response_id = ?, "
            "finished_at = ? WHERE attempt_id = ?",
            (cost, dumps(usage), response_id, utcnow(), attempt_id))
        if row["state"] == "reconciled":  # the provider total already counted it
            conn.execute(
                "INSERT INTO adjustments(adjustment_id, correction_key, amount_micro_usd, scope, evidence, reason, "
                "actor, covered_attempts_json, created_at) VALUES (?, ?, ?, 'allowance', 'late usage', "
                "'late settlement of reconciled attempt', 'system', ?, ?)",
                (str(uuid.uuid4()), f"late-settlement:{attempt_id}", -cost, dumps([attempt_id]), utcnow()))
        overrun = cost > row["reserved_micro_usd"]
        if overrun:  # counting or rates were wrong: stop new paid work until inspected
            conn.execute("UPDATE budget_settings SET frozen_reason = ? WHERE id = 1",
                         (f"settled cost exceeded reservation for attempt {attempt_id}",))
        _bump(conn)
    return {"settled_micro_usd": cost, "duplicate": False, "overrun": overrun}


def add_adjustment(db: Path, actor: str, correction_key: str, amount_micro: int, evidence: str, reason: str,
                   scope: str = "allowance") -> bool:
    """Append-only and idempotent by correction_key. Returns False when that key was already applied."""
    if not evidence.strip():
        raise BudgetError("adjustments require evidence")
    with open_db(db) as conn, tx(conn, immediate=True):
        prior = conn.execute("SELECT amount_micro_usd FROM adjustments WHERE correction_key = ?",
                             (correction_key,)).fetchone()
        if prior is not None:
            if prior["amount_micro_usd"] != amount_micro:
                raise BudgetError("correction key reused with a different amount")
            return False
        conn.execute(
            "INSERT INTO adjustments(adjustment_id, correction_key, amount_micro_usd, scope, evidence, reason, actor, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), correction_key, amount_micro, scope, evidence, reason, actor, utcnow()))
        _bump(conn)
    return True


def reconcile(db: Path, actor: str, reconciliation_id: str, interval_start: str, interval_end: str,
              provider_total_micro: int, scope: str, evidence: str, covered_attempt_ids: list[str]) -> bool:
    """Closed interval: records provider total minus local settled cost for it; covered unknown attempts
    become reconciled and stop holding a reservation."""
    key = f"reconcile:{reconciliation_id}"
    with open_db(db) as conn, tx(conn, immediate=True):
        prior = conn.execute("SELECT * FROM adjustments WHERE correction_key = ?", (key,)).fetchone()
        if prior is not None:
            return False
        overlap = conn.execute(
            "SELECT 1 FROM adjustments WHERE correction_key LIKE 'reconcile:%' AND scope = ? "
            "AND NOT (interval_end < ? OR interval_start > ?)", (scope, interval_start, interval_end)).fetchone()
        if overlap:
            raise BudgetError("reconciliation intervals must not overlap")
        local = conn.execute(
            "SELECT COALESCE(SUM(settled_micro_usd), 0) FROM attempts WHERE state = 'settled' "
            "AND finished_at >= ? AND finished_at <= ?", (interval_start, interval_end)).fetchone()[0]
        conn.execute(
            "INSERT INTO adjustments(adjustment_id, correction_key, amount_micro_usd, interval_start, interval_end, "
            "scope, evidence, reason, actor, covered_attempts_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), key, provider_total_micro - local, interval_start, interval_end, scope, evidence,
             "provider reconciliation", actor, dumps(covered_attempt_ids), utcnow()))
        for attempt_id in covered_attempt_ids:
            conn.execute("UPDATE attempts SET state = 'reconciled' WHERE attempt_id = ? AND state = 'unknown'",
                         (attempt_id,))
        _bump(conn)
    return True


def recover(db: Path) -> dict:
    """Startup recovery. Caller holds the process-owner lock, so no older worker can still dispatch."""
    with open_db(db) as conn, tx(conn, immediate=True):
        unknown = conn.execute(
            "UPDATE attempts SET state = 'unknown', error_json = ? WHERE state = 'dispatching'",
            (dumps({"error": "process ended during dispatch"}),)).rowcount
        released = conn.execute(
            "UPDATE attempts SET state = 'released', error_json = ? WHERE state = 'reserved'",
            (dumps({"reason": "never dispatched before restart"}),)).rowcount
        if unknown or released:
            _bump(conn)
    return {"unknown": unknown, "released": released}


def snapshot(db: Path) -> BudgetSnapshot:
    with open_db(db) as conn:
        row = _settings_row(conn)
        totals = _totals(conn)
        tokens = {"prompt": 0, "completion": 0, "cached": 0}
        per_member: dict[str, int] = {}
        for a in conn.execute("SELECT member_id, state, settled_micro_usd, raw_usage_json FROM attempts "
                              "WHERE state = 'settled'"):
            usage = json.loads(a["raw_usage_json"] or "{}")
            tokens["prompt"] += int(usage.get("prompt_tokens", 0))
            tokens["completion"] += int(usage.get("completion_tokens", 0))
            tokens["cached"] += int(usage.get("cached_tokens", 0))
            per_member[a["member_id"]] = per_member.get(a["member_id"], 0) + a["settled_micro_usd"]
        open_attempts = conn.execute(f"SELECT COUNT(*) FROM attempts WHERE state IN {OPEN_STATES}").fetchone()[0]
        last = conn.execute("SELECT MAX(created_at) FROM adjustments WHERE correction_key LIKE 'reconcile:%'"
                            ).fetchone()[0]
        revision = get_app_setting(conn, "ledger_revision") or "0"
    allowance = row["allowance_micro_usd"]
    return BudgetSnapshot(
        allowance_micro_usd=allowance, cap_micro_usd=row["cap_micro_usd"], spent_micro_usd=totals["spent"],
        pending_micro_usd=totals["pending"],
        available_micro_usd=row["cap_micro_usd"] - totals["spent"] - totals["pending"],
        spent_percent=100 * totals["spent"] / allowance,
        committed_percent=100 * (totals["spent"] + totals["pending"]) / allowance,
        paid_enabled=bool(row["paid_enabled"]), frozen_reason=row["frozen_reason"], tokens=tokens,
        per_member=per_member, open_attempts=open_attempts, ledger_revision=revision, tracking_scope=TRACKING_SCOPE,
        last_reconciliation=last)
