"""Real HTTP endpoint for Swiggy's OAuth redirect, and Render's health check.

Socket Mode gives the bot no inbound HTTP surface, so this is a tiny stdlib
server run in a background thread — one GET route isn't worth a whole web
framework dependency.
"""

from __future__ import annotations

import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from canteen import auth

_OK = b"Swiggy connected. You can close this tab and go back to Slack."


def start(port: int, db, http, notify) -> ThreadingHTTPServer:
    """notify(user_id, text) DMs the person once their sign-in lands here."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urllib.parse.urlparse(self.path)
            if path.path != "/callback":
                self._reply(200, b"ok")  # Render's health check hits "/"
                return

            params = urllib.parse.parse_qs(path.query)
            state = (params.get("state") or [None])[0]
            code = (params.get("code") or [None])[0]
            error = (params.get("error") or [None])[0]
            try:
                if error:
                    raise auth.LinkFailed(f"Swiggy refused the sign-in: {error}.")
                if not state or not code:
                    raise auth.LinkFailed("Missing code or state.")
                user_id = auth.complete_link_by_state(db(), http, state, code, time.time())
            except auth.LinkFailed as exc:
                self._reply(400, str(exc).encode())
                return
            self._reply(200, _OK)
            notify(user_id, "Swiggy connected :white_check_mark: — what do you feel like?")

        def _reply(self, status, body):
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass  # ponytail: default access log is just request-line noise on
            # top of canteen's own logger; add it back if you need to audit hits.

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
