"""Capability checks on the principal a caller supplies.

There is no login (owner decision 2026-09-30, reaffirmed for phase 3 on 2026-10-01): every visitor gets every
screen, and the name typed in the sidebar only attributes paid requests, reviews and owner actions. The checks
stay so each public service function still states which role it serves, and in-process callers (CLI, tests) can
still construct narrower principals.
"""

from __future__ import annotations

from ..contracts import CAPABILITIES, Principal


class AuthError(PermissionError):
    pass


def require(principal: Principal | None, capability: str) -> Principal:
    if principal is None or not principal.can(capability):
        raise AuthError("이 작업을 수행할 권한이 없습니다.")
    return principal


def require_any(principal: Principal | None, *capabilities: str) -> Principal:
    if principal is None or not any(principal.can(c) for c in capabilities):
        raise AuthError("이 작업을 수행할 권한이 없습니다.")
    return principal


def visitor(name: str | None) -> Principal:
    """The UI principal: the typed name (attribution, not authentication) with every capability."""
    return Principal((name or "").strip()[:40] or "owner", frozenset(CAPABILITIES))


OWNER_CLI = Principal("owner-cli", frozenset(CAPABILITIES))
