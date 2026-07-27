import base64
import hashlib
import time
from urllib.parse import parse_qs, urlparse

import pytest

from canteen import db
from canteen import swiggy_auth as sa


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeHttp:
    """Records posts and replays queued responses."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return FakeResponse(self.responses.pop(0))


def test_pkce_challenge_is_the_s256_of_the_verifier():
    verifier, challenge = sa.generate_pkce()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    assert challenge == expected
    assert 43 <= len(verifier) <= 128


def test_pkce_is_not_a_constant():
    assert sa.generate_pkce()[0] != sa.generate_pkce()[0]


def test_authorize_url_carries_every_required_oauth_param():
    url = sa.authorize_url("cid", "chal", "st8")
    q = parse_qs(urlparse(url).query)
    assert q["response_type"] == ["code"]
    assert q["client_id"] == ["cid"]
    assert q["code_challenge"] == ["chal"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["state"] == ["st8"]
    assert q["redirect_uri"] == [sa.REDIRECT_URI]


def test_register_client_posts_the_localhost_redirect_and_returns_the_id():
    http = FakeHttp({"client_id": "cid-123"})
    assert sa.register_client(http) == "cid-123"
    url, kwargs = http.posts[0]
    assert url == sa.AUTH_BASE + "/auth/register"
    assert kwargs["json"]["redirect_uris"] == [sa.REDIRECT_URI]


def test_exchange_code_sends_the_verifier_and_returns_the_token_payload():
    http = FakeHttp({"access_token": "acc", "refresh_token": "ref", "expires_in": 100})
    out = sa.exchange_code(http, "cid", "the-code", "the-verifier")
    assert out["access_token"] == "acc"
    body = http.posts[0][1]["data"]
    assert body["grant_type"] == "authorization_code"
    assert body["code_verifier"] == "the-verifier"
    assert body["code"] == "the-code"


def test_valid_token_returns_the_stored_token_when_it_is_fresh(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    db.save_token(conn, "fresh", "ref", time.time() + 86400)
    http = FakeHttp()
    assert sa.valid_token(conn, http, "cid") == "fresh"
    assert http.posts == []  # no refresh attempted


def test_valid_token_refreshes_when_close_to_expiry_and_persists_the_new_one(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    db.save_token(conn, "stale", "ref", time.time() + 10)
    http = FakeHttp({"access_token": "brand-new", "refresh_token": "ref2",
                     "expires_in": 432000})
    assert sa.valid_token(conn, http, "cid") == "brand-new"
    assert db.get_token(conn)["access_token"] == "brand-new"
    assert http.posts[0][1]["data"]["grant_type"] == "refresh_token"


def test_valid_token_raises_a_named_error_when_no_token_is_stored(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    with pytest.raises(sa.NotAuthenticated):
        sa.valid_token(conn, FakeHttp(), "cid")
