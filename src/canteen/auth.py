"""Per-user Swiggy OAuth 2.1 + PKCE, without a public redirect URI.

Socket Mode gives us no public URL, and Swiggy only allows http/https
redirects — so there is nothing for Swiggy to redirect *to* that we can read.
The paste flow closes that gap: the person is sent to a localhost redirect that
their browser cannot load, and they paste the failed URL back into the DM. The
authorization code is in its query string.

That is safe because of PKCE. The code alone is worthless: redeeming it also
requires the verifier, which never leaves this process, and the code is
single-use and expires in 120 seconds.
"""

from __future__ import annotations

import base64
import hashlib
import html
import os
import re
import secrets
import urllib.parse

from canteen import store

AUTH_BASE = "https://mcp.swiggy.com"
# Registration always returns this same id regardless of what we send, so there
# is nothing gained by calling POST /auth/register at runtime.
CLIENT_ID = "swiggy-mcp"
# In production this is the deployed domain's real /callback (see callback.py);
# swiggy.com won't accept a redirect_uri unless it's registered as this exact
# value, so it must match whatever's actually deployed.
REDIRECT_URI = os.environ.get("CANTEEN_REDIRECT_URI", "http://localhost:8765/callback")

PENDING_TTL_SECONDS = 600        # how long a person has to finish signing in
REFRESH_MARGIN_SECONDS = 300     # refresh this long before actual expiry
DEFAULT_TOKEN_LIFETIME = 432000  # Swiggy access tokens last 5 days

_CALLBACK = re.compile(r"https?://[^\s<>|]*/callback\?([^\s<>|]+)")


class NotConnected(RuntimeError):
    """This person has no usable Swiggy token. They must run the link flow."""


class LinkFailed(RuntimeError):
    """The paste could not be turned into a token. The message is user-facing."""


def generate_pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return verifier, challenge


def authorize_url(challenge: str, state: str) -> str:
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })
    return f"{AUTH_BASE}/auth/authorize?{query}"


def parse_callback(text: str) -> dict:
    """Pull code/state (or error/state) out of a pasted redirect URL.

    People paste with a sentence wrapped around it, and Slack wraps bare URLs
    in angle brackets, so this scans rather than parses the whole message.

    Slack also escapes & as &amp; in message text. Unescaped, parse_qs reads the
    second parameter as "amp;state" and the state check compares against None.
    """
    match = _CALLBACK.search(html.unescape(text or ""))
    if not match:
        return {}
    params = urllib.parse.parse_qs(match.group(1))
    got = {k: v[0] for k, v in params.items() if k in ("code", "state", "error")}
    return got if ("code" in got or "error" in got) else {}


def begin_link(conn, user_id: str, now: float) -> str:
    """Start a link for this person and return the URL they must open."""
    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(16)
    store.save_pending(conn, user_id, verifier, state, now)
    return authorize_url(challenge, state)


def _redeem_code(http, code: str, verifier: str) -> dict:
    response = http.post(
        AUTH_BASE + "/auth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
        },
    )
    response.raise_for_status()
    return response.json()


def complete_link(conn, http, user_id: str, pasted: str, now: float) -> None:
    """Turn a pasted redirect URL into a stored token for this person."""
    got = parse_callback(pasted)
    if not got:
        raise LinkFailed("That doesn't look like the redirect URL.")

    pending = store.take_pending(conn, user_id)
    if not pending:
        raise LinkFailed("I have no sign-in waiting for you. Start again.")
    if now - pending["created_at"] > PENDING_TTL_SECONDS:
        raise LinkFailed("That sign-in expired. Start again.")
    if got.get("state") != pending["state"]:
        raise LinkFailed("The sign-in did not match the one I started.")
    if "error" in got:
        raise LinkFailed(f"Swiggy refused the sign-in: {got['error']}.")

    payload = _redeem_code(http, got["code"], pending["verifier"])
    store.save_token(
        conn, user_id,
        payload["access_token"],
        payload.get("refresh_token"),
        now + payload.get("expires_in", DEFAULT_TOKEN_LIFETIME),
    )


def complete_link_by_state(conn, http, state: str, code: str, now: float) -> str:
    """Same job as complete_link, for the real HTTP /callback: Swiggy hits it
    directly with code/state in the query string, no Slack user_id in sight —
    the state token is the only thing tying it back to whoever started this.
    Returns the user_id so the caller can tell them they're connected."""
    pending = store.take_pending_by_state(conn, state)
    if not pending:
        raise LinkFailed("No sign-in waiting for that state — it may have expired.")
    if now - pending["created_at"] > PENDING_TTL_SECONDS:
        raise LinkFailed("That sign-in expired. Start again.")

    payload = _redeem_code(http, code, pending["verifier"])
    store.save_token(
        conn, pending["user_id"],
        payload["access_token"],
        payload.get("refresh_token"),
        now + payload.get("expires_in", DEFAULT_TOKEN_LIFETIME),
    )
    return pending["user_id"]


def valid_token(conn, http, user_id: str, now: float) -> str:
    """The only sanctioned way to obtain this person's Swiggy token."""
    row = store.get_token(conn, user_id)
    if not row:
        raise NotConnected("No Swiggy account linked for this person.")
    if row["expires_at"] - now > REFRESH_MARGIN_SECONDS:
        return row["access_token"]
    if not row["refresh_token"]:
        store.delete_token(conn, user_id)
        raise NotConnected("Swiggy session expired and there is nothing to refresh.")

    try:
        response = http.post(
            AUTH_BASE + "/auth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": row["refresh_token"],
                "client_id": CLIENT_ID,
            },
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        # A dead token on file makes every later call fail identically with no
        # way out, so drop it and make the person reconnect.
        store.delete_token(conn, user_id)
        raise NotConnected(f"Swiggy sign-in expired: {exc}") from exc

    store.save_token(
        conn, user_id,
        payload["access_token"],
        payload.get("refresh_token", row["refresh_token"]),
        now + payload.get("expires_in", DEFAULT_TOKEN_LIFETIME),
    )
    return payload["access_token"]
