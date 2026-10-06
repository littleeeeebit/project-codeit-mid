"""Who is calling: JupyterHub sign-in, then capability checks on the principal a caller supplies.

Members sign in with their JupyterHub account (owner decision 2026-10-06, which reverses the earlier no-login
decision; see `.wiki/decisions`). BidMate is an OAuth client of the hub, registered there as the `bidmate` service:
it never sees a password and keeps no user table, only in-memory sessions for the hub usernames on its allowlist.
Every signed-in member holds every capability. The checks stay so each public service function still states which
role it serves, and in-process callers (CLI, tests) can still construct narrower principals.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode
from urllib.request import Request, urlopen

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
    """A signed-in member (the hub username), or an in-process caller's name, with every capability."""
    return Principal((name or "").strip()[:40] or "owner", frozenset(CAPABILITIES))


OWNER_CLI = Principal("owner-cli", frozenset(CAPABILITIES))


# ---------------------------------------------------------------- JupyterHub sign-in

# Read from the process environment only: on `codeit` that is /etc/bidmate/server.env (runbook 3.1).
HUB_ENV = {"hub_url": "BIDMATE_HUB_URL", "client_id": "BIDMATE_OAUTH_CLIENT_ID",
           "client_secret": "BIDMATE_OAUTH_CLIENT_SECRET", "redirect_uri": "BIDMATE_OAUTH_REDIRECT_URI",
           "allowed": "BIDMATE_ALLOWED_USERS"}
HUB_API_ENV = "BIDMATE_HUB_API_URL"  # where the server itself reaches the hub; default BIDMATE_HUB_URL
# Local development only: every caller is this name and nobody signs in. Never in server.env or the systemd unit.
LOCAL_MEMBER_ENV = "BIDMATE_LOCAL_MEMBER"
SESSION_SECONDS = 12 * 3600
HUB_TIMEOUT_SECONDS = 10


class LoginError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Hub:
    hub_url: str  # where the browser is sent
    api_url: str  # where this server calls
    client_id: str
    client_secret: str
    redirect_uri: str
    allowed: frozenset[str]

    def authorize_url(self, state: str) -> str:
        return f"{self.hub_url}/hub/api/oauth2/authorize?" + urlencode(
            {"client_id": self.client_id, "redirect_uri": self.redirect_uri, "response_type": "code",
             "state": state})

    def username(self, code: str) -> str:
        """Exchanges the code at the hub's token endpoint, then asks the hub whose token it is."""
        form = urlencode({"client_id": self.client_id, "client_secret": self.client_secret,
                          "grant_type": "authorization_code", "code": code,
                          "redirect_uri": self.redirect_uri}).encode()
        try:
            token = _json(Request(f"{self.api_url}/hub/api/oauth2/token", data=form, method="POST"))["access_token"]
            name = _json(Request(f"{self.api_url}/hub/api/user", headers={"Authorization": f"Bearer {token}"}))["name"]
        except (OSError, ValueError, KeyError, TypeError):  # URLError and HTTPError are OSErrors
            raise LoginError(502, "JupyterHub에서 로그인을 확인하지 못했습니다. 다시 로그인하세요.") from None
        if not isinstance(name, str) or not name:
            raise LoginError(502, "JupyterHub가 사용자 이름을 알려 주지 않았습니다.")
        return name


def _json(request: Request) -> dict:
    with urlopen(request, timeout=HUB_TIMEOUT_SECONDS) as response:  # noqa: S310 - the configured hub URL
        return json.loads(response.read())


class Login:
    """How this process knows who calls: the hub (with sessions), one fixed local name, or nobody (`problem`), in
    which case every API call is refused."""

    def __init__(self, hub: Hub | None = None, local_member: str | None = None, problem: str | None = None) -> None:
        self.hub, self.local_member, self.problem = hub, local_member, problem
        self._lock = threading.Lock()
        self._sessions: dict[str, tuple[str, float]] = {}  # token -> (hub username, expiry); memory only

    @classmethod
    def from_env(cls, environ=os.environ) -> Login:
        values = {k: environ.get(v, "").strip() for k, v in HUB_ENV.items()}
        local = environ.get(LOCAL_MEMBER_ENV, "").strip()
        if local and any(values.values()):
            return cls(problem=f"{LOCAL_MEMBER_ENV}와 JupyterHub 로그인 설정이 함께 있습니다. {LOCAL_MEMBER_ENV}를 지우세요.")
        if local:
            return cls(local_member=local[:40])
        missing = [HUB_ENV[k] for k, v in values.items() if not v]
        if missing:
            return cls(problem="로그인이 설정되지 않아 API를 열지 않습니다. 없는 설정: " + ", ".join(missing))
        allowed = frozenset(n.strip() for n in values["allowed"].split(",") if n.strip())
        hub_url = values["hub_url"].rstrip("/")
        return cls(Hub(hub_url, (environ.get(HUB_API_ENV, "").strip() or hub_url).rstrip("/"), values["client_id"],
                       values["client_secret"], values["redirect_uri"], allowed))

    def sign_in(self, code: str) -> str:
        """A new session token for the hub user this code belongs to, if that user is on the allowlist."""
        name = self.hub.username(code)
        if name not in self.hub.allowed:
            raise LoginError(403, f"{name} 계정은 입찰메이트 사용자 목록에 없습니다.")
        token, now = secrets.token_urlsafe(32), time.time()
        with self._lock:
            self._sessions = {t: s for t, s in self._sessions.items() if s[1] > now}
            self._sessions[token] = (name, now + SESSION_SECONDS)
        return token

    def member(self, token: str | None) -> str | None:
        if self.local_member:
            return self.local_member
        with self._lock:
            name, expires = self._sessions.get(token or "", ("", 0.0))
        return name if expires > time.time() else None

    def sign_out(self, token: str | None) -> None:
        with self._lock:
            self._sessions.pop(token or "", None)
