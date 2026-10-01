"""Server-owned principals: member accounts, sessions, revocation and capability checks.

Two modes, decided by whether the private accounts file exists (`Settings.accounts_path`):

* token: six (or so) owner-provisioned members, each with a random high-entropy access token whose SHA256 digest
  and capabilities live in the private accounts file. Login creates a server session with an absolute expiry;
  every public service call that carries a session revalidates it (expiry, revocation, account enabled, account
  revision), so removing a capability or rotating a token takes effect on the next call.
* open: the phase-1 owner decision (2026-09-30) for a single localhost host without accounts. The visitor's name
  only attributes work; it grants consultant and verifier screens but never budget administration or sealed data.

Principals without a session ID are in-process callers (CLI maintenance, tests) and are trusted as constructed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .contracts import CAPABILITIES, Principal
from .store import open_db, tx, write_text_atomic

FAILURE_WINDOW = timedelta(minutes=15)
MAX_MEMBER_FAILURES = 5  # per known account inside the window
MAX_UNKNOWN_FAILURES = 30  # across unknown account names inside the window
LOGIN_FAILED = "로그인에 실패했습니다. 이름과 접속 토큰을 확인하거나 잠시 후 다시 시도하세요."
OPEN_CAPABILITIES = frozenset({"consultant", "verifier"})


class AuthError(PermissionError):
    pass


def clock() -> datetime:
    """Patched by tests to move time; all expiry and rate-limit decisions read it."""
    return datetime.now(timezone.utc)


def require(principal: Principal | None, capability: str) -> Principal:
    if principal is None or not principal.can(capability):
        raise AuthError("이 작업을 수행할 권한이 없습니다.")
    return principal


def require_any(principal: Principal | None, *capabilities: str) -> Principal:
    if principal is None or not any(principal.can(c) for c in capabilities):
        raise AuthError("이 작업을 수행할 권한이 없습니다.")
    return principal


OWNER_CLI = Principal("owner-cli", frozenset(CAPABILITIES))


# ---------------------------------------------------------------- accounts file


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _member_revision(member: dict) -> str:
    """Changes whenever this member's token, capabilities or enabled state change; old sessions then end."""
    data = {k: member.get(k) for k in ("member_id", "token_sha256", "capabilities", "enabled", "rotated_at")}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def mode(settings) -> str:
    return "token" if settings.accounts_path.exists() else "open"


def load_accounts(settings) -> dict[str, dict]:
    path: Path = settings.accounts_path
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    members = {}
    for m in data.get("members", []):
        caps = set(m.get("capabilities") or [])
        if not caps <= set(CAPABILITIES) or not m.get("member_id") or len(m.get("token_sha256") or "") != 64:
            raise AuthError(f"invalid account record for {m.get('member_id')!r}")
        members[m["member_id"]] = m
    return members


def _write_accounts(settings, members: dict[str, dict]) -> None:
    path: Path = settings.accounts_path
    write_text_atomic(path, json.dumps({"members": sorted(members.values(), key=lambda m: m["member_id"])},
                                       ensure_ascii=False, indent=1))
    try:
        os.chmod(path, 0o600)
    except OSError:  # Windows ACLs; the file stays under the gitignored runtime directory
        pass


def provision(settings, member_id: str, capabilities: list[str]) -> str:
    """Owner-only, on the owner host. Creates or rotates a member; returns the new token, shown only once."""
    member_id = member_id.strip()
    if not member_id or len(member_id) > 40:
        raise AuthError("member_id must be 1..40 characters")
    caps = sorted(set(capabilities))
    if not caps or not set(caps) <= set(CAPABILITIES):
        raise AuthError(f"capabilities must be a nonempty subset of {CAPABILITIES}")
    members = load_accounts(settings)
    token = secrets.token_urlsafe(32)  # 32 random bytes
    members[member_id] = {"member_id": member_id, "token_sha256": _digest(token), "capabilities": caps,
                          "enabled": True, "rotated_at": clock().isoformat()}
    _write_accounts(settings, members)
    revoke_sessions(settings, member_id, "token_rotated")
    return token


def set_enabled(settings, member_id: str, enabled: bool) -> None:
    members = load_accounts(settings)
    if member_id not in members:
        raise AuthError(f"unknown member {member_id!r}")
    members[member_id]["enabled"] = enabled
    _write_accounts(settings, members)
    if not enabled:
        revoke_sessions(settings, member_id, "account_disabled")


def list_members(settings) -> list[dict]:
    return [{"member_id": m["member_id"], "capabilities": m["capabilities"], "enabled": m["enabled"]}
            for m in load_accounts(settings).values()]


# ---------------------------------------------------------------- sessions


def _recent_failures(conn, key: str, now: datetime) -> int:
    return conn.execute("SELECT COUNT(*) FROM login_failures WHERE member_key = ? AND at >= ?",
                        (key, (now - FAILURE_WINDOW).isoformat())).fetchone()[0]


def login(settings, member_id: str, token: str) -> Principal:
    """Generic failure text whatever went wrong; bounded attempts per account and for unknown names."""
    now = clock()
    members = load_accounts(settings)
    member = members.get((member_id or "").strip())
    key = member["member_id"] if member else "*unknown*"
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        limit = MAX_MEMBER_FAILURES if member else MAX_UNKNOWN_FAILURES
        if _recent_failures(conn, key, now) >= limit:
            raise AuthError(LOGIN_FAILED)
        ok = bool(member and member.get("enabled")) and hmac.compare_digest(
            _digest(token or ""), member["token_sha256"] if member else "0" * 64)
        if not ok:
            conn.execute("INSERT INTO login_failures(member_key, at) VALUES (?, ?)", (key, now.isoformat()))
            conn.execute("DELETE FROM login_failures WHERE at < ?", ((now - 4 * FAILURE_WINDOW).isoformat(),))
            ok_session = None
        else:
            ok_session = secrets.token_urlsafe(24)
            conn.execute(
                "INSERT INTO sessions(session_id, member_id, member_revision, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (ok_session, member["member_id"], _member_revision(member), now.isoformat(),
                 (now + timedelta(hours=settings.session_hours)).isoformat()))
    if ok_session is None:
        raise AuthError(LOGIN_FAILED)
    return Principal(member["member_id"], frozenset(member["capabilities"]), ok_session)


def validate_session(settings, session_id: str) -> Principal:
    """Current principal of a session: expired, revoked, disabled or changed accounts end it."""
    with open_db(settings.db_path) as conn:
        row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    if row is None or row["revoked_at"] or clock().isoformat() >= row["expires_at"]:
        raise AuthError("세션이 만료되었거나 종료되었습니다. 다시 로그인하세요.")
    member = load_accounts(settings).get(row["member_id"])
    if member is None or not member.get("enabled") or _member_revision(member) != row["member_revision"]:
        raise AuthError("계정 설정이 바뀌어 세션이 종료되었습니다. 다시 로그인하세요.")
    return Principal(member["member_id"], frozenset(member["capabilities"]), session_id)


def refresh(settings, principal: Principal | None) -> Principal:
    """Revalidates a session principal against current accounts; in-process principals pass unchanged."""
    if principal is None:
        raise AuthError("이 작업을 수행할 권한이 없습니다.")
    if principal.session_id is None:
        return principal
    current = validate_session(settings, principal.session_id)
    if current.member_id != principal.member_id:
        raise AuthError("세션과 사용자가 일치하지 않습니다.")
    return current


def logout(settings, session_id: str) -> None:
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        conn.execute("UPDATE sessions SET revoked_at = ?, revoke_reason = 'logout' WHERE session_id = ? "
                     "AND revoked_at IS NULL", (clock().isoformat(), session_id))


def revoke_sessions(settings, member_id: str | None, reason: str) -> int:
    """Ends every open session of one member, or of everyone when member_id is None."""
    with open_db(settings.db_path) as conn, tx(conn, immediate=True):
        sql = "UPDATE sessions SET revoked_at = ?, revoke_reason = ? WHERE revoked_at IS NULL"
        args: tuple = (clock().isoformat(), reason)
        if member_id is not None:
            sql += " AND member_id = ?"
            args += (member_id,)
        return conn.execute(sql, args).rowcount


def open_principal(name: str) -> Principal:
    """Open (no-accounts) mode: attribution only, never budget administration or sealed data."""
    return Principal((name or "owner").strip()[:40] or "owner", OPEN_CAPABILITIES, None)
