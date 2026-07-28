import pytest

from canteen import auth, store


def fresh(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_schema(conn)
    return conn


class FakeHTTP:
    """Stands in for httpx.Client. Records posts, returns canned JSON."""

    def __init__(self, payload=None, status=200):
        self.payload = payload or {}
        self.status = status
        self.posts = []

    def post(self, url, data=None, json=None):
        self.posts.append({"url": url, "data": data, "json": json})
        status, payload = self.status, self.payload

        class Response:
            status_code = status

            @staticmethod
            def json():
                return payload

            @staticmethod
            def raise_for_status():
                if status >= 400:
                    raise RuntimeError(f"HTTP {status}")

        return Response()


def pending_state(conn):
    return conn.execute("select state from pending_auth").fetchone()["state"]


def test_pkce_challenge_is_derived_from_the_verifier():
    verifier, challenge = auth.generate_pkce()
    again = auth.generate_pkce()
    assert verifier != challenge
    assert "=" not in challenge  # base64url, unpadded
    assert verifier != again[0]  # fresh every time


def test_authorize_url_carries_everything_swiggy_needs():
    url = auth.authorize_url("chal", "st-1")
    assert url.startswith("https://mcp.swiggy.com/auth/authorize?")
    for fragment in ("response_type=code", "client_id=swiggy-mcp",
                     "code_challenge=chal", "code_challenge_method=S256",
                     "state=st-1", "localhost%3A8765%2Fcallback"):
        assert fragment in url, fragment


def test_parse_callback_reads_a_pasted_error_url():
    got = auth.parse_callback(
        "http://localhost:8765/callback?code=abc123&state=st-1")
    assert got == {"code": "abc123", "state": "st-1"}


def test_parse_callback_tolerates_surrounding_chat_text():
    """People paste with a sentence around it, or with Slack's angle brackets."""
    got = auth.parse_callback(
        "here you go <http://localhost:8765/callback?code=abc&state=st> thanks")
    assert got == {"code": "abc", "state": "st"}


def test_parse_callback_survives_slacks_ampersand_escaping():
    """Slack rewrites & to &amp; in message text. Unhandled, parse_qs names the
    second parameter 'amp;state' and every paste fails the state check."""
    got = auth.parse_callback(
        "<http://localhost:8765/callback?code=abc&amp;state=st-1>")
    assert got == {"code": "abc", "state": "st-1"}


def test_parse_callback_surfaces_an_oauth_error():
    got = auth.parse_callback(
        "http://localhost:8765/callback?error=access_denied&state=st")
    assert got == {"error": "access_denied", "state": "st"}


def test_parse_callback_returns_nothing_for_ordinary_chat():
    assert auth.parse_callback("what's for lunch?") == {}
    assert auth.parse_callback("http://localhost:8765/callback") == {}


def test_begin_link_stores_the_verifier_against_the_user(tmp_path):
    conn = fresh(tmp_path)
    url = auth.begin_link(conn, "U1", now=1000.0)
    pending = store.take_pending(conn, "U1")
    assert pending["verifier"]
    assert f"state={pending['state']}" in url


def test_complete_link_exchanges_the_code_and_saves_the_token(tmp_path):
    conn = fresh(tmp_path)
    auth.begin_link(conn, "U1", now=1000.0)
    state = pending_state(conn)
    http = FakeHTTP({"access_token": "acc", "refresh_token": "ref",
                     "expires_in": 100})

    auth.complete_link(
        conn, http, "U1",
        f"http://localhost:8765/callback?code=xyz&state={state}", now=1000.0)

    saved = store.get_token(conn, "U1")
    assert saved["access_token"] == "acc"
    assert saved["expires_at"] == 1100.0
    sent = http.posts[0]["data"]
    assert sent["grant_type"] == "authorization_code"
    assert sent["code"] == "xyz"
    assert sent["code_verifier"]  # PKCE proof travels with the exchange


def test_complete_link_rejects_a_mismatched_state(tmp_path):
    """Without this check, anyone could paste a code minted for another app."""
    conn = fresh(tmp_path)
    auth.begin_link(conn, "U1", now=1000.0)
    http = FakeHTTP()
    with pytest.raises(auth.LinkFailed, match="did not match"):
        auth.complete_link(
            conn, http, "U1",
            "http://localhost:8765/callback?code=xyz&state=wrong", now=1000.0)
    assert http.posts == []  # nothing was exchanged


def test_complete_link_refuses_an_expired_pending_record(tmp_path):
    conn = fresh(tmp_path)
    auth.begin_link(conn, "U1", now=1000.0)
    state = pending_state(conn)
    later = 1000.0 + auth.PENDING_TTL_SECONDS + 1
    with pytest.raises(auth.LinkFailed, match="expired"):
        auth.complete_link(
            conn, FakeHTTP(), "U1",
            f"http://localhost:8765/callback?code=xyz&state={state}", now=later)


def test_complete_link_without_a_pending_record_is_refused(tmp_path):
    conn = fresh(tmp_path)
    with pytest.raises(auth.LinkFailed, match="Start again"):
        auth.complete_link(
            conn, FakeHTTP(), "U1",
            "http://localhost:8765/callback?code=xyz&state=st", now=1000.0)


def test_complete_link_reports_a_denied_authorisation(tmp_path):
    conn = fresh(tmp_path)
    auth.begin_link(conn, "U1", now=1000.0)
    state = pending_state(conn)
    with pytest.raises(auth.LinkFailed, match="access_denied"):
        auth.complete_link(
            conn, FakeHTTP(), "U1",
            f"http://localhost:8765/callback?error=access_denied&state={state}",
            now=1000.0)


def test_valid_token_returns_a_live_token_without_calling_out(tmp_path):
    conn = fresh(tmp_path)
    store.save_token(conn, "U1", "acc", "ref", 9999.0)
    http = FakeHTTP()
    assert auth.valid_token(conn, http, "U1", now=1000.0) == "acc"
    assert http.posts == []


def test_valid_token_refreshes_inside_the_margin(tmp_path):
    conn = fresh(tmp_path)
    store.save_token(conn, "U1", "old", "ref", 1200.0)
    http = FakeHTTP({"access_token": "new", "expires_in": 500})
    assert auth.valid_token(conn, http, "U1", now=1000.0) == "new"
    assert store.get_token(conn, "U1")["access_token"] == "new"
    # A response that omits refresh_token must not wipe the one we hold.
    assert store.get_token(conn, "U1")["refresh_token"] == "ref"


def test_valid_token_raises_when_the_person_never_connected(tmp_path):
    conn = fresh(tmp_path)
    with pytest.raises(auth.NotConnected):
        auth.valid_token(conn, FakeHTTP(), "U1", now=1000.0)


def test_a_failed_refresh_clears_the_token_so_the_user_reconnects(tmp_path):
    """Leaving a dead token on file makes every later call fail the same way
    with no path out."""
    conn = fresh(tmp_path)
    store.save_token(conn, "U1", "old", "ref", 1200.0)
    with pytest.raises(auth.NotConnected):
        auth.valid_token(conn, FakeHTTP(status=400), "U1", now=1000.0)
    assert store.get_token(conn, "U1") is None
