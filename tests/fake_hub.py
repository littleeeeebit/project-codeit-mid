"""A stand-in for JupyterHub's OAuth provider, for tests and tools/verify.py browser flows.

It answers the three hub endpoints BidMate uses, over real HTTP on loopback: /hub/api/oauth2/authorize (a form
that asks for a username, in place of the hub's own login), /hub/api/oauth2/token (single-use codes, checked
against the client) and /hub/api/user. Nothing here checks a password: whoever fills the form is that user.
"""

from __future__ import annotations

import html
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

from rfp_assistant.service import auth

CLIENT_ID, CLIENT_SECRET = "service-bidmate", "fake-hub-secret"
TEST_CALLBACK = "http://testserver/api/auth/callback"  # where TestClient's app lives


class FakeHub:
    def __init__(self) -> None:
        self.codes: dict[str, tuple[str, str]] = {}  # code -> (username, redirect_uri), consumed by the exchange
        self.tokens: dict[str, str] = {}
        self.lock = threading.Lock()
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_GET(self) -> None:
                url = urlsplit(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                if url.path == "/hub/api/oauth2/authorize":
                    if query.get("client_id") != CLIENT_ID or query.get("response_type") != "code":
                        return self.reply(400, {"error": "invalid_client"})
                    fields = "".join(f'<input type="hidden" name="{k}" value="{html.escape(v)}">'
                                     for k, v in query.items())
                    return self.reply(200, f'<!doctype html><html lang="ko"><title>Fake JupyterHub</title>'
                                           f'<form method="post">{fields}<label>Username <input name="username">'
                                           f'</label><button type="submit">Sign in</button></form></html>')
                if url.path == "/hub/api/user":
                    with hub.lock:
                        name = hub.tokens.get(self.headers.get("Authorization", "").removeprefix("Bearer "))
                    return self.reply(200, {"name": name, "kind": "user"}) if name else self.reply(403, {})
                self.reply(404, {})

            def do_POST(self) -> None:
                url = urlsplit(self.path)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
                if url.path == "/hub/api/oauth2/authorize":  # the form above, filled in
                    self.send_response(302)
                    self.send_header("Location", hub.grant(form, form.get("username", "")))
                    return self.end_headers()
                if url.path == "/hub/api/oauth2/token":
                    with hub.lock:
                        name, redirect = hub.codes.pop(form.get("code", ""), ("", ""))
                        if not (name and (form.get("client_id"), form.get("client_secret")) == (CLIENT_ID, CLIENT_SECRET)
                                and form.get("grant_type") == "authorization_code"
                                and form.get("redirect_uri") == redirect):
                            return self.reply(400, {"error": "invalid_grant"})
                        token = secrets.token_hex(16)
                        hub.tokens[token] = name
                    return self.reply(200, {"access_token": token, "token_type": "Bearer"})
                self.reply(404, {})

            def reply(self, status: int, body) -> None:
                data = (body if isinstance(body, str) else json.dumps(body)).encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8" if isinstance(body, str)
                                 else "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def grant(self, query: dict, name: str) -> str:
        """The callback URL the hub sends a signed-in `name` back to."""
        code = secrets.token_hex(16)
        with self.lock:
            self.codes[code] = (name, query["redirect_uri"])
        return f"{query['redirect_uri']}?" + urlencode({"code": code, "state": query.get("state", "")})

    def env(self, redirect_uri: str, allowed) -> dict[str, str]:
        """The server.env variables that point BidMate at this hub."""
        return {"BIDMATE_HUB_URL": self.url, "BIDMATE_OAUTH_CLIENT_ID": CLIENT_ID,
                "BIDMATE_OAUTH_CLIENT_SECRET": CLIENT_SECRET, "BIDMATE_OAUTH_REDIRECT_URI": redirect_uri,
                "BIDMATE_ALLOWED_USERS": ",".join(allowed)}

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


_shared: FakeHub | None = None


def hub() -> FakeHub:
    """One hub per process, started on first use."""
    global _shared
    if _shared is None:
        _shared = FakeHub()
    return _shared


def login(*allowed: str) -> auth.Login:
    """A test app's sign-in through the shared fake hub, for TestClient's address."""
    return auth.Login.from_env(hub().env(TEST_CALLBACK, allowed))


def client(res, name: str):
    """A TestClient for an app over `res`, signed in as `name`. Enter it to run the app's lifespan."""
    from fastapi.testclient import TestClient

    from rfp_assistant import api

    signed_in = TestClient(api.create_app(res, login(name)))
    sign_in(signed_in, name)
    return signed_in


def browser_sign_in(page, origin: str, name: str) -> None:
    """Signs a Playwright page in as `name`: the app's sign-in entry, this hub's form, back on 질문하기."""
    page.goto(origin + "/")
    page.get_by_role("link", name="JupyterHub 계정으로 로그인").click(timeout=60000)
    page.get_by_label("Username").fill(name)
    page.get_by_role("button", name="Sign in").click()
    page.get_by_role("heading", name="질문하기").wait_for(timeout=60000)


def sign_in(client, name: str):
    """Signs a TestClient in as `name` the way a browser does: login, the hub's consent, the callback. Returns the
    callback's response; a refused one leaves the client without a session."""
    to_hub = client.get("/api/auth/login", follow_redirects=False)
    assert to_hub.status_code == 303, to_hub.text
    query = {k: v[0] for k, v in parse_qs(urlsplit(to_hub.headers["location"]).query).items()}
    return client.get(hub().grant(query, name), follow_redirects=False)
