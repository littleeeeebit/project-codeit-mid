"""Reservation, settlement, billing recovery, adjustments and snapshots over one shared ledger.

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
from .settings import DEFAULT_RATES, RATE_VERSION, LARGE_RATE_VERSION, LARGE_RATE_CHECKED_AT
from .store import OPERATIONAL_ERRORS, dumps, get_app_setting, open_db, set_app_setting, tx, utcnow
from .postgres import owner_guard, require_owner, Target

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
            "INSERT INTO budget_settings(id, allowance_micro_usd, cap_micro_usd, envelopes_json, rates_json, "
            "rate_version) VALUES (1, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
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
    """The one paid switch: the ledger flag and PostgreSQL admission change together. Enabling requires the
    validated import, no recovery fence and no unknown billing; a restored database stays off until this runs."""
    from .postgres import recovery_blocked, validation_ready

    with open_db(db) as conn, tx(conn, immediate=True):
        row = _settings_row(conn)
        if enabled and not (row["prior_use_recorded"] and row["project_start"] and row["project_end"]):
            raise BudgetError("configure-budget must record dates and prior use first")
        if isinstance(db, Target):
            if enabled and (recovery_blocked(conn) or not validation_ready(conn)):
                raise BudgetError("paid admission requires a validated import and no open recovery fence")
            if enabled and conn.execute("SELECT count(*) FROM attempts WHERE state = 'unknown'").fetchone()[0]:
                raise BudgetError("reconcile unknown billing before enabling paid admission")
            conn.execute("UPDATE database_control SET paid_admission = ? WHERE id = 1", (enabled,))
        history = json.loads(row["history_json"]) + [{"at": utcnow(), "actor": actor, "paid_enabled": enabled,
                                                      "reason": reason}]
        conn.execute("UPDATE budget_settings SET paid_enabled = ?, frozen_reason = NULL, history_json = ? WHERE id = 1",
                     (int(enabled), dumps(history)))


def set_envelopes(db, actor: str, envelopes: dict[str, int], reason: str):
    """Explicit owner reallocation, retaining the cap, settled spend and open reservations."""
    if not actor.strip() or not reason.strip() or set(envelopes) != set(ENVELOPE_SHARES) or \
            any(type(value) is not int or value < 0 for value in envelopes.values()):
        raise BudgetError("envelopes require an actor, reason and nonnegative integer micro-units for every purpose")
    with open_db(db) as conn, tx(conn, immediate=True):
        row = _settings_row(conn)
        if sum(envelopes.values()) != row["cap_micro_usd"]:
            raise BudgetError("purpose envelopes must sum to the existing operating cap")
        if any(value < _purpose_used(conn, purpose) for purpose, value in envelopes.items()):
            raise BudgetError("an envelope cannot be reduced below settled spending and open reservations")
        history = json.loads(row["history_json"]) + [{"at": utcnow(), "actor": actor, "reason": reason,
                                                    "envelopes_micro_usd": envelopes}]
        conn.execute("UPDATE budget_settings SET envelopes_json=?,history_json=?,revision=revision+1 WHERE id=1",
                     (dumps(envelopes), dumps(history)))
        conn.execute("INSERT INTO audit_events VALUES (?,?,?,?,?,?,?)",
                     (str(uuid.uuid4()), actor, "set_envelopes", "budget", reason, dumps(envelopes), utcnow()))
        _bump(conn)
    return envelopes


def set_limit(db, actor: str, cap_micro: int, reason: str):
    """Change a shared limit atomically, without resetting spending, paid state or rate identities."""
    if type(cap_micro) is not int or not 0 < cap_micro <= 9_000_000_000_000_000 or not actor.strip() or not reason.strip():
        raise BudgetError("a positive exact limit, actor and reason are required")
    with open_db(db) as conn, tx(conn, immediate=True):
        row = _settings_row(conn)
        totals = _totals(conn)
        if cap_micro < totals["spent"] + totals["pending"]:
            raise BudgetError("limit cannot be below settled spending and open reservations")
        old = json.loads(row["envelopes_json"])
        if sum(old.values()) != row["cap_micro_usd"]:
            raise BudgetError("existing envelopes do not match the operating cap; reconcile before changing the limit")
        # Preserve the owner's current purpose allocation, including any embedding reallocation.
        envelopes = {key: cap_micro * value // row["cap_micro_usd"] for key, value in old.items()}
        envelopes["interactive"] += cap_micro - sum(envelopes.values())
        if any(value < _purpose_used(conn, key) for key, value in envelopes.items()):
            raise BudgetError("scaled purpose limit is below its spending/reservations; reallocate envelopes first")
        allowance = max(row["allowance_micro_usd"], cap_micro)
        record = {"at": utcnow(), "actor": actor, "reason": reason, "cap_micro_usd": cap_micro,
                  "allowance_micro_usd": allowance, "envelopes_micro_usd": envelopes}
        history = json.loads(row["history_json"]) + [record]
        conn.execute("UPDATE budget_settings SET cap_micro_usd=?,allowance_micro_usd=?,envelopes_json=?,"
                     "history_json=?,revision=revision+1 WHERE id=1",
                     (cap_micro, allowance, dumps(envelopes), dumps(history)))
        conn.execute("INSERT INTO audit_events VALUES (?,?,?,?,?,?,?)",
                     (str(uuid.uuid4()), actor, "set_limit", "budget", reason, dumps(record), utcnow()))
        _bump(conn)
    return record


def register_large_rate(db, actor: str, reason: str):
    """Register the approved embedding price without rewriting historical model rates or attempts."""
    if not actor.strip() or not reason.strip():
        raise BudgetError("rate registration requires an actor and reason")
    model = "text-embedding-3-large"
    with open_db(db) as conn, tx(conn, immediate=True):
        row = _settings_row(conn)
        rates = json.loads(row["rates_json"])
        rates[model] = DEFAULT_RATES[model]
        record = {"at": utcnow(), "actor": actor, "reason": reason, "model": model,
                  "rate_version": LARGE_RATE_VERSION, "checked_at": LARGE_RATE_CHECKED_AT, "rates": rates[model]}
        history = json.loads(row["history_json"]) + [record]
        conn.execute("UPDATE budget_settings SET rates_json=?,rate_version=?,history_json=?,revision=revision+1 WHERE id=1",
                     (dumps(rates), LARGE_RATE_VERSION, dumps(history)))
        conn.execute("INSERT INTO audit_events VALUES (?,?,?,?,?,?,?)",
                     (str(uuid.uuid4()), actor, "register_embedding_rate", "budget", reason, dumps(record), utcnow()))
        _bump(conn)
    return record


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
            max_output_tokens: int, count_method: str, ceiling_micro_usd: int | None = None) -> dict:
    """Atomic admission. Returns {'admitted': bool, 'attempt_id', 'reserved_micro_usd', 'reason'}.
    `ceiling_micro_usd` is a maximum the user consented to: a reservation above it (e.g. rates reconfigured since
    the estimate) is refused in the same transaction that would have admitted it."""
    attempt_id = str(uuid.uuid4())
    try:
        require_owner(db)
        with open_db(db) as conn, tx(conn, immediate=True):
            ownership_error = owner_guard(conn)
            if ownership_error:
                return {"admitted": False, "reason": ownership_error, "attempt_id": None}
            if isinstance(db, Target) and not conn.execute(
                    "SELECT paid_admission FROM database_control WHERE id=1").fetchone()[0]:
                return {"admitted": False, "reason": "postgresql_maintenance", "attempt_id": None}
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
            if ceiling_micro_usd is not None and amount > ceiling_micro_usd:
                return {"admitted": False, "reason": "above_consented_maximum", "attempt_id": None,
                        "reserved_micro_usd": amount, "ceiling_micro_usd": ceiling_micro_usd}
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
    except OPERATIONAL_ERRORS:  # lock wait exceeded or database unavailable: fail closed, no DSN in errors
        return {"admitted": False, "reason": "ledger_unavailable", "attempt_id": None}
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


class DispatchRefused(BudgetError):
    """The owning request may no longer start a paid call (cancelled, interrupted, service closing). The
    reservation was released in the same transaction that refused it; nothing was sent."""


def mark_dispatching(db: Path, attempt_id: str, guard=None) -> None:
    """Durable marker before the network call. `guard(conn)` runs in the same transaction and returns a refusal
    reason (or None), so a request marked interrupted or cancelled can never be dispatched after the check."""
    require_owner(db)
    with open_db(db) as conn, tx(conn, immediate=True):
        reason = guard(conn) if guard is not None else None
        reason = reason or owner_guard(conn)
        if isinstance(db, Target) and not conn.execute(
                "SELECT paid_admission FROM database_control WHERE id=1").fetchone()[0]:
            reason = "postgresql_maintenance"
        if isinstance(db, Target) and conn.execute(
                "SELECT count(*) FROM attempts WHERE state = 'unknown'").fetchone()[0]:
            reason = "unknown PostgreSQL billing must be reconciled before dispatch"
        if reason:
            cur = conn.execute("UPDATE attempts SET state = 'released', finished_at = ?, error_json = ? "
                               "WHERE attempt_id = ? AND state = 'reserved'",
                               (utcnow(), dumps({"reason": f"stopped_before_dispatch:{reason}"}), attempt_id))
        else:
            cur = conn.execute("UPDATE attempts SET state = 'dispatching', dispatched_at = ? "
                               "WHERE attempt_id = ? AND state = 'reserved'", (utcnow(), attempt_id))
        if cur.rowcount:
            _bump(conn)
    if cur.rowcount != 1:
        raise BudgetError("attempt is not in reserved state")
    if reason:
        raise DispatchRefused(reason)


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
            same = (prior["interval_start"], prior["interval_end"], prior["scope"],
                    json.loads(prior["covered_attempts_json"])) == (interval_start, interval_end, scope,
                                                                     covered_attempt_ids)
            if not same or prior["evidence"] != evidence or _provider_total(conn, prior) != provider_total_micro:
                raise BudgetError("this reconciliation ID was already imported with different values; a changed "
                                  "provider total needs a separate owner correction")
            return False
        if not interval_start or not interval_end or interval_start > interval_end:
            raise BudgetError("reconciliation needs a closed interval with start <= end")
        if not evidence.strip() or not scope.strip():
            raise BudgetError("reconciliation needs the provider scope and dated evidence")
        for attempt_id in covered_attempt_ids:  # only explicitly covered unknown attempts inside the interval
            a = conn.execute("SELECT state, dispatched_at FROM attempts WHERE attempt_id = ?",
                             (attempt_id,)).fetchone()
            if a is None or a["state"] != "unknown":
                raise BudgetError(f"covered attempt {attempt_id} is not an unknown attempt")
            if not a["dispatched_at"] or not interval_start <= a["dispatched_at"] <= interval_end:
                raise BudgetError(f"covered attempt {attempt_id} was not dispatched inside the interval")
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
             f"provider reconciliation; provider_total_micro_usd={provider_total_micro}; "
             f"local_settled_micro_usd={local}", actor, dumps(covered_attempt_ids), utcnow()))
        for attempt_id in covered_attempt_ids:
            conn.execute("UPDATE attempts SET state = 'reconciled' WHERE attempt_id = ? AND state = 'unknown'",
                         (attempt_id,))
        _bump(conn)
    return True


def _provider_total(conn: sqlite3.Connection, adjustment: sqlite3.Row) -> int | None:
    """The provider total an imported reconciliation recorded (reason text written by `reconcile`)."""
    for part in adjustment["reason"].split(";"):
        name, _, value = part.strip().partition("=")
        if name == "provider_total_micro_usd":
            return int(value)
    return None


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


CAP_WARNINGS = (50, 75, 90)


def pacing(cap_micro: int, committed_micro: int, start: str | None, end: str | None, today: date) -> dict | None:
    """Linear pacing over the configured project dates: how much of the cap the elapsed share would allow."""
    if not start or not end:
        return None
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    total = (e - s).days or 1
    elapsed = min(1.0, max(0.0, ((today - s).days + 1) / total))
    expected = int(cap_micro * elapsed)
    return {"elapsed_fraction": round(elapsed, 4), "expected_micro_usd": expected,
            "committed_micro_usd": committed_micro, "ahead": committed_micro > expected,
            "days_left": max(0, (e - today).days)}


def snapshot(db: Path, today: date | None = None) -> BudgetSnapshot:
    from zoneinfo import ZoneInfo
    from datetime import datetime

    today = today or datetime.now(ZoneInfo("Asia/Seoul")).date()
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
        unknown = conn.execute("SELECT COALESCE(SUM(reserved_micro_usd), 0) FROM attempts WHERE state = 'unknown'"
                               ).fetchone()[0]
    allowance = row["allowance_micro_usd"]
    committed = totals["spent"] + totals["pending"]
    cap_percent = 100 * committed / row["cap_micro_usd"] if row["cap_micro_usd"] else 100.0
    warnings = [f"cap_{level}" for level in CAP_WARNINGS if cap_percent >= level]
    pace = pacing(row["cap_micro_usd"], committed, row["project_start"], row["project_end"], today)
    if pace and pace["ahead"]:
        warnings.append("ahead_of_pace")
    if row["cap_micro_usd"] - committed <= 0:
        warnings.append("cap_exhausted")
    if unknown:
        warnings.append("unknown_billing")
    return BudgetSnapshot(
        allowance_micro_usd=allowance, cap_micro_usd=row["cap_micro_usd"], spent_micro_usd=totals["spent"],
        pending_micro_usd=totals["pending"],
        available_micro_usd=row["cap_micro_usd"] - totals["spent"] - totals["pending"],
        spent_percent=100 * totals["spent"] / allowance,
        committed_percent=100 * (totals["spent"] + totals["pending"]) / allowance,
        paid_enabled=bool(row["paid_enabled"]), frozen_reason=row["frozen_reason"], tokens=tokens,
        per_member=per_member, open_attempts=open_attempts, ledger_revision=revision, tracking_scope=TRACKING_SCOPE,
        last_reconciliation=last, project_start=row["project_start"], project_end=row["project_end"],
        cap_percent=cap_percent, warnings=warnings, pacing=pace, read_at=utcnow(), unknown_micro_usd=unknown)
