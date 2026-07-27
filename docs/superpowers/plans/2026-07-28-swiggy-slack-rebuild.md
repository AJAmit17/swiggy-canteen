# Swiggy Slack Assistant Rebuild — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the canteen-brain build with a Slack app where a DM is a personal Swiggy assistant on your own account and a channel runs three group flows, against the real Swiggy MCP contract.

**Architecture:** Slack Bolt in Socket Mode routes DMs to a single conversational agent call and mentions to one of three group flows. Gemini's Interactions API reaches Swiggy's three MCP servers server-side, so no MCP client exists here. Swiggy owns the cart, Gemini owns the transcript, and SQLite holds only a per-user token, a preference line, an interaction id and any live group flow.

**Tech Stack:** Python 3.12, uv, slack-bolt (Socket Mode), google-genai (Interactions API), httpx, SQLite via stdlib `sqlite3`, pytest.

## Global Constraints

- Model id: `gemini-3.6-flash`, overridable with `CANTEEN_MODEL`.
- MCP server names must be lowercase snake_case: `swiggy_food`, `swiggy_im`, `swiggy_dineout`. Gemini 400s otherwise.
- Swiggy response fields are camelCase: `addressId`, `restaurantId`, `itemId`, `spinId`, `slotId`, `orderId`, `bookingId`, `guestCount`, `availabilityStatus`, `availability`.
- Food: only `availabilityStatus == "OPEN"`; cart is single-restaurant; **₹1000 cap**; COD only, so only coupons with `requiresOnlinePayment == false`.
- Instamart: **₹99 minimum**; COD only; cart is address-locked.
- Dineout: only `availability == "AVAILABLE"`; 7-day forward window; IST; `date` is `YYYY-MM-DD`.
- `place_food_order`, `checkout`, `book_table` are **not idempotent**. Never retry. On failure call `get_food_orders` / `get_orders` / `get_booking_status` and report what actually happened.
- Spend tools must be absent from `allowed_tools` unless the call was triggered by a button click.
- Never cache cart contents. Any turn that may touch a cart calls `get_food_cart` / `get_cart` first.
- Never state that a dish is safe for an allergy — Swiggy menu data has no allergen field.
- sqlite3 connections are per-thread (`threading.local`). Bolt dispatches listeners on a pool.
- Every model reply passes through `slackfmt.to_mrkdwn` before reaching Slack.
- Tests use plain asserts, no network, no fixtures beyond `tmp_path`.

## File Structure

| File | Responsibility |
|---|---|
| `src/canteen/store.py` | SQLite. Thread-local connection, five tables, all persistence. |
| `src/canteen/auth.py` | Per-user OAuth 2.1 + PKCE, the paste flow, token refresh. |
| `src/canteen/agent.py` | Gemini Interactions API over Swiggy MCP; spend allowlist; local tools; money guards. |
| `src/canteen/blocks.py` | Block Kit builders. Pure. |
| `src/canteen/slackfmt.py` | Markdown → mrkdwn. Pure. **Unchanged.** |
| `src/canteen/group.py` | The three group flows and their Slack handlers. |
| `src/canteen/app.py` | Bolt app, routing, DM path, error handling, progress indicator. |

Deleted: `brain.py`, `lunch.py`, `pantry.py`, `dineout.py`, `parsing.py`, `db.py`, `swiggy_auth.py` and their tests.

---

### Task 1: Strip the old build and lay the new schema

**Files:**
- Delete: `src/canteen/brain.py`, `src/canteen/lunch.py`, `src/canteen/pantry.py`, `src/canteen/dineout.py`, `src/canteen/parsing.py`, `src/canteen/db.py`, `src/canteen/swiggy_auth.py`
- Delete: `tests/test_brain.py`, `tests/test_lunch.py`, `tests/test_pantry.py`, `tests/test_dineout.py`, `tests/test_parsing.py`, `tests/test_db.py`, `tests/test_swiggy_auth.py`, `tests/test_blocks.py`, `tests/test_app_wiring.py`
- Create: `src/canteen/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `store.connect(path=None) -> sqlite3.Connection`, `store.init_schema(conn) -> None`, `store.save_token(conn, user_id, access_token, refresh_token, expires_at) -> None`, `store.get_token(conn, user_id) -> dict | None`, `store.delete_token(conn, user_id) -> None`, `store.save_pending(conn, user_id, verifier, state, created_at) -> None`, `store.take_pending(conn, user_id) -> dict | None`, `store.set_preference(conn, user_id, note) -> None`, `store.get_preference(conn, user_id) -> str | None`, `store.set_interaction(conn, key, interaction_id, updated_at) -> None`, `store.get_interaction(conn, key) -> str | None`, `store.clear_interaction(conn, key) -> None`, `store.save_group(conn, channel_id, kind, host_user_id, message_ts, context, created_at) -> None`, `store.get_group(conn, channel_id) -> dict | None`, `store.set_group_context(conn, channel_id, context) -> None`, `store.delete_group(conn, channel_id) -> None`

- [ ] **Step 1: Delete the superseded modules and their tests**

```bash
git rm src/canteen/brain.py src/canteen/lunch.py src/canteen/pantry.py \
       src/canteen/dineout.py src/canteen/parsing.py src/canteen/db.py \
       src/canteen/swiggy_auth.py
git rm tests/test_brain.py tests/test_lunch.py tests/test_pantry.py \
       tests/test_dineout.py tests/test_parsing.py tests/test_db.py \
       tests/test_swiggy_auth.py tests/test_blocks.py tests/test_app_wiring.py
```

`src/canteen/app.py` and `src/canteen/blocks.py` are now broken. They are rewritten in Tasks 5 and 4. `tests/test_slackfmt.py` and `tests/test_agent.py` stay.

- [ ] **Step 2: Write the failing test**

Create `tests/test_store.py`:

```python
import threading

from canteen import store


def fresh(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_schema(conn)
    return conn


def test_tokens_are_per_user(tmp_path):
    conn = fresh(tmp_path)
    store.save_token(conn, "U1", "acc-1", "ref-1", 1800000000.0)
    store.save_token(conn, "U2", "acc-2", "ref-2", 1800000001.0)
    assert store.get_token(conn, "U1")["access_token"] == "acc-1"
    assert store.get_token(conn, "U2")["access_token"] == "acc-2"
    assert store.get_token(conn, "U3") is None


def test_saving_a_token_twice_replaces_it(tmp_path):
    conn = fresh(tmp_path)
    store.save_token(conn, "U1", "old", "r", 1.0)
    store.save_token(conn, "U1", "new", "r", 2.0)
    assert store.get_token(conn, "U1")["access_token"] == "new"
    assert conn.execute("select count(*) from swiggy_token").fetchone()[0] == 1


def test_deleting_a_token_forces_a_reconnect(tmp_path):
    conn = fresh(tmp_path)
    store.save_token(conn, "U1", "acc", "ref", 1.0)
    store.delete_token(conn, "U1")
    assert store.get_token(conn, "U1") is None


def test_pending_auth_can_only_be_taken_once(tmp_path):
    """The auth code is single-use, so the record that authorises it must be
    too — otherwise a replayed paste re-enters the exchange."""
    conn = fresh(tmp_path)
    store.save_pending(conn, "U1", "verifier", "state-abc", 1000.0)
    first = store.take_pending(conn, "U1")
    assert first["verifier"] == "verifier"
    assert first["state"] == "state-abc"
    assert store.take_pending(conn, "U1") is None


def test_starting_a_second_link_replaces_the_first(tmp_path):
    conn = fresh(tmp_path)
    store.save_pending(conn, "U1", "v1", "s1", 1000.0)
    store.save_pending(conn, "U1", "v2", "s2", 2000.0)
    assert store.take_pending(conn, "U1")["state"] == "s2"


def test_preferences_round_trip(tmp_path):
    conn = fresh(tmp_path)
    assert store.get_preference(conn, "U1") is None
    store.set_preference(conn, "U1", "vegetarian, no mushroom")
    store.set_preference(conn, "U1", "vegetarian, no mushroom, ~300")
    assert store.get_preference(conn, "U1") == "vegetarian, no mushroom, ~300"


def test_interaction_id_round_trips_and_clears(tmp_path):
    conn = fresh(tmp_path)
    assert store.get_interaction(conn, "D1") is None
    store.set_interaction(conn, "D1", "i_1", 1000.0)
    assert store.get_interaction(conn, "D1") == "i_1"
    store.clear_interaction(conn, "D1")
    assert store.get_interaction(conn, "D1") is None


def test_group_context_survives_as_a_dict(tmp_path):
    conn = fresh(tmp_path)
    store.save_group(conn, "C1", "food", "U1", "1.1",
                     {"restaurantId": "r1", "joined": ["U1"]}, 1000.0)
    got = store.get_group(conn, "C1")
    assert got["kind"] == "food"
    assert got["host_user_id"] == "U1"
    assert got["message_ts"] == "1.1"
    assert got["context"] == {"restaurantId": "r1", "joined": ["U1"]}


def test_group_context_can_be_updated_without_losing_the_row(tmp_path):
    conn = fresh(tmp_path)
    store.save_group(conn, "C1", "food", "U1", "1.1", {"joined": []}, 1000.0)
    store.set_group_context(conn, "C1", {"joined": ["U1", "U2"]})
    got = store.get_group(conn, "C1")
    assert got["context"]["joined"] == ["U1", "U2"]
    assert got["host_user_id"] == "U1"


def test_only_one_group_flow_per_channel(tmp_path):
    conn = fresh(tmp_path)
    store.save_group(conn, "C1", "food", "U1", "1.1", {}, 1000.0)
    store.save_group(conn, "C1", "table", "U2", "2.2", {}, 2000.0)
    assert store.get_group(conn, "C1")["kind"] == "table"
    assert conn.execute("select count(*) from group_order").fetchone()[0] == 1


def test_deleting_a_group_ends_the_flow(tmp_path):
    conn = fresh(tmp_path)
    store.save_group(conn, "C1", "food", "U1", "1.1", {}, 1000.0)
    store.delete_group(conn, "C1")
    assert store.get_group(conn, "C1") is None


def test_connect_hands_each_thread_its_own_connection(tmp_path):
    """Bolt runs every listener on a pool thread and sqlite3 objects cannot
    cross threads."""
    path = str(tmp_path / "t.db")
    main_conn = store.connect(path)
    store.init_schema(main_conn)
    seen = {}

    def worker():
        seen["conn"] = store.connect(path)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert seen["conn"] is not main_conn


def test_a_write_on_one_thread_is_visible_from_another(tmp_path):
    path = str(tmp_path / "t.db")
    store.init_schema(store.connect(path))
    store.save_token(store.connect(path), "U1", "acc", "ref", 1.0)
    seen = {}

    def worker():
        seen["token"] = store.get_token(store.connect(path), "U1")

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert seen["token"]["access_token"] == "acc"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_store.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'canteen.store'`

- [ ] **Step 4: Write the implementation**

Create `src/canteen/store.py`:

```python
"""SQLite persistence. Every read and write to disk goes through this module.

We deliberately hold almost nothing: Swiggy owns the cart and the orders,
Gemini owns the conversation transcript. What is left is a token per person, a
preference line, an interaction id, and any group flow currently running.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading

# sqlite3 connections cannot be used from a thread other than the one that
# created them, and Slack Bolt runs every listener on a pool thread.
_local = threading.local()

SCHEMA = """
create table if not exists swiggy_token (
    user_id text primary key,
    access_token text not null,
    refresh_token text,
    expires_at real not null
);
create table if not exists pending_auth (
    user_id text primary key,
    verifier text not null,
    state text not null,
    created_at real not null
);
create table if not exists preference (
    user_id text primary key,
    note text not null
);
create table if not exists conversation (
    key text primary key,
    interaction_id text not null,
    updated_at real not null
);
create table if not exists group_order (
    channel_id text primary key,
    kind text not null,
    host_user_id text not null,
    message_ts text not null,
    context text not null,
    created_at real not null
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    """The connection for the calling thread, opened on first use."""
    target = path or os.environ.get("CANTEEN_DB", "canteen.db")
    if getattr(_local, "path", None) == target:
        return _local.conn
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    _local.conn = conn
    _local.path = target
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# --- tokens, one per Slack user ---

def save_token(conn, user_id: str, access_token: str, refresh_token: str | None,
               expires_at: float) -> None:
    conn.execute(
        "insert into swiggy_token (user_id, access_token, refresh_token, expires_at) "
        "values (?, ?, ?, ?) on conflict(user_id) do update set "
        "access_token=excluded.access_token, refresh_token=excluded.refresh_token, "
        "expires_at=excluded.expires_at",
        (user_id, access_token, refresh_token, expires_at),
    )
    conn.commit()


def get_token(conn, user_id: str) -> dict | None:
    row = conn.execute("select * from swiggy_token where user_id = ?",
                       (user_id,)).fetchone()
    return dict(row) if row else None


def delete_token(conn, user_id: str) -> None:
    conn.execute("delete from swiggy_token where user_id = ?", (user_id,))
    conn.commit()


# --- in-flight authorisations ---

def save_pending(conn, user_id: str, verifier: str, state: str,
                 created_at: float) -> None:
    conn.execute(
        "insert into pending_auth (user_id, verifier, state, created_at) "
        "values (?, ?, ?, ?) on conflict(user_id) do update set "
        "verifier=excluded.verifier, state=excluded.state, "
        "created_at=excluded.created_at",
        (user_id, verifier, state, created_at),
    )
    conn.commit()


def take_pending(conn, user_id: str) -> dict | None:
    """Read and delete in one go — an auth code may only be redeemed once."""
    row = conn.execute("select * from pending_auth where user_id = ?",
                       (user_id,)).fetchone()
    if not row:
        return None
    conn.execute("delete from pending_auth where user_id = ?", (user_id,))
    conn.commit()
    return dict(row)


# --- preferences ---

def set_preference(conn, user_id: str, note: str) -> None:
    conn.execute(
        "insert into preference (user_id, note) values (?, ?) "
        "on conflict(user_id) do update set note=excluded.note",
        (user_id, note),
    )
    conn.commit()


def get_preference(conn, user_id: str) -> str | None:
    row = conn.execute("select note from preference where user_id = ?",
                       (user_id,)).fetchone()
    return row["note"] if row else None


# --- Gemini conversation continuity ---

def set_interaction(conn, key: str, interaction_id: str, updated_at: float) -> None:
    conn.execute(
        "insert into conversation (key, interaction_id, updated_at) values (?, ?, ?) "
        "on conflict(key) do update set interaction_id=excluded.interaction_id, "
        "updated_at=excluded.updated_at",
        (key, interaction_id, updated_at),
    )
    conn.commit()


def get_interaction(conn, key: str) -> str | None:
    row = conn.execute("select interaction_id from conversation where key = ?",
                       (key,)).fetchone()
    return row["interaction_id"] if row else None


def clear_interaction(conn, key: str) -> None:
    conn.execute("delete from conversation where key = ?", (key,))
    conn.commit()


# --- group flows ---

def save_group(conn, channel_id: str, kind: str, host_user_id: str,
               message_ts: str, context: dict, created_at: float) -> None:
    conn.execute(
        "insert into group_order (channel_id, kind, host_user_id, message_ts, "
        "context, created_at) values (?, ?, ?, ?, ?, ?) "
        "on conflict(channel_id) do update set kind=excluded.kind, "
        "host_user_id=excluded.host_user_id, message_ts=excluded.message_ts, "
        "context=excluded.context, created_at=excluded.created_at",
        (channel_id, kind, host_user_id, message_ts, json.dumps(context), created_at),
    )
    conn.commit()


def get_group(conn, channel_id: str) -> dict | None:
    row = conn.execute("select * from group_order where channel_id = ?",
                       (channel_id,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["context"] = json.loads(out["context"])
    return out


def set_group_context(conn, channel_id: str, context: dict) -> None:
    conn.execute("update group_order set context = ? where channel_id = ?",
                 (json.dumps(context), channel_id))
    conn.commit()


def delete_group(conn, channel_id: str) -> None:
    conn.execute("delete from group_order where channel_id = ?", (channel_id,))
    conn.commit()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_store.py -q`
Expected: PASS, 13 tests.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor: delete the canteen-brain modules, add per-user store"
```

---

### Task 2: Per-user OAuth with the paste flow

**Files:**
- Create: `src/canteen/auth.py`
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: `store.save_pending`, `store.take_pending`, `store.save_token`, `store.get_token`, `store.delete_token`
- Produces: `auth.NotConnected` (exception), `auth.CLIENT_ID: str`, `auth.REDIRECT_URI: str`, `auth.PENDING_TTL_SECONDS: int`, `auth.generate_pkce() -> tuple[str, str]`, `auth.authorize_url(challenge, state) -> str`, `auth.parse_callback(text) -> dict`, `auth.begin_link(conn, user_id, now) -> str`, `auth.complete_link(conn, http, user_id, pasted, now) -> None`, `auth.valid_token(conn, http, user_id, now) -> str`

- [ ] **Step 1: Write the failing test**

Create `tests/test_auth.py`:

```python
import time

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
    state = store.connect(str(tmp_path / "t.db"))  # same thread, same handle
    pending = state.execute("select state from pending_auth").fetchone()["state"]
    http = FakeHTTP({"access_token": "acc", "refresh_token": "ref",
                     "expires_in": 100})

    auth.complete_link(
        conn, http, "U1",
        f"http://localhost:8765/callback?code=xyz&state={pending}", now=1000.0)

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
    pending_state = conn.execute("select state from pending_auth").fetchone()["state"]
    later = 1000.0 + auth.PENDING_TTL_SECONDS + 1
    with pytest.raises(auth.LinkFailed, match="expired"):
        auth.complete_link(
            conn, FakeHTTP(), "U1",
            f"http://localhost:8765/callback?code=xyz&state={pending_state}",
            now=later)


def test_complete_link_without_a_pending_record_is_refused(tmp_path):
    conn = fresh(tmp_path)
    with pytest.raises(auth.LinkFailed, match="Start again"):
        auth.complete_link(
            conn, FakeHTTP(), "U1",
            "http://localhost:8765/callback?code=xyz&state=st", now=1000.0)


def test_complete_link_reports_a_denied_authorisation(tmp_path):
    conn = fresh(tmp_path)
    auth.begin_link(conn, "U1", now=1000.0)
    st = conn.execute("select state from pending_auth").fetchone()["state"]
    with pytest.raises(auth.LinkFailed, match="access_denied"):
        auth.complete_link(
            conn, FakeHTTP(), "U1",
            f"http://localhost:8765/callback?error=access_denied&state={st}",
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_auth.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'canteen.auth'`

- [ ] **Step 3: Write the implementation**

Create `src/canteen/auth.py`:

```python
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
import re
import secrets
import urllib.parse

from canteen import store

AUTH_BASE = "https://mcp.swiggy.com"
# Registration always returns this same id regardless of what we send, so there
# is nothing gained by calling POST /auth/register at runtime.
CLIENT_ID = "swiggy-mcp"
REDIRECT_URI = "http://localhost:8765/callback"

PENDING_TTL_SECONDS = 600      # how long a person has to finish signing in
REFRESH_MARGIN_SECONDS = 300   # refresh this long before actual expiry
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
    """
    match = _CALLBACK.search(text or "")
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

    response = http.post(
        AUTH_BASE + "/auth/token",
        data={
            "grant_type": "authorization_code",
            "code": got["code"],
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "code_verifier": pending["verifier"],
        },
    )
    response.raise_for_status()
    payload = response.json()
    store.save_token(
        conn, user_id,
        payload["access_token"],
        payload.get("refresh_token"),
        now + payload.get("expires_in", DEFAULT_TOKEN_LIFETIME),
    )


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_auth.py -q`
Expected: PASS, 16 tests.

- [ ] **Step 5: Commit**

```bash
git add src/canteen/auth.py tests/test_auth.py
git commit -m "feat: per-user Swiggy OAuth via the paste flow"
```

---

### Task 3: Rework the agent for per-user tokens, continuity and money guards

**Files:**
- Modify: `src/canteen/agent.py` (replace `LOCAL_TOOLS`, `SYSTEM`, `run`; add guards)
- Modify: `tests/test_agent.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `agent.run(client, prompt, token, servers, ctx, extra_system=None, previous_id=None) -> tuple[str, str]` returning `(reply_text, interaction_id)`; `agent.FOOD_CAP_RUPEES = 1000`; `agent.INSTAMART_MIN_RUPEES = 99`; `agent.blocked_reason(service, total) -> str | None`; `agent.system_for(preference) -> str`; `agent.LOCAL_TOOLS` containing exactly `propose_purchase`, `propose_booking`, `remember_preference`; `agent.SPEND_TOOLS`, `agent.mcp_tools(token, names, allow_spend=False)`, `agent.dispatch_local(name, args, ctx)` unchanged in shape.

- [ ] **Step 1: Write the failing test**

Replace the local-tool and run-loop tests in `tests/test_agent.py`. Keep every existing test for `mcp_tools`, `SERVER_TOOLS`, `SPEND_TOOLS`, snake_case names and `dispatch_local`. Add:

```python
def test_local_tools_are_exactly_the_three_the_bot_acts_on():
    assert {t["name"] for t in agent.LOCAL_TOOLS} == {
        "propose_purchase", "propose_booking", "remember_preference"}


def test_money_guard_blocks_a_food_cart_over_the_cap():
    assert agent.blocked_reason("food", 1001) is not None
    assert "1000" in agent.blocked_reason("food", 1001)
    assert agent.blocked_reason("food", 1000) is None


def test_money_guard_blocks_an_instamart_cart_under_the_minimum():
    assert "99" in agent.blocked_reason("instamart", 98)
    assert agent.blocked_reason("instamart", 99) is None


def test_money_guard_ignores_services_without_a_cart():
    assert agent.blocked_reason("dineout", 0) is None


def test_the_preference_line_reaches_the_system_instruction():
    assert "no mushroom" in agent.system_for("vegetarian, no mushroom")
    assert agent.system_for(None) == agent.SYSTEM


def test_run_returns_the_interaction_id_so_the_next_turn_can_continue():
    class FakeClient:
        class interactions:
            @staticmethod
            def create(**kw):
                return type("I", (), {"id": "i_7", "steps": [],
                                      "output_text": "done"})()

    text, interaction_id = agent.run(FakeClient(), prompt="hi", token="t",
                                     servers=["food"], ctx={})
    assert text == "done"
    assert interaction_id == "i_7"


def test_run_passes_the_previous_interaction_id_on_the_first_call():
    """Continuity is the whole multi-turn state model — Google holds the
    transcript and we hold one id."""
    seen = []

    class FakeClient:
        class interactions:
            @staticmethod
            def create(**kw):
                seen.append(kw)
                return type("I", (), {"id": "i_2", "steps": [],
                                      "output_text": "ok"})()

    agent.run(FakeClient(), prompt="and then?", token="t", servers=["food"],
              ctx={}, previous_id="i_1")
    assert seen[0]["previous_interaction_id"] == "i_1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent.py -q`
Expected: FAIL — `AttributeError: module 'canteen.agent' has no attribute 'blocked_reason'`, and the local-tool set assertion fails.

- [ ] **Step 3: Replace the tool definitions, system prompt and run loop**

In `src/canteen/agent.py`, keep `MODEL`, `MAX_TURNS`, `SERVERS`, `SERVER_TOOLS`, `SPEND_TOOLS`, `mcp_tools` and `dispatch_local` exactly as they are. Replace `SYSTEM`, `AUTHORISED`, `LOCAL_TOOLS` and `run` with:

```python
# Swiggy v1 limits, enforced here rather than left to the model.
FOOD_CAP_RUPEES = 1000
INSTAMART_MIN_RUPEES = 99


def blocked_reason(service: str, total: int) -> str | None:
    """Why this cart may not be ordered yet, or None if it may."""
    if service == "food" and total > FOOD_CAP_RUPEES:
        return (f"That cart is ₹{total}, over Swiggy's ₹{FOOD_CAP_RUPEES} limit "
                "for this kind of order. Drop an item or two.")
    if service == "instamart" and total < INSTAMART_MIN_RUPEES:
        return (f"Instamart needs at least ₹{INSTAMART_MIN_RUPEES} and this cart "
                f"is ₹{total}. Add something else.")
    return None


SYSTEM = """You are a Swiggy assistant living in Slack. You order food, order
groceries from Instamart, and book restaurant tables — by talking, not by
making people learn commands.

How to work:
- The cart lives on Swiggy's servers, not in this conversation. Before you talk
  about a cart or change it, call get_food_cart or get_cart and use what comes
  back. Never quote a total you did not just read from a tool.
- Only suggest restaurants whose availabilityStatus is OPEN, and dineout
  restaurants whose availability is AVAILABLE.
- The food cart holds one restaurant at a time. Changing restaurant empties it —
  say so and get a yes before you do it.
- Payment is cash on delivery only, so ignore coupons that need online payment.

How orders actually happen:
- You never place an order, check out, or book a table yourself. When everything
  is ready, call propose_purchase or propose_booking. A human then clicks a
  button, and only then are you asked to complete it.
- Confirm the date, party size and time out loud before proposing a booking.

Being honest:
- Swiggy's menu data has no allergen information. Never say a dish is safe for
  an allergy. Say what you filtered on and that you cannot verify ingredients.
- Money is in whole rupees.
- If someone tells you a lasting preference — diet, a dislike, a usual budget —
  call remember_preference so you do not ask again.

How to write:
- Slack, not email. Two or three sentences. No tables, no headings.
"""

AUTHORISED = (
    "The user has explicitly authorised this transaction by clicking a button. "
    "Re-read the cart first. If the total differs materially from what they "
    "approved, stop and say so instead of ordering. Otherwise complete it now "
    "and report the id."
)


def system_for(preference: str | None) -> str:
    """The system instruction, with this person's standing preferences."""
    if not preference:
        return SYSTEM
    return f"{SYSTEM}\nWhat this person has told you before: {preference}"


LOCAL_TOOLS = [
    {
        "type": "function",
        "name": "propose_purchase",
        "description": (
            "Call when a Swiggy cart is ready to be paid for. Shows the human a "
            "confirm button. This is the only way an order can happen — you "
            "cannot place it yourself. Read the total from get_food_cart or "
            "get_cart immediately before calling this."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "enum": ["food", "instamart"]},
                "total": {"type": "integer",
                          "description": "Cart total in whole rupees, from the cart tool."},
                "summary": {"type": "string",
                            "description": "One line: what is in the cart and from where."},
            },
            "required": ["service", "total", "summary"],
        },
    },
    {
        "type": "function",
        "name": "propose_booking",
        "description": (
            "Call when a specific dineout slot is ready to be booked. Shows the "
            "human a confirm button. You cannot book a table yourself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "restaurant_id": {"type": "string"},
                "restaurant_name": {"type": "string"},
                "slot_id": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "time": {"type": "string", "description": "As shown to the user, IST"},
                "guest_count": {"type": "integer"},
            },
            "required": ["restaurant_id", "restaurant_name", "slot_id", "date",
                         "time", "guest_count"],
        },
    },
    {
        "type": "function",
        "name": "remember_preference",
        "description": (
            "Store a lasting fact about this person — diet, dislikes, usual "
            "budget — so it is not asked again. Pass the whole preference line, "
            "not just the new part."
        ),
        "parameters": {
            "type": "object",
            "properties": {"note": {"type": "string"}},
            "required": ["note"],
        },
    },
]


def run(client, prompt: str, token: str, servers: list[str], ctx: dict,
        extra_system: str | None = None,
        previous_id: str | None = None) -> tuple[str, str]:
    """Drive the agent loop until the model stops calling our function tools.

    Returns (reply_text, interaction_id). The caller stores the id and passes it
    back as previous_id next turn — that is the entire multi-turn state model,
    because Google keeps the transcript and Swiggy keeps the cart.

    MCP tool calls execute inside the Gemini API, so the only calls reaching
    this loop are local ones. Spending tools are visible to the model only when
    the caller supplied the AUTHORISED preamble.
    """
    settings = {
        "model": MODEL,
        "system_instruction": extra_system or SYSTEM,
        "tools": [
            *mcp_tools(token, servers, allow_spend=AUTHORISED in (extra_system or "")),
            *LOCAL_TOOLS,
        ],
    }
    payload: str | list = prompt
    interaction_id = previous_id

    for _ in range(MAX_TURNS):
        interaction = client.interactions.create(
            input=payload, previous_interaction_id=interaction_id, **settings
        )
        interaction_id = interaction.id

        calls = [s for s in (interaction.steps or []) if s.type == "function_call"]
        if not calls:
            return (interaction.output_text or "").strip(), interaction_id

        payload = [
            {
                "type": "function_result",
                "call_id": call.id,
                "name": call.name,
                "result": dispatch_local(call.name, call.arguments or {}, ctx),
            }
            for call in calls
        ]

    return ("I got stuck working on that — try again or narrow the request.",
            interaction_id)
```

Note the changed gate condition: `extra_system` now carries the full system
instruction including preferences, so the check is `AUTHORISED in extra_system`
rather than equality.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_agent.py -q`
Expected: PASS. Fix any existing test that assumed `run` returned a bare string
or that `extra_system` was appended to `SYSTEM` — callers now pass the complete
instruction.

- [ ] **Step 5: Add a guard test for the spend gate under the new condition**

Append to `tests/test_agent.py`:

```python
def test_spend_tools_appear_only_when_the_instruction_carries_the_authorisation():
    seen = []

    class FakeClient:
        class interactions:
            @staticmethod
            def create(**kw):
                seen.append(kw)
                return type("I", (), {"id": "i", "steps": [],
                                      "output_text": "x"})()

    agent.run(FakeClient(), prompt="p", token="t", servers=["food"], ctx={},
              extra_system=agent.system_for("vegetarian"))
    agent.run(FakeClient(), prompt="p", token="t", servers=["food"], ctx={},
              extra_system=agent.system_for("vegetarian") + "\n" + agent.AUTHORISED)

    plain, authorised = (kw["tools"][0]["allowed_tools"][0]["tools"] for kw in seen)
    assert "place_food_order" not in plain
    assert "place_food_order" in authorised
```

Run: `uv run pytest tests/test_agent.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/canteen/agent.py tests/test_agent.py
git commit -m "feat: agent proposes purchases, carries continuity, enforces caps"
```

---

### Task 4: Block Kit surfaces

**Files:**
- Replace: `src/canteen/blocks.py`
- Create: `tests/test_blocks.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `blocks.connect_prompt(url) -> list`, `blocks.confirm_purchase(service, total, summary) -> list`, `blocks.confirm_booking(proposal) -> list`, `blocks.group_food(host_user_id, restaurant_name, lines, total, joined) -> list`, `blocks.pantry_list(items, total) -> list`, `blocks.table_options(options) -> list`

- [ ] **Step 1: Write the failing test**

Create `tests/test_blocks.py`:

```python
import json

from canteen import blocks


def action_ids(payload):
    return [e["action_id"]
            for b in payload for e in b.get("elements", [])
            if "action_id" in e]


def rendered(payload):
    return json.dumps(payload)


def test_connect_prompt_links_out_and_explains_the_broken_page():
    """The redirect deliberately fails to load. If we don't warn, people think
    the bot is broken and stop."""
    payload = blocks.connect_prompt("https://mcp.swiggy.com/auth/authorize?x=1")
    text = rendered(payload)
    assert "https://mcp.swiggy.com/auth/authorize?x=1" in text
    assert "won't load" in text or "will not load" in text
    assert "paste" in text.lower()


def test_confirm_purchase_puts_the_real_total_on_the_button():
    payload = blocks.confirm_purchase("food", 480, "Dosa and filter coffee")
    assert "confirm_purchase" in action_ids(payload)
    assert "cancel_purchase" in action_ids(payload)
    assert "₹480" in rendered(payload)
    assert "COD" in rendered(payload)


def test_confirm_purchase_carries_the_service_in_the_button_value():
    payload = blocks.confirm_purchase("instamart", 250, "Milk, bread")
    values = [e["value"] for b in payload for e in b.get("elements", [])
              if e.get("action_id") == "confirm_purchase"]
    assert values == ["instamart"]


def test_confirm_booking_states_date_time_and_party_size():
    payload = blocks.confirm_booking({
        "restaurant_id": "r1", "restaurant_name": "Toit", "slot_id": "s1",
        "date": "2026-08-01", "time": "8:00 PM", "guest_count": 6})
    text = rendered(payload)
    assert "Toit" in text and "2026-08-01" in text and "8:00 PM" in text
    assert "6" in text
    assert "confirm_booking" in action_ids(payload)


def test_group_food_names_the_host_and_who_has_joined():
    payload = blocks.group_food("U1", "Sattvik", ["Dosa ₹120"], 120, ["U1", "U2"])
    text = rendered(payload)
    assert "<@U1>" in text          # host is credited, and pays
    assert "<@U2>" in text
    assert "Sattvik" in text
    assert "₹120" in text
    assert "add_my_dish" in action_ids(payload)
    assert "place_group_order" in action_ids(payload)


def test_group_food_before_a_restaurant_is_chosen_offers_no_order_button():
    """Nothing may be ordered until there is a restaurant and a cart."""
    payload = blocks.group_food("U1", None, [], 0, ["U1"])
    assert "place_group_order" not in action_ids(payload)


def test_pantry_list_shows_every_item_and_the_total():
    payload = blocks.pantry_list(
        [{"spinId": "p1", "name": "Milk 1L", "quantity": 2, "price": 60},
         {"spinId": "p2", "name": "Coffee", "quantity": 1, "price": 240}], 360)
    text = rendered(payload)
    assert "Milk 1L" in text and "Coffee" in text
    assert "₹360" in text
    assert "confirm_purchase" in action_ids(payload)


def test_table_options_render_one_button_per_slot():
    payload = blocks.table_options([
        {"restaurant_id": "r1", "restaurant_name": "Toit", "slot_id": "s1",
         "date": "2026-08-01", "time": "7:00 PM", "guest_count": 6},
        {"restaurant_id": "r1", "restaurant_name": "Toit", "slot_id": "s2",
         "date": "2026-08-01", "time": "8:00 PM", "guest_count": 6},
    ])
    values = [e["value"] for b in payload for e in b.get("elements", [])
              if e.get("action_id") == "pick_slot"]
    assert len(values) == 2
    assert json.loads(values[0])["slot_id"] == "s1"


def test_every_button_value_stays_within_slack_limits():
    """Slack rejects an action value over 2000 characters, and the failure is a
    silent 400 at post time."""
    payload = blocks.table_options([
        {"restaurant_id": "r" * 50, "restaurant_name": "n" * 200,
         "slot_id": "s" * 50, "date": "2026-08-01", "time": "8:00 PM",
         "guest_count": 6}])
    for b in payload:
        for e in b.get("elements", []):
            assert len(e.get("value", "")) <= 2000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_blocks.py -q`
Expected: FAIL — `AttributeError: module 'canteen.blocks' has no attribute 'connect_prompt'`

- [ ] **Step 3: Write the implementation**

Replace `src/canteen/blocks.py`:

```python
"""Block Kit builders. Pure — no Slack client, no database, no model."""

from __future__ import annotations

import json


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _button(text: str, action_id: str, value: str = "1",
            style: str | None = None) -> dict:
    element = {
        "type": "button",
        "text": {"type": "plain_text", "text": text},
        "action_id": action_id,
        "value": value,
    }
    if style:
        element["style"] = style
    return element


def _actions(*elements: dict) -> dict:
    return {"type": "actions", "elements": list(elements)}


def connect_prompt(url: str) -> list:
    return [
        _section(
            "*Connect your Swiggy account*\nOrders, carts and addresses stay "
            "yours — I never see anyone else's."
        ),
        _section(
            f"1. <{url}|Sign in to Swiggy>\n"
            "2. Afterwards your browser lands on a page that *won't load* — "
            "that is expected.\n"
            "3. Copy that page's address from the URL bar and paste it here."
        ),
    ]


def confirm_purchase(service: str, total: int, summary: str) -> list:
    label = "Place order" if service == "food" else "Check out"
    return [
        _section(f"{summary}\n*Total ₹{total}* · cash on delivery"),
        _actions(
            _button(f"{label} · ₹{total}", "confirm_purchase", service, "primary"),
            _button("Cancel", "cancel_purchase", service),
        ),
    ]


def confirm_booking(proposal: dict) -> list:
    return [
        _section(
            f"*{proposal['restaurant_name']}*\n"
            f"{proposal['date']} at {proposal['time']} · "
            f"{proposal['guest_count']} people"
        ),
        _actions(
            _button("Book it", "confirm_booking", "1", "primary"),
            _button("Cancel", "cancel_purchase", "dineout"),
        ),
    ]


def group_food(host_user_id: str, restaurant_name: str | None, lines: list[str],
               total: int, joined: list[str]) -> list:
    who = ", ".join(f"<@{u}>" for u in joined) or "nobody yet"
    header = (f"*Group lunch* — on <@{host_user_id}>'s Swiggy account\n"
              f"In: {who}")
    payload = [_section(header)]

    if not restaurant_name:
        payload.append(_section(
            f"<@{host_user_id}>, tell me in this thread where we're ordering from."))
        payload.append(_actions(
            _button("Join", "join_group", "1"),
            _button("Cancel", "cancel_group", "1"),
        ))
        return payload

    body = "\n".join(lines) if lines else "_Cart is empty._"
    payload.append(_section(f"*{restaurant_name}*\n{body}\n\n*Total ₹{total}*"))
    payload.append(_actions(
        _button("Add my dish", "add_my_dish", "1"),
        _button(f"Place order · ₹{total}", "place_group_order", "1", "primary"),
        _button("Cancel", "cancel_group", "1"),
    ))
    return payload


def pantry_list(items: list[dict], total: int) -> list:
    lines = "\n".join(
        f"• {i['name']} ×{i['quantity']} — ₹{i['price']}" for i in items
    ) or "_Nothing suggested._"
    return [
        _section(f"*Pantry restock*\n{lines}\n\n*Total ₹{total}* · cash on delivery"),
        _actions(
            _button(f"Order · ₹{total}", "confirm_purchase", "instamart", "primary"),
            _button("Cancel", "cancel_purchase", "instamart"),
        ),
    ]


def table_options(options: list[dict]) -> list:
    """One button per slot. The whole proposal rides in the button value so the
    click handler needs no lookup — Slack caps that value at 2000 characters."""
    payload = [_section("*Tables I can book*")]
    for option in options:
        value = json.dumps(option, separators=(",", ":"))[:2000]
        payload.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": (f"*{option['restaurant_name']}* · "
                              f"{option['date']} at {option['time']} · "
                              f"{option['guest_count']} people")},
        })
        payload.append(_actions(_button("Pick this", "pick_slot", value)))
    return payload
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_blocks.py -q`
Expected: PASS, 9 tests.

- [ ] **Step 5: Commit**

```bash
git add src/canteen/blocks.py tests/test_blocks.py
git commit -m "feat: Block Kit surfaces for connect, confirm, group and tables"
```

---

### Task 5: The DM personal assistant

**Files:**
- Replace: `src/canteen/app.py`
- Create: `tests/test_app.py`

**Interfaces:**
- Consumes: `store.*`, `auth.*`, `agent.run`, `agent.system_for`, `agent.blocked_reason`, `agent.AUTHORISED`, `blocks.connect_prompt`, `blocks.confirm_purchase`, `blocks.confirm_booking`, `slackfmt.to_mrkdwn`
- Produces: `app.app` (the Bolt App), `app.db() -> sqlite3.Connection`, `app.token_for(user_id) -> str`, `app.local_ctx(user_id, channel_id) -> dict`, `app.converse(channel_id, user_id, prompt, servers, extra_system=None) -> str`, `app.progress(channel_id, thread_ts=None) -> callable`, `app.PROPOSALS: dict[str, dict]`, `app.SERVERS_ALL: list[str]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_app.py`:

```python
"""app.py builds a Bolt App at import, so the environment is set up first."""

import os
import tempfile

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test")
os.environ.setdefault("GEMINI_API_KEY", "gemini-test")
os.environ.setdefault("CANTEEN_VERIFY_SLACK", "0")
os.environ.setdefault("CANTEEN_DB", os.path.join(tempfile.mkdtemp(), "app.db"))

from canteen import agent, app, auth, store  # noqa: E402

CHANNEL = "D-TEST"
USER = "U1"


def setup_function():
    app.PROPOSALS.clear()
    for table in ("swiggy_token", "pending_auth", "preference",
                  "conversation", "group_order"):
        app.db().execute(f"delete from {table}")
    app.db().commit()


def test_local_tools_declared_to_the_model_are_all_dispatchable():
    """A tool the model can call but we cannot run is a dead end mid-order."""
    ctx = app.local_ctx(USER, CHANNEL)
    assert {t["name"] for t in agent.LOCAL_TOOLS} == set(ctx)


def test_proposing_a_purchase_stores_it_for_the_button_to_find():
    ctx = app.local_ctx(USER, CHANNEL)
    result = ctx["propose_purchase"](service="food", total=480, summary="Dosa")
    assert app.PROPOSALS[CHANNEL]["service"] == "food"
    assert app.PROPOSALS[CHANNEL]["total"] == 480
    assert "shown" in result.lower() or "button" in result.lower()


def test_a_cart_over_the_food_cap_is_refused_before_any_button_appears():
    ctx = app.local_ctx(USER, CHANNEL)
    result = ctx["propose_purchase"](service="food", total=1400, summary="Feast")
    assert "1000" in result
    assert CHANNEL not in app.PROPOSALS


def test_a_cart_under_the_instamart_minimum_is_refused():
    ctx = app.local_ctx(USER, CHANNEL)
    result = ctx["propose_purchase"](service="instamart", total=50, summary="Milk")
    assert "99" in result
    assert CHANNEL not in app.PROPOSALS


def test_remembering_a_preference_writes_it_where_the_prompt_reads_it():
    ctx = app.local_ctx(USER, CHANNEL)
    ctx["remember_preference"](note="vegetarian, no mushroom")
    assert store.get_preference(app.db(), USER) == "vegetarian, no mushroom"
    assert "no mushroom" in agent.system_for(store.get_preference(app.db(), USER))


def test_proposing_a_booking_stores_the_whole_proposal():
    ctx = app.local_ctx(USER, CHANNEL)
    ctx["propose_booking"](restaurant_id="r1", restaurant_name="Toit",
                           slot_id="s1", date="2026-08-01", time="8:00 PM",
                           guest_count=6)
    assert app.PROPOSALS[CHANNEL]["slot_id"] == "s1"
    assert app.PROPOSALS[CHANNEL]["service"] == "dineout"


def test_converse_stores_the_interaction_id_for_the_next_turn(monkeypatch):
    monkeypatch.setattr(agent, "run",
                        lambda *a, **k: ("**hi**", "i_42"))
    store.save_token(app.db(), USER, "acc", "ref", 9e9)

    reply = app.converse(CHANNEL, USER, "hello", servers=["food"])

    assert reply == "*hi*"  # mrkdwn conversion happened on the way out
    assert store.get_interaction(app.db(), CHANNEL) == "i_42"


def test_converse_passes_the_stored_interaction_id_back(monkeypatch):
    seen = {}

    def fake_run(client, **kw):
        seen.update(kw)
        return "ok", "i_2"

    monkeypatch.setattr(agent, "run", fake_run)
    store.save_token(app.db(), USER, "acc", "ref", 9e9)
    store.set_interaction(app.db(), CHANNEL, "i_1", 0.0)

    app.converse(CHANNEL, USER, "and then?", servers=["food"])

    assert seen["previous_id"] == "i_1"


def test_converse_injects_the_persons_preference(monkeypatch):
    seen = {}

    def fake_run(client, **kw):
        seen.update(kw)
        return "ok", "i_1"

    monkeypatch.setattr(agent, "run", fake_run)
    store.save_token(app.db(), USER, "acc", "ref", 9e9)
    store.set_preference(app.db(), USER, "jain")

    app.converse(CHANNEL, USER, "lunch?", servers=["food"])

    assert "jain" in seen["extra_system"]


def test_an_unconnected_person_gets_NotConnected_rather_than_a_crash():
    import pytest
    with pytest.raises(auth.NotConnected):
        app.token_for("U-NOBODY")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_app.py -q`
Expected: FAIL — `ImportError` / `AttributeError` for the missing `app` members.

- [ ] **Step 3: Write the implementation**

Replace `src/canteen/app.py`:

```python
"""Slack Bolt app in Socket Mode.

Routing is the whole job: a DM is a personal Swiggy assistant on that person's
own account, a mention starts a group flow. Socket Mode means no public URL.

Money moves only from a button handler. Everything else assembles and stops.
"""

from __future__ import annotations

import logging
import os
import time

import httpx
from dotenv import load_dotenv
from google import genai
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from canteen import agent, auth, blocks, store
from canteen.slackfmt import to_mrkdwn

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("canteen")

SERVERS_ALL = ["food", "im", "dineout"]
THINKING = ":hourglass_flowing_sand: _Working on it…_"

# Bolt calls auth.test at construction, which needs the network and a real
# token. CANTEEN_VERIFY_SLACK=0 skips it so the module can be imported in CI.
app = App(
    token=os.environ["SLACK_BOT_TOKEN"],
    token_verification_enabled=os.environ.get("CANTEEN_VERIFY_SLACK", "1") == "1",
)
gemini = genai.Client()
http = httpx.Client(timeout=30)  # httpx.Client is thread-safe; sqlite3 is not
store.init_schema(store.connect())

# A purchase awaiting its button click, per channel. Deliberately in memory: a
# proposal that does not survive a restart is a proposal nobody can accidentally
# confirm an hour later against a cart that has changed underneath it.
PROPOSALS: dict[str, dict] = {}


def db():
    """The DB handle for whichever thread is asking.

    Bolt dispatches listeners on a pool, so this must never be hoisted into a
    module-level variable.
    """
    return store.connect()


def token_for(user_id: str) -> str:
    return auth.valid_token(db(), http, user_id, time.time())


# ------------------------------------------------------------- local tools

def _propose_purchase(channel_id: str, service: str, total: int,
                      summary: str) -> str:
    blocked = agent.blocked_reason(service, total)
    if blocked:
        return f"Not offering that yet: {blocked}"
    PROPOSALS[channel_id] = {"service": service, "total": total,
                             "summary": summary}
    return "A confirm button has been shown to the user. Stop and wait."


def _propose_booking(channel_id: str, **proposal) -> str:
    PROPOSALS[channel_id] = {"service": "dineout", **proposal}
    return "A confirm button has been shown to the user. Stop and wait."


def _remember_preference(user_id: str, note: str) -> str:
    store.set_preference(db(), user_id, note)
    return "Saved."


def local_ctx(user_id: str, channel_id: str) -> dict:
    """The local tools the model may call, bound to this person and channel."""
    return {
        "propose_purchase": lambda service, total, summary: _propose_purchase(
            channel_id, service, total, summary),
        "propose_booking": lambda **kw: _propose_booking(channel_id, **kw),
        "remember_preference": lambda note: _remember_preference(user_id, note),
    }


# ------------------------------------------------------------ model access

def converse(channel_id: str, user_id: str, prompt: str, servers: list[str],
             extra_system: str | None = None) -> str:
    """One turn of conversation, continuing whatever came before in this channel."""
    instruction = extra_system or agent.system_for(
        store.get_preference(db(), user_id))
    reply, interaction_id = agent.run(
        gemini,
        prompt=prompt,
        token=token_for(user_id),
        servers=servers,
        ctx=local_ctx(user_id, channel_id),
        extra_system=instruction,
        previous_id=store.get_interaction(db(), channel_id),
    )
    if interaction_id:
        store.set_interaction(db(), channel_id, interaction_id, time.time())
    return to_mrkdwn(reply)


def progress(channel_id: str, thread_ts: str | None = None):
    """Post a placeholder now; return a function that turns it into the answer.

    A Swiggy round trip takes ten to thirty seconds and Slack shows nothing at
    all meanwhile — the channel just looks broken.
    """
    posted = app.client.chat_postMessage(channel=channel_id, text=THINKING,
                                         thread_ts=thread_ts)

    def finish(text: str, block_kit: list | None = None) -> None:
        app.client.chat_update(channel=channel_id, ts=posted["ts"], text=text,
                               blocks=block_kit or [])

    return finish


def respond(channel_id: str, user_id: str, prompt: str,
            servers: list[str] | None = None,
            thread_ts: str | None = None) -> None:
    """Converse, showing progress, and render any proposal the model produced."""
    finish = progress(channel_id, thread_ts)
    try:
        reply = converse(channel_id, user_id, prompt, servers or SERVERS_ALL)
    except auth.NotConnected:
        finish("Connect your Swiggy account first.",
               blocks.connect_prompt(auth.begin_link(db(), user_id, time.time())))
        return
    except Exception as exc:
        log.exception("conversation failed")
        finish(f":warning: That didn't work: `{exc}`. Nothing was ordered.")
        return

    proposal = PROPOSALS.get(channel_id)
    if not proposal:
        finish(reply)
        return
    if proposal["service"] == "dineout":
        finish(reply, blocks.confirm_booking(proposal))
    else:
        finish(reply, blocks.confirm_purchase(
            proposal["service"], proposal["total"], proposal["summary"]))


# ------------------------------------------------------------- DM handlers

@app.event("message")
def handle_dm(body, client):
    """A DM is a personal Swiggy assistant. No commands to learn."""
    event = body.get("event", {})
    if (event.get("channel_type") != "im" or event.get("bot_id")
            or event.get("subtype")):
        return

    user_id = event["user"]
    channel_id = event["channel"]
    text = (event.get("text") or "").strip()
    if not text:
        return

    # A pasted redirect URL finishes the link flow. Only ever accepted in a DM.
    if auth.parse_callback(text):
        try:
            auth.complete_link(db(), http, user_id, text, time.time())
        except auth.LinkFailed as exc:
            client.chat_postMessage(channel=channel_id, text=f":warning: {exc}")
            return
        client.chat_postMessage(
            channel=channel_id,
            text="Swiggy connected :white_check_mark: — what do you feel like?")
        return

    if store.get_token(db(), user_id) is None:
        client.chat_postMessage(
            channel=channel_id, text="Connect your Swiggy account first.",
            blocks=blocks.connect_prompt(
                auth.begin_link(db(), user_id, time.time())))
        return

    if text.lower().strip("!.?") in ("reset", "start over", "forget it"):
        store.clear_interaction(db(), channel_id)
        PROPOSALS.pop(channel_id, None)
        client.chat_postMessage(channel=channel_id, text="Fresh start. Go ahead.")
        return

    respond(channel_id, user_id, text)


# ----------------------------------------------------------- spend handlers

@app.action("cancel_purchase")
def handle_cancel_purchase(ack, body, client):
    ack()
    channel_id = body["channel"]["id"]
    PROPOSALS.pop(channel_id, None)
    client.chat_update(channel=channel_id, ts=body["message"]["ts"],
                       text="Cancelled. Nothing was ordered.", blocks=[])


@app.action("confirm_purchase")
def handle_confirm_purchase(ack, body, client):
    """The only path to place_food_order and checkout. A human clicked this."""
    ack()
    channel_id = body["channel"]["id"]
    user_id = body["user"]["id"]
    proposal = PROPOSALS.pop(channel_id, None)
    if not proposal:
        client.chat_update(channel=channel_id, ts=body["message"]["ts"], blocks=[],
                           text="That order expired — ask me again.")
        return

    service = proposal["service"]
    servers = ["food"] if service == "food" else ["im"]
    verb = "place the food order" if service == "food" else "check out the cart"
    recent = "get_food_orders" if service == "food" else "get_orders"

    client.chat_update(channel=channel_id, ts=body["message"]["ts"], blocks=[],
                       text=":hourglass_flowing_sand: _Ordering…_")
    try:
        reply = converse(
            channel_id, user_id,
            f"Re-read the cart, then {verb} with paymentMethod COD. "
            f"The user approved ₹{proposal['total']}. Report the order id.",
            servers=servers,
            extra_system=agent.system_for(store.get_preference(db(), user_id))
            + "\n" + agent.AUTHORISED,
        )
    except Exception as exc:
        log.exception("order failed")
        # Not idempotent: the order may have landed before the failure, so
        # never retry — look first.
        status = converse(
            channel_id, user_id,
            f"Call {recent} and report my most recent order and its status. "
            "Do not order anything.",
            servers=servers)
        client.chat_postMessage(
            channel=channel_id,
            text=(f"The order call failed (`{exc}`). I did *not* retry — that "
                  f"risks ordering twice. Latest on your account:\n{status}"))
        return

    client.chat_postMessage(channel=channel_id, text=reply)


@app.action("confirm_booking")
def handle_confirm_booking(ack, body, client):
    """The only path to book_table. A human clicked this."""
    ack()
    channel_id = body["channel"]["id"]
    user_id = body["user"]["id"]
    proposal = PROPOSALS.pop(channel_id, None)
    if not proposal:
        client.chat_update(channel=channel_id, ts=body["message"]["ts"], blocks=[],
                           text="That booking expired — ask me again.")
        return

    client.chat_update(channel=channel_id, ts=body["message"]["ts"], blocks=[],
                       text=":hourglass_flowing_sand: _Booking…_")
    try:
        reply = converse(
            channel_id, user_id,
            f"Book restaurant {proposal['restaurant_id']} slot "
            f"{proposal['slot_id']} for {proposal['guest_count']} people on "
            f"{proposal['date']}. Report the booking id and confirmation.",
            servers=["dineout"],
            extra_system=agent.system_for(store.get_preference(db(), user_id))
            + "\n" + agent.AUTHORISED,
        )
    except Exception as exc:
        log.exception("booking failed")
        status = converse(
            channel_id, user_id,
            f"Check get_booking_status for restaurant "
            f"{proposal['restaurant_id']} slot {proposal['slot_id']} and report "
            "what you find. Do not book anything.",
            servers=["dineout"])
        client.chat_postMessage(
            channel=channel_id,
            text=(f"The booking call failed (`{exc}`). I did *not* retry. "
                  f"What Swiggy shows:\n{status}"))
        return

    client.chat_postMessage(channel=channel_id, text=reply)


# ------------------------------------------------------------------ errors

@app.error
def handle_uncaught(error, body, logger):
    """Without this a failed listener is silent in Slack and visible only in the
    server log — the user stares at a message that never updates."""
    logger.exception("listener failed: %s", error)
    channel_id = (body or {}).get("channel_id") or (
        (body or {}).get("channel") or {}).get("id")
    if not channel_id:
        return
    try:
        app.client.chat_postMessage(
            channel=channel_id,
            text=f"That didn't work: `{error}`. Nothing was ordered.")
    except Exception:
        logger.exception("could not report the error back to Slack")


def main() -> None:
    from canteen import group
    group.register(app, converse, progress, db, token_for)
    log.info("Swiggy assistant up. Connecting to Slack…")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_app.py -q`
Expected: PASS, 10 tests. `main()` is not exercised — `group` does not exist yet.

- [ ] **Step 5: Commit**

```bash
git add src/canteen/app.py tests/test_app.py
git commit -m "feat: DM personal assistant with per-user tokens and confirm gates"
```

---

### Task 6: The three group flows

**Files:**
- Create: `src/canteen/group.py`
- Create: `tests/test_group.py`

**Interfaces:**
- Consumes: `store.save_group`, `store.get_group`, `store.set_group_context`, `store.delete_group`, `blocks.group_food`, `blocks.table_options`, `blocks.pantry_list`, `agent.blocked_reason`, `app.converse`, `app.progress`, `app.db`, `app.token_for`
- Produces: `group.FOOD`, `group.TABLE`, `group.PANTRY`, `group.classify(text) -> str | None`, `group.join(context, user_id) -> dict`, `group.cart_lock(channel_id) -> threading.Lock`, `group.register(app, converse, progress, db, token_for) -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_group.py`:

```python
import threading

from canteen import group


def test_classify_recognises_the_three_group_intents():
    assert group.classify("lunch") == group.FOOD
    assert group.classify("order lunch for the team") == group.FOOD
    assert group.classify("book a table for 8 at 8pm") == group.TABLE
    assert group.classify("dinner reservation tonight") == group.TABLE
    assert group.classify("restock the pantry") == group.PANTRY
    assert group.classify("we're out of coffee, groceries please") == group.PANTRY


def test_classify_leaves_plain_questions_to_the_model():
    assert group.classify("what's good around here?") is None
    assert group.classify("track my order") is None


def test_table_beats_food_when_both_words_appear():
    """'book a table for lunch' is a booking, not a group food order."""
    assert group.classify("book a table for lunch tomorrow") == group.TABLE


def test_joining_is_idempotent():
    context = {"joined": ["U1"]}
    once = group.join(context, "U2")
    twice = group.join(once, "U2")
    assert twice["joined"] == ["U1", "U2"]


def test_joining_preserves_the_rest_of_the_context():
    context = {"joined": ["U1"], "restaurantId": "r1"}
    assert group.join(context, "U2")["restaurantId"] == "r1"


def test_join_handles_a_context_that_has_no_joined_list_yet():
    assert group.join({}, "U1")["joined"] == ["U1"]


def test_the_same_channel_gets_the_same_cart_lock():
    """Joiners mutate one server-side cart, so their writes must serialise."""
    assert group.cart_lock("C1") is group.cart_lock("C1")
    assert group.cart_lock("C1") is not group.cart_lock("C2")
    assert isinstance(group.cart_lock("C1"), type(threading.Lock()))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_group.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'canteen.group'`

- [ ] **Step 3: Write the implementation**

Create `src/canteen/group.py`:

```python
"""The three things a channel does: group food order, table booking, pantry.

A DM spends your money. A channel spends the money of whoever started the flow,
and only after they click a button. Joiners add dishes to the starter's real
Swiggy cart — there is no local cart, because Swiggy owns it.
"""

from __future__ import annotations

import threading
import time

from canteen import agent, blocks, store

FOOD = "food"
TABLE = "table"
PANTRY = "pantry"

_TABLE_WORDS = ("table", "reservation", "reserve", "book a", "dineout")
_PANTRY_WORDS = ("pantry", "restock", "grocer", "instamart", "supplies")
_FOOD_WORDS = ("lunch", "order food", "team food", "food order", "dinner order")

# One lock per channel. Joiners mutate a single server-side cart, so their
# writes must not interleave. This is the only lock in the system.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def cart_lock(channel_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(channel_id, threading.Lock())


def classify(text: str) -> str | None:
    """Which group flow this mention starts, or None to just answer it.

    Table wins over food: "book a table for lunch" is a booking.
    """
    low = (text or "").lower()
    if any(w in low for w in _TABLE_WORDS):
        return TABLE
    if any(w in low for w in _PANTRY_WORDS):
        return PANTRY
    if any(w in low for w in _FOOD_WORDS):
        return FOOD
    return None


def join(context: dict, user_id: str) -> dict:
    joined = list(context.get("joined") or [])
    if user_id not in joined:
        joined.append(user_id)
    return {**context, "joined": joined}


def register(app, converse, progress, db, token_for) -> None:
    """Attach the group handlers to the Bolt app.

    The four callables come from app.py rather than being imported, so this
    module never imports app.py back and the cycle stays broken.
    """

    def _refresh(channel_id: str, host_user_id: str, context: dict,
                 message_ts: str) -> None:
        """Re-read the host's cart from Swiggy and rewrite the live message."""
        summary = converse(
            channel_id, host_user_id,
            "Call get_food_cart and report exactly what is in it: one line per "
            "item as 'name xN — ₹price', then a final line 'TOTAL ₹n'. "
            "No commentary.",
            ["food"])
        lines = [ln.strip() for ln in summary.splitlines() if ln.strip()]
        total = 0
        for line in lines:
            if line.upper().startswith("TOTAL"):
                digits = "".join(c for c in line if c.isdigit())
                total = int(digits) if digits else 0
        app.client.chat_update(
            channel=channel_id, ts=message_ts, text="Group lunch",
            blocks=blocks.group_food(
                host_user_id, context.get("restaurantName"),
                [ln for ln in lines if not ln.upper().startswith("TOTAL")],
                total, context.get("joined") or []),
        )

    def start(channel_id: str, user_id: str, text: str, kind: str) -> None:
        if kind == FOOD:
            posted = app.client.chat_postMessage(
                channel=channel_id, text="Group lunch",
                blocks=blocks.group_food(user_id, None, [], 0, [user_id]))
            store.save_group(db(), channel_id, FOOD, user_id, posted["ts"],
                             {"joined": [user_id]}, time.time())
            return

        if kind == TABLE:
            finish = progress(channel_id)
            reply = converse(
                channel_id, user_id,
                f"A group wants a table. Request: {text!r}. Use "
                "get_saved_locations, then search_restaurants_dineout keeping "
                "only availability AVAILABLE, then get_available_slots for the "
                "best two or three. Then call propose_booking for the single "
                "best slot. Confirm the date, time and party size in your reply.",
                ["dineout"])
            finish(reply)
            return

        finish = progress(channel_id)
        reply = converse(
            channel_id, user_id,
            "Restock the office pantry. Call get_addresses, then "
            "your_go_to_items for that address, then update_cart with sensible "
            "quantities for an office. Then call get_cart and propose_purchase "
            "with the real total. List what you added.",
            ["im"])
        finish(reply)

    @app.event("app_mention")
    def handle_mention(body, client):
        event = body["event"]
        channel_id = event["channel"]
        user_id = event["user"]
        text = " ".join(w for w in (event.get("text") or "").split()
                        if not w.startswith("<@")).strip()

        kind = classify(text)
        if kind is None:
            from canteen.app import respond
            respond(channel_id, user_id, text or "What can you do?",
                    thread_ts=event.get("thread_ts"))
            return

        existing = store.get_group(db(), channel_id)
        if existing:
            client.chat_postMessage(
                channel=channel_id,
                text=(f"There's already a group {existing['kind']} running here, "
                      f"started by <@{existing['host_user_id']}>. Cancel it first."))
            return
        start(channel_id, user_id, text, kind)

    @app.action("join_group")
    def handle_join(ack, body, client):
        ack()
        channel_id = body["channel"]["id"]
        row = store.get_group(db(), channel_id)
        if not row:
            return
        context = join(row["context"], body["user"]["id"])
        store.set_group_context(db(), channel_id, context)
        client.chat_update(
            channel=channel_id, ts=row["message_ts"], text="Group lunch",
            blocks=blocks.group_food(row["host_user_id"],
                                     context.get("restaurantName"), [], 0,
                                     context["joined"]))

    @app.action("cancel_group")
    def handle_cancel_group(ack, body, client):
        ack()
        channel_id = body["channel"]["id"]
        row = store.get_group(db(), channel_id)
        store.delete_group(db(), channel_id)
        store.clear_interaction(db(), channel_id)
        client.chat_update(channel=channel_id,
                           ts=(row or {}).get("message_ts") or body["message"]["ts"],
                           text="Group order cancelled.", blocks=[])

    @app.action("add_my_dish")
    def handle_add_dish(ack, body, client):
        """Open a private conversation for this person to pick their dish."""
        ack()
        channel_id = body["channel"]["id"]
        row = store.get_group(db(), channel_id)
        if not row or not row["context"].get("restaurantId"):
            return
        client.chat_postEphemeral(
            channel=channel_id, user=body["user"]["id"],
            text=("Tell me your dish in this channel by mentioning me, e.g. "
                  "`@Canteen add a masala dosa` — I'll put it in the shared cart."))

    @app.action("place_group_order")
    def handle_place_group_order(ack, body, client):
        """The only path to place_food_order for a group. The host clicked it."""
        ack()
        channel_id = body["channel"]["id"]
        row = store.get_group(db(), channel_id)
        if not row:
            return
        clicker = body["user"]["id"]
        if clicker != row["host_user_id"]:
            client.chat_postEphemeral(
                channel=channel_id, user=clicker,
                text=(f"Only <@{row['host_user_id']}> can place this — it goes "
                      "on their Swiggy account."))
            return

        client.chat_update(channel=channel_id, ts=row["message_ts"], blocks=[],
                           text=":hourglass_flowing_sand: _Placing the order…_")
        try:
            with cart_lock(channel_id):
                reply = converse(
                    channel_id, row["host_user_id"],
                    "Re-read the cart with get_food_cart. If the total is within "
                    f"₹{agent.FOOD_CAP_RUPEES}, place the food order with "
                    "paymentMethod COD and report the order id.",
                    ["food"],
                    extra_system=agent.SYSTEM + "\n" + agent.AUTHORISED)
        except Exception as exc:
            status = converse(channel_id, row["host_user_id"],
                              "Call get_food_orders and report my most recent "
                              "order and its status. Do not order anything.",
                              ["food"])
            client.chat_postMessage(
                channel=channel_id,
                text=(f"The order call failed (`{exc}`). I did *not* retry — "
                      f"that risks ordering twice. Latest:\n{status}"))
            return
        finally:
            store.delete_group(db(), channel_id)

        client.chat_postMessage(channel=channel_id, text=reply)

    @app.action("pick_slot")
    def handle_pick_slot(ack, body, client):
        """Whoever clicks owns the booking, on their own Swiggy account."""
        import json
        ack()
        channel_id = body["channel"]["id"]
        user_id = body["user"]["id"]
        proposal = json.loads(body["actions"][0]["value"])
        from canteen.app import PROPOSALS
        PROPOSALS[channel_id] = {"service": "dineout", **proposal}
        client.chat_postMessage(channel=channel_id, text="Confirm this booking",
                                blocks=blocks.confirm_booking(proposal))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_group.py -q`
Expected: PASS, 7 tests.

- [ ] **Step 5: Verify the whole app still imports with group registered**

```bash
CANTEEN_VERIFY_SLACK=0 SLACK_BOT_TOKEN=x SLACK_APP_TOKEN=x GEMINI_API_KEY=x \
CANTEEN_DB=/tmp/c.db uv run python -c "
from canteen import app, group
group.register(app.app, app.converse, app.progress, app.db, app.token_for)
print('handlers:', len(app.app._listeners))
"
```
Expected: prints a handler count of at least 10 and exits 0.

- [ ] **Step 6: Commit**

```bash
git add src/canteen/group.py tests/test_group.py
git commit -m "feat: group food order, table booking and pantry restock"
```

---

### Task 7: Manifest, docs and end-to-end verification

**Files:**
- Modify: `slack-app-manifest.yaml`
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `pyproject.toml` (drop the `/canteen` console script name if it no longer fits)

**Interfaces:**
- Consumes: everything above.
- Produces: no code interfaces.

- [ ] **Step 1: Update the Slack manifest**

Replace `slack-app-manifest.yaml`:

```yaml
display_information:
  name: Swiggy
  description: Order food and groceries, and book tables, from Slack.
  background_color: "#fc8019"
features:
  bot_user:
    display_name: Swiggy
    always_online: true
  app_home:
    home_tab_enabled: false
    # Without these two the bot's DM is read-only, and the DM is the whole
    # personal assistant.
    messages_tab_enabled: true
    messages_tab_read_only_enabled: false
oauth_config:
  scopes:
    bot:
      - chat:write
      - app_mentions:read
      - im:history
      - im:write
settings:
  event_subscriptions:
    bot_events:
      - app_mention
      - message.im
  interactivity:
    is_enabled: true
  org_deploy_enabled: false
  socket_mode_enabled: true
  token_rotation_enabled: false
```

The `/canteen` slash command and the `commands` scope are gone — every surface
is now a DM or a mention.

- [ ] **Step 2: Rewrite the README usage section**

Replace the usage and setup sections of `README.md` with:

````markdown
## What it does

**DM the bot** to use your own Swiggy account:

> order me a masala dosa from somewhere south indian
> what did I order last week?
> we're out of milk and coffee — get some
> book a table for four on Saturday at 8

**Mention it in a channel** for group things:

> @Swiggy lunch — group food order on your account
> @Swiggy book a table for 8 at 8pm
> @Swiggy restock the pantry

## Setup

1. Create the Slack app from `slack-app-manifest.yaml`, install it, and copy
   the bot token (`xoxb-`) and an app-level token with `connections:write`
   (`xapp-`).
2. Get a Gemini API key at <https://aistudio.google.com/apikey>.
3. Copy `.env.example` to `.env` and fill in the three values.
4. `uv run canteen`

## Connecting Swiggy

Each person connects their own account, once. DM the bot, and it walks you
through it:

1. Click the sign-in link it sends you.
2. Sign in to Swiggy.
3. Your browser lands on a page that **fails to load**. That is expected —
   nothing is listening on that address.
4. Copy that page's URL from the address bar and paste it back into the DM.

Your token is stored against your Slack user id. Carts, orders and addresses
are yours alone.
````

- [ ] **Step 3: Update `.env.example`**

```
# Slack — create an app from slack-app-manifest.yaml, enable Socket Mode
SLACK_BOT_TOKEN=xoxb-
SLACK_APP_TOKEN=xapp-

# Gemini — get a key at aistudio.google.com/apikey
GEMINI_API_KEY=

# Optional: override the model
# CANTEEN_MODEL=gemini-3.6-flash

# Local state
CANTEEN_DB=canteen.db
```

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS. Around 60 tests across `test_store.py`, `test_auth.py`,
`test_agent.py`, `test_blocks.py`, `test_app.py`, `test_group.py`,
`test_slackfmt.py`.

- [ ] **Step 5: Verify a live read-only call still works**

```bash
uv run python -c "
import httpx, os, time
from dotenv import load_dotenv; load_dotenv()
from google import genai
from canteen import agent, auth, store
conn = store.connect(); store.init_schema(conn)
row = conn.execute('select user_id from swiggy_token limit 1').fetchone()
assert row, 'connect an account through the DM flow first'
tok = auth.valid_token(conn, httpx.Client(timeout=30), row['user_id'], time.time())
text, iid = agent.run(genai.Client(), prompt='List my saved delivery addresses with ids.',
                      token=tok, servers=['food'], ctx={})
print(text); print('interaction:', iid)
"
```
Expected: the account's real addresses, and a non-empty interaction id.

- [ ] **Step 6: Confirm the old database does not break the new schema**

The old `canteen.db` has a `swiggy_token` table keyed by `id`, which the new
schema cannot use. Delete it — every person reconnects through the DM flow:

```bash
rm -f canteen.db
```

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "docs: manifest, README and env for the rebuilt assistant"
```

---

## Self-Review

**Spec coverage**

| Spec section | Task |
|---|---|
| Ground truth (tool names, camelCase, constraints) | Global Constraints; enforced in Tasks 3, 6 |
| Two surfaces (DM / channel) | Tasks 5, 6 |
| Per-user auth, paste flow | Task 2; UI in Task 4; handler in Task 5 |
| State split (Swiggy / Gemini / us) | Tasks 1, 3, 5 |
| Architecture, seven modules | Tasks 1–6 |
| Data model, five tables | Task 1 |
| Group food order | Tasks 4, 6 |
| Group table booking | Tasks 4, 6 |
| Pantry restock | Tasks 4, 6 |
| Money: allowlist gate + button gate | Tasks 3, 5, 6 |
| Error handling table | Tasks 2, 5, 6 |
| Testing guards | Tasks 1–6 |

**Gap found and closed:** the spec requires that changing restaurant mid-order
warns and requires a second confirmation. That is carried by the `SYSTEM`
prompt in Task 3 ("Changing restaurant empties it — say so and get a yes before
you do it"), not by code, because the restaurant change happens inside the
model's tool loop where no handler can intercept it. Noted here so the
reviewer does not look for a handler that cannot exist.

**Second gap:** the spec's per-channel cart lock only wraps
`place_group_order` in Task 6. Dish additions arrive through the mention path,
which serialises naturally because each is its own model turn against a real
cart read. The lock therefore protects the order-placement window, which is
the case that actually loses money.

**Placeholder scan:** no TBD, no "handle errors appropriately", every code step
carries real code.

**Type consistency:** `agent.run` returns `tuple[str, str]` in Task 3 and is
consumed as a tuple in Task 5. `store.get_group` returns `context` already
decoded, and Task 6 uses `row["context"]` as a dict. `blocks.confirm_booking`
takes the proposal dict produced by `propose_booking` in Task 3 and consumed by
Task 6's `pick_slot`. `PROPOSALS` is keyed by channel id in both Tasks 5 and 6.
