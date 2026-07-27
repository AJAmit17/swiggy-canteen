"""SQLite persistence. Every read and write to disk goes through this module."""

from __future__ import annotations

import json
import os
import sqlite3
import threading

DEFAULT_DIET = "nonveg"

# sqlite3 connections cannot be used from a thread other than the one that
# created them, and Slack Bolt runs every listener on a pool thread while
# APScheduler runs jobs on its own. So connections are per-thread, not global.
_local = threading.local()

SCHEMA = """
create table if not exists swiggy_token (
    id integer primary key check (id = 1),
    access_token text not null,
    refresh_token text,
    expires_at real not null
);
create table if not exists user_profile (
    user_id text primary key,
    diet text not null,
    blocklist text not null,
    budget integer
);
create table if not exists office (
    channel_id text primary key,
    address_id text not null,
    timezone text not null,
    roll_call_time text not null
);
create table if not exists policy (
    channel_id text primary key,
    per_head_cap integer,
    vendor_allowlist text not null
);
create table if not exists team_order (
    id integer primary key autoincrement,
    channel_id text not null,
    restaurant_id text not null,
    restaurant_name text not null,
    cuisines text not null,
    participants text not null,
    total integer not null,
    ordered_at real not null
);
create table if not exists rating (
    user_id text not null,
    restaurant_id text not null,
    score integer not null,
    primary key (user_id, restaurant_id)
);
create table if not exists spend (
    id integer primary key autoincrement,
    user_id text not null,
    order_id text not null,
    amount integer not null
);
create table if not exists par_level (
    product_id text primary key,
    name text not null,
    qty integer not null
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    """The connection for the calling thread, opened on first use.

    Safe and cheap to call on every access — callers should not cache the
    result across threads.
    """
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


# --- token (single host account, so the row id is pinned to 1) ---

def save_token(conn, access_token: str, refresh_token: str | None, expires_at: float) -> None:
    conn.execute(
        "insert into swiggy_token (id, access_token, refresh_token, expires_at) "
        "values (1, ?, ?, ?) on conflict(id) do update set "
        "access_token=excluded.access_token, refresh_token=excluded.refresh_token, "
        "expires_at=excluded.expires_at",
        (access_token, refresh_token, expires_at),
    )
    conn.commit()


def get_token(conn) -> dict | None:
    row = conn.execute("select * from swiggy_token where id = 1").fetchone()
    return dict(row) if row else None


# --- profiles ---

def upsert_profile(conn, user_id: str, diet: str, blocklist: list[str], budget: int | None) -> None:
    conn.execute(
        "insert into user_profile (user_id, diet, blocklist, budget) values (?, ?, ?, ?) "
        "on conflict(user_id) do update set diet=excluded.diet, "
        "blocklist=excluded.blocklist, budget=excluded.budget",
        (user_id, diet, json.dumps(blocklist), budget),
    )
    conn.commit()


def _profile_row(row) -> dict:
    return {
        "user_id": row["user_id"],
        "diet": row["diet"],
        "blocklist": json.loads(row["blocklist"]),
        "budget": row["budget"],
    }


def get_profile(conn, user_id: str) -> dict | None:
    row = conn.execute("select * from user_profile where user_id = ?", (user_id,)).fetchone()
    return _profile_row(row) if row else None


def get_profiles(conn, user_ids: list[str]) -> list[dict]:
    """Unknown users get a permissive default so a new hire never blocks a lunch."""
    return [
        get_profile(conn, uid)
        or {"user_id": uid, "diet": DEFAULT_DIET, "blocklist": [], "budget": None}
        for uid in user_ids
    ]


# --- office + policy ---

def upsert_office(conn, channel_id: str, address_id: str, timezone: str,
                  roll_call_time: str) -> None:
    conn.execute(
        "insert into office (channel_id, address_id, timezone, roll_call_time) "
        "values (?, ?, ?, ?) on conflict(channel_id) do update set "
        "address_id=excluded.address_id, timezone=excluded.timezone, "
        "roll_call_time=excluded.roll_call_time",
        (channel_id, address_id, timezone, roll_call_time),
    )
    conn.commit()


def get_office(conn, channel_id: str) -> dict | None:
    row = conn.execute("select * from office where channel_id = ?", (channel_id,)).fetchone()
    return dict(row) if row else None


def upsert_policy(conn, channel_id: str, per_head_cap: int | None,
                  vendor_allowlist: list[str]) -> None:
    conn.execute(
        "insert into policy (channel_id, per_head_cap, vendor_allowlist) values (?, ?, ?) "
        "on conflict(channel_id) do update set per_head_cap=excluded.per_head_cap, "
        "vendor_allowlist=excluded.vendor_allowlist",
        (channel_id, per_head_cap, json.dumps(vendor_allowlist)),
    )
    conn.commit()


def get_policy(conn, channel_id: str) -> dict:
    row = conn.execute("select * from policy where channel_id = ?", (channel_id,)).fetchone()
    if not row:
        return {"channel_id": channel_id, "per_head_cap": None, "vendor_allowlist": []}
    return {
        "channel_id": row["channel_id"],
        "per_head_cap": row["per_head_cap"],
        "vendor_allowlist": json.loads(row["vendor_allowlist"]),
    }


# --- history ---

def record_order(conn, channel_id, restaurant_id, restaurant_name, cuisines,
                 participants, total, ordered_at) -> None:
    conn.execute(
        "insert into team_order (channel_id, restaurant_id, restaurant_name, cuisines, "
        "participants, total, ordered_at) values (?, ?, ?, ?, ?, ?, ?)",
        (channel_id, restaurant_id, restaurant_name, json.dumps(cuisines),
         json.dumps(participants), total, ordered_at),
    )
    conn.commit()


def recent_orders(conn, channel_id: str, since_ts: float) -> list[dict]:
    rows = conn.execute(
        "select * from team_order where channel_id = ? and ordered_at >= ? "
        "order by ordered_at desc",
        (channel_id, since_ts),
    ).fetchall()
    return [
        {
            "restaurant_id": r["restaurant_id"],
            "restaurant_name": r["restaurant_name"],
            "cuisines": json.loads(r["cuisines"]),
            "participants": json.loads(r["participants"]),
            "total": r["total"],
            "ordered_at": r["ordered_at"],
        }
        for r in rows
    ]


def record_rating(conn, user_id: str, restaurant_id: str, score: int) -> None:
    conn.execute(
        "insert into rating (user_id, restaurant_id, score) values (?, ?, ?) "
        "on conflict(user_id, restaurant_id) do update set score=excluded.score",
        (user_id, restaurant_id, score),
    )
    conn.commit()


def restaurant_ratings(conn) -> dict[str, float]:
    rows = conn.execute(
        "select restaurant_id, avg(score) as avg_score from rating group by restaurant_id"
    ).fetchall()
    return {r["restaurant_id"]: float(r["avg_score"]) for r in rows}


def record_spend(conn, user_id: str, order_id: str, amount: int) -> None:
    conn.execute(
        "insert into spend (user_id, order_id, amount) values (?, ?, ?)",
        (user_id, order_id, amount),
    )
    conn.commit()


# --- pantry par levels ---

def set_par_level(conn, product_id: str, name: str, qty: int) -> None:
    conn.execute(
        "insert into par_level (product_id, name, qty) values (?, ?, ?) "
        "on conflict(product_id) do update set name=excluded.name, qty=excluded.qty",
        (product_id, name, qty),
    )
    conn.commit()


def par_levels(conn) -> dict[str, dict]:
    rows = conn.execute("select * from par_level").fetchall()
    return {r["product_id"]: dict(r) for r in rows}
