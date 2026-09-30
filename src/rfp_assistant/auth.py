"""Capability checks on the principal a caller supplies.

There is no login (owner decision, 2026-09-30): the UI builds a principal from the visitor's chosen name with
every capability. The checks stay so each public service function still states which role it serves.
"""

from __future__ import annotations

from .contracts import CAPABILITIES, Principal


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


OWNER_CLI = Principal("owner-cli", frozenset(CAPABILITIES))
