"""A stand-in for the two unauthenticated GitHub routes the update check reads (service/update.py), for the
tools/verify.py update-banner flow: over real HTTP on loopback, main's head is `latest`, two commits ahead of
`running`, with the titles in TITLES."""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from rfp_assistant.service import update

TITLES = ("검증 화면 결론 먼저", "업데이트 버튼 추가")


def serve(running: str, latest: str) -> ThreadingHTTPServer:
    shas = [hashlib.sha1(b"update-banner-middle").hexdigest(), latest]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def do_GET(self) -> None:
            if self.path == f"/repos/{update.REPO}/commits/main":
                body, kind = latest.encode(), "text/plain"
            elif self.path == f"/repos/{update.REPO}/compare/{running}...{latest}":
                body, kind = json.dumps({"status": "ahead", "ahead_by": 2, "behind_by": 0, "commits": [
                    {"sha": s, "commit": {"message": t}} for s, t in zip(shas, TITLES)]}).encode(), "application/json"
            else:
                self.send_response(404)
                return self.end_headers()
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
