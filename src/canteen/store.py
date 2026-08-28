"""SQLite persistence. Every read and write to disk goes through this module.

We deliberately hold almost nothing: Swiggy owns the cart and the orders. The
Messages API is stateless, so unlike a server-side-transcript provider, we are
the ones holding the conversation history per channel. What is left is a token
per person, a preference line, that history, and any group flow currently
running.
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
    messages text not null,
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
create table if not exists bot_thread (
    channel_id text not null,
    thread_ts text not null,
    updated_at real not null,
    primary key (channel_id, thread_ts)
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


def take_pending_by_state(conn, state: str) -> dict | None:
    """Same as take_pending, but for the real HTTP callback: it only ever
    gets `state` back from Swiggy, never the Slack user_id."""
    row = conn.execute("select * from pending_auth where state = ?",
                       (state,)).fetchone()
    if not row:
        return None
    conn.execute("delete from pending_auth where state = ?", (state,))
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


# --- conversation continuity ---
#
# The Messages API is stateless, so we hold the transcript ourselves, keyed by
# a channel/DM id, and hand the whole thing back on the next turn.

def set_history(conn, key: str, messages: list[dict], updated_at: float) -> None:
    conn.execute(
        "insert into conversation (key, messages, updated_at) values (?, ?, ?) "
        "on conflict(key) do update set messages=excluded.messages, "
        "updated_at=excluded.updated_at",
        (key, json.dumps(messages), updated_at),
    )
    conn.commit()


def get_history(conn, key: str) -> list[dict] | None:
    row = conn.execute("select messages from conversation where key = ?",
                       (key,)).fetchone()
    return json.loads(row["messages"]) if row else None


def clear_history(conn, key: str) -> None:
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


# --- bot-started threads ---
#
# Channels only ever tell us about a plain mention (app_mention); a thread
# reply with no @mention in it arrives as a generic message event. To let
# people keep talking without re-mentioning every turn, we remember which
# threads the assistant itself is already part of and only auto-continue those.

def mark_bot_thread(conn, channel_id: str, thread_ts: str, updated_at: float) -> None:
    conn.execute(
        "insert into bot_thread (channel_id, thread_ts, updated_at) "
        "values (?, ?, ?) on conflict(channel_id, thread_ts) do update set "
        "updated_at=excluded.updated_at",
        (channel_id, thread_ts, updated_at),
    )
    conn.commit()


def is_bot_thread(conn, channel_id: str, thread_ts: str) -> bool:
    row = conn.execute(
        "select 1 from bot_thread where channel_id = ? and thread_ts = ?",
        (channel_id, thread_ts)).fetchone()
    return row is not None
