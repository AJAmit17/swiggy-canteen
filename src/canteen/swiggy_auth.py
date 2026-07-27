"""Swiggy MCP OAuth 2.1 + PKCE with dynamic client registration.

One host account authenticates for the whole workspace, so there is exactly
one token row. Localhost redirects are allowed by Swiggy for development.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import threading
import time
import urllib.parse
import webbrowser

import httpx

from canteen import db

AUTH_BASE = "https://mcp.swiggy.com"
REDIRECT_URI = "http://localhost:8765/callback"
CALLBACK_PORT = 8765
REFRESH_MARGIN_SECONDS = 300  # refresh this long before actual expiry
DEFAULT_TOKEN_LIFETIME = 432000  # Swiggy access tokens last 5 days


class NotAuthenticated(RuntimeError):
    """No usable Swiggy token on file — an admin must run the login flow."""


def generate_pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return verifier, challenge


def register_client(http) -> str:
    resp = http.post(
        AUTH_BASE + "/auth/register",
        json={
            "client_name": "Swiggy Canteen (Slack)",
            "redirect_uris": [REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    resp.raise_for_status()
    return resp.json()["client_id"]


def authorize_url(client_id: str, challenge: str, state: str) -> str:
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })
    return f"{AUTH_BASE}/auth/authorize?{query}"


def exchange_code(http, client_id: str, code: str, verifier: str) -> dict:
    resp = http.post(
        AUTH_BASE + "/auth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    resp.raise_for_status()
    return resp.json()


def refresh_token(http, client_id: str, refresh: str) -> dict:
    resp = http.post(
        AUTH_BASE + "/auth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": client_id,
        },
    )
    resp.raise_for_status()
    return resp.json()


def valid_token(conn, http, client_id: str) -> str:
    """The only way the rest of the app should obtain a Swiggy token."""
    row = db.get_token(conn)
    if not row:
        raise NotAuthenticated("No Swiggy account linked. Run the login flow.")
    if row["expires_at"] - time.time() > REFRESH_MARGIN_SECONDS:
        return row["access_token"]
    if not row["refresh_token"]:
        raise NotAuthenticated("Swiggy token expired and no refresh token is on file.")
    payload = refresh_token(http, client_id, row["refresh_token"])
    db.save_token(
        conn,
        payload["access_token"],
        payload.get("refresh_token", row["refresh_token"]),
        time.time() + payload.get("expires_in", DEFAULT_TOKEN_LIFETIME),
    )
    return payload["access_token"]


# --- interactive login (run once by an admin, from a terminal) ---

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):  # noqa: N802 - stdlib naming
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.result = {k: v[0] for k, v in params.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h2>Swiggy linked. You can close this tab.</h2>")

    def log_message(self, *args):
        return


def login(conn) -> str:
    """Blocking browser login. Returns the client_id, saves the token."""
    with httpx.Client(timeout=30) as client:
        client_id = register_client(client)
        verifier, challenge = generate_pkce()
        state = secrets.token_urlsafe(16)

        server = http.server.HTTPServer(("localhost", CALLBACK_PORT), _CallbackHandler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()

        url = authorize_url(client_id, challenge, state)
        print(f"Open this to link the Swiggy account:\n{url}")
        webbrowser.open(url)
        # The auth code lives 120 seconds; give the human a little longer to click.
        thread.join(timeout=180)
        server.server_close()

        result = _CallbackHandler.result
        if result.get("state") != state:
            raise RuntimeError("OAuth state mismatch — aborting.")
        if "code" not in result:
            raise RuntimeError(f"No authorization code returned: {result}")

        payload = exchange_code(client, client_id, result["code"], verifier)
        db.save_token(
            conn,
            payload["access_token"],
            payload.get("refresh_token"),
            time.time() + payload.get("expires_in", DEFAULT_TOKEN_LIFETIME),
        )
        return client_id
