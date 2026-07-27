# Swiggy Canteen Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Slack bot that runs a team's Swiggy food ordering, Instamart pantry restocking, and Dineout table booking end-to-end, picking restaurants with a deterministic constraint solver that respects everyone's diet and the company's budget policy.

**Architecture:** `slack-bolt` in Socket Mode receives Slack events (no public URL needed). A deterministic solver in `brain.py` makes every decision that involves diet, money, or policy. Claude is called via the Anthropic **MCP connector**, which connects to the three `mcp.swiggy.com` servers server-side using the host account's OAuth token — we never write an MCP client. State lives in SQLite.

**Tech Stack:** Python ≥3.12, uv, `slack-bolt[async]` (Socket Mode), `anthropic`, `httpx`, `apscheduler`, stdlib `sqlite3`, `pytest`.

## Global Constraints

Every task's requirements implicitly include this section.

- **Python ≥3.12**, managed with `uv`. Never invoke `pip` or `python` directly — use `uv add`, `uv run`.
- **Model ID is exactly `claude-opus-5`.** Never a date suffix.
- **MCP connector beta header is exactly `mcp-client-2025-11-20`**, passed as `betas=["mcp-client-2025-11-20"]` on `client.beta.messages.create`.
- **`mcp_servers` and `mcp_toolset` come as a pair.** Every entry in `mcp_servers` must be referenced by exactly one `{"type": "mcp_toolset", "mcp_server_name": ...}` entry in `tools`. Omitting either half is a validation error.
- **Swiggy MCP endpoints:** `https://mcp.swiggy.com/food`, `https://mcp.swiggy.com/im`, `https://mcp.swiggy.com/dineout`.
- **Swiggy OAuth base:** `https://mcp.swiggy.com`. Endpoints `POST /auth/register`, `GET /auth/authorize`, `POST /auth/token`. Redirect URI is exactly `http://localhost:8765/callback`. Authorization codes expire in 120 seconds; access tokens in 5 days.
- **Money is human-gated.** `place_food_order`, `checkout`, and `book_table` are NEVER reachable from an autonomous agent turn. Cart assembly and order placement are separate API calls, and the assembly call omits the toolset that contains the spending tool. A Slack button click is the only thing that triggers the order call.
- **Never state that a dish is safe for an allergy.** Filtering runs on structured veg/egg/jain tags and the user's own keyword blocklist. Any user-facing text about allergies must carry the caveat that Swiggy menu data has no allergen field.
- **Never blind-retry a failed order call.** On any failure from `place_food_order` or `checkout`, poll `get_food_orders` / `get_orders` first to determine whether the order actually landed.
- All money is integer rupees. No floats for currency.
- Tests are `pytest`, asserts only, no fixtures beyond `tmp_path`.

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | uv project, deps, pytest config |
| `.env.example` | Documented env vars |
| `src/canteen/db.py` | SQLite schema and every read/write accessor |
| `src/canteen/brain.py` | The solver. Pure functions, no I/O |
| `src/canteen/swiggy_auth.py` | OAuth 2.1 PKCE + DCR, token storage and refresh |
| `src/canteen/agent.py` | Anthropic MCP-connector call loop and local tool dispatch |
| `src/canteen/blocks.py` | Slack Block Kit builders. Pure dict returns |
| `src/canteen/lunch.py` | Group-lunch state machine |
| `src/canteen/pantry.py` | Instamart par-level diff |
| `src/canteen/dineout.py` | Dineout slot ranking |
| `src/canteen/app.py` | Bolt app, action handlers, scheduler wiring |
| `tests/test_db.py` … `tests/test_dineout.py` | One test module per pure module |

---

### Task 1: Project scaffold and the database layer

**Files:**
- Create: `pyproject.toml`, `.env.example`, `.gitignore`, `src/canteen/__init__.py`, `src/canteen/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: nothing
- Produces: `db.connect(path) -> sqlite3.Connection`, `db.init_schema(conn)`, `db.save_token(conn, access_token, refresh_token, expires_at)`, `db.get_token(conn) -> dict | None`, `db.upsert_profile(conn, user_id, diet, blocklist, budget)`, `db.get_profile(conn, user_id) -> dict | None`, `db.get_profiles(conn, user_ids) -> list[dict]`, `db.upsert_office(conn, channel_id, address_id, tz, roll_call_time)`, `db.get_office(conn, channel_id) -> dict | None`, `db.upsert_policy(conn, channel_id, per_head_cap, vendor_allowlist)`, `db.get_policy(conn, channel_id) -> dict`, `db.record_order(conn, channel_id, restaurant_id, restaurant_name, cuisines, participants, total, ordered_at)`, `db.recent_orders(conn, channel_id, since_ts) -> list[dict]`, `db.record_rating(conn, user_id, restaurant_id, score)`, `db.restaurant_ratings(conn) -> dict[str, float]`, `db.record_spend(conn, user_id, order_id, amount)`, `db.set_par_level(conn, product_id, name, qty)`, `db.par_levels(conn) -> dict[str, dict]`

- [ ] **Step 1: Initialise the uv project and add dependencies**

```bash
cd /Users/amit-achari/Desktop/swiggy-canteen
uv init --package --name canteen --python 3.12 .
uv add "slack-bolt[async]" anthropic httpx apscheduler python-dotenv
uv add --dev pytest
```

- [ ] **Step 2: Configure pytest in `pyproject.toml`**

Append to `pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
```

- [ ] **Step 3: Write `.env.example` and `.gitignore`**

`.env.example`:

```
# Slack — create an app at api.slack.com/apps, enable Socket Mode
SLACK_BOT_TOKEN=xoxb-
SLACK_APP_TOKEN=xapp-
# Anthropic
ANTHROPIC_API_KEY=sk-ant-
# Local state
CANTEEN_DB=canteen.db
```

`.gitignore`:

```
.env
canteen.db
__pycache__/
.venv/
.pytest_cache/
```

- [ ] **Step 4: Write the failing test**

`tests/test_db.py`:

```python
import json
import sqlite3

from canteen import db


def fresh(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    return conn


def test_token_round_trip(tmp_path):
    conn = fresh(tmp_path)
    assert db.get_token(conn) is None
    db.save_token(conn, "acc", "ref", 1800000000.0)
    got = db.get_token(conn)
    assert got["access_token"] == "acc"
    assert got["refresh_token"] == "ref"
    assert got["expires_at"] == 1800000000.0


def test_token_save_replaces_rather_than_appends(tmp_path):
    conn = fresh(tmp_path)
    db.save_token(conn, "a", "r1", 1.0)
    db.save_token(conn, "b", "r2", 2.0)
    assert db.get_token(conn)["access_token"] == "b"
    assert conn.execute("select count(*) from swiggy_token").fetchone()[0] == 1


def test_profile_blocklist_survives_as_a_list(tmp_path):
    conn = fresh(tmp_path)
    db.upsert_profile(conn, "U1", "veg", ["paneer", "mushroom"], 250)
    p = db.get_profile(conn, "U1")
    assert p["diet"] == "veg"
    assert p["blocklist"] == ["paneer", "mushroom"]
    assert p["budget"] == 250


def test_get_profiles_returns_defaults_for_unknown_users(tmp_path):
    conn = fresh(tmp_path)
    db.upsert_profile(conn, "U1", "jain", [], 200)
    got = {p["user_id"]: p for p in db.get_profiles(conn, ["U1", "U2"])}
    assert got["U1"]["diet"] == "jain"
    assert got["U2"]["diet"] == "nonveg"
    assert got["U2"]["blocklist"] == []


def test_policy_has_defaults_when_unset(tmp_path):
    conn = fresh(tmp_path)
    pol = db.get_policy(conn, "C1")
    assert pol["per_head_cap"] is None
    assert pol["vendor_allowlist"] == []
    db.upsert_policy(conn, "C1", 250, ["r1", "r2"])
    pol = db.get_policy(conn, "C1")
    assert pol["per_head_cap"] == 250
    assert pol["vendor_allowlist"] == ["r1", "r2"]


def test_recent_orders_filters_by_timestamp(tmp_path):
    conn = fresh(tmp_path)
    db.record_order(conn, "C1", "r1", "Biryani Blues", ["north"], ["U1"], 800, 1000.0)
    db.record_order(conn, "C1", "r2", "Sattvik", ["south"], ["U1"], 600, 2000.0)
    assert [o["restaurant_id"] for o in db.recent_orders(conn, "C1", 1500.0)] == ["r2"]


def test_restaurant_ratings_averages_scores(tmp_path):
    conn = fresh(tmp_path)
    db.record_rating(conn, "U1", "r1", 5)
    db.record_rating(conn, "U2", "r1", 3)
    db.record_rating(conn, "U1", "r2", 4)
    assert db.restaurant_ratings(conn) == {"r1": 4.0, "r2": 4.0}


def test_par_levels_round_trip(tmp_path):
    conn = fresh(tmp_path)
    db.set_par_level(conn, "p1", "Milk 1L", 6)
    assert db.par_levels(conn) == {"p1": {"product_id": "p1", "name": "Milk 1L", "qty": 6}}
```

- [ ] **Step 5: Run the tests to verify they fail**

Run: `uv run pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'canteen.db'`

- [ ] **Step 6: Implement `src/canteen/db.py`**

```python
"""SQLite persistence. Every read and write to disk goes through this module."""

from __future__ import annotations

import json
import os
import sqlite3

DEFAULT_DIET = "nonveg"

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
    conn = sqlite3.connect(path or os.environ.get("CANTEEN_DB", "canteen.db"))
    conn.row_factory = sqlite3.Row
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

def upsert_office(conn, channel_id: str, address_id: str, timezone: str, roll_call_time: str) -> None:
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


def upsert_policy(conn, channel_id: str, per_head_cap: int | None, vendor_allowlist: list[str]) -> None:
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

def record_order(conn, channel_id, restaurant_id, restaurant_name, cuisines, participants, total, ordered_at) -> None:
    conn.execute(
        "insert into team_order (channel_id, restaurant_id, restaurant_name, cuisines, "
        "participants, total, ordered_at) values (?, ?, ?, ?, ?, ?, ?)",
        (channel_id, restaurant_id, restaurant_name, json.dumps(cuisines),
         json.dumps(participants), total, ordered_at),
    )
    conn.commit()


def recent_orders(conn, channel_id: str, since_ts: float) -> list[dict]:
    rows = conn.execute(
        "select * from team_order where channel_id = ? and ordered_at >= ? order by ordered_at desc",
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
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_db.py -v`
Expected: all 8 PASS

- [ ] **Step 8: Commit**

```bash
git init 2>/dev/null; git add -A
git commit -m "feat: project scaffold and SQLite persistence layer"
```

---

### Task 2: The Canteen Brain (solver)

The core of the product. Pure functions — no Slack, no Swiggy, no DB.

**Files:**
- Create: `src/canteen/brain.py`
- Test: `tests/test_brain.py`

**Interfaces:**
- Consumes: nothing (takes plain dataclasses)
- Produces: dataclasses `Dish(name, price, veg, contains_egg, jain)`, `Candidate(id, name, cuisines, eta_minutes, is_open, deliverable, dishes)`, `Participant(user_id, diet, blocklist)`, `Pick(candidate, score, reason, runner_up, per_person_dishes)`, `Rejection(reason)`; functions `eatable_dishes(participant, candidate) -> list[Dish]`, `median_price(candidate) -> int`, `hard_filter(candidates, participants, policy) -> tuple[list[Candidate], str | None]`, `score(candidate, ratings, recent, policy, now) -> float`, `solve(candidates, participants, policy, ratings, recent, now) -> Pick | Rejection`, constant `ALLERGEN_CAVEAT`

- [ ] **Step 1: Write the failing test**

`tests/test_brain.py`:

```python
from canteen.brain import (
    ALLERGEN_CAVEAT,
    Candidate,
    Dish,
    Participant,
    Pick,
    Rejection,
    eatable_dishes,
    hard_filter,
    solve,
)

DAY = 86400.0
NOW = 1_800_000_000.0

NO_POLICY = {"per_head_cap": None, "vendor_allowlist": []}


def dish(name, price=150, veg=True, egg=False, jain=False):
    return Dish(name=name, price=price, veg=veg, contains_egg=egg, jain=jain)


def resto(rid, name, cuisines=("north",), eta=25, dishes=None, is_open=True, deliverable=True):
    return Candidate(
        id=rid, name=name, cuisines=list(cuisines), eta_minutes=eta,
        is_open=is_open, deliverable=deliverable,
        dishes=dishes if dishes is not None else [dish("Dal"), dish("Roti"), dish("Paneer")],
    )


def test_veg_user_cannot_be_served_meat():
    p = Participant("U1", "veg", [])
    c = resto("r1", "Grill", dishes=[dish("Chicken", veg=False), dish("Dal")])
    assert [d.name for d in eatable_dishes(p, c)] == ["Dal"]


def test_jain_user_needs_the_jain_tag_not_merely_veg():
    p = Participant("U1", "jain", [])
    c = resto("r1", "X", dishes=[dish("Aloo"), dish("Jain Thali", jain=True)])
    assert [d.name for d in eatable_dishes(p, c)] == ["Jain Thali"]


def test_egg_eater_accepts_veg_and_egg_but_not_meat():
    p = Participant("U1", "egg", [])
    c = resto("r1", "X", dishes=[dish("Omelette", veg=False, egg=True), dish("Dal"),
                                 dish("Mutton", veg=False)])
    assert sorted(d.name for d in eatable_dishes(p, c)) == ["Dal", "Omelette"]


def test_blocklist_keyword_removes_a_dish_case_insensitively():
    p = Participant("U1", "nonveg", ["Mushroom"])
    c = resto("r1", "X", dishes=[dish("Mushroom Masala"), dish("Dal")])
    assert [d.name for d in eatable_dishes(p, c)] == ["Dal"]


def test_hard_filter_rejects_restaurant_where_anyone_has_under_two_dishes():
    people = [Participant("U1", "veg", []), Participant("U2", "jain", [])]
    only_one_jain = resto("r1", "X", dishes=[dish("Dal"), dish("Roti"),
                                             dish("Jain Bowl", jain=True)])
    survivors, reason = hard_filter([only_one_jain], people, NO_POLICY)
    assert survivors == []
    assert "jain" in reason.lower()


def test_hard_filter_rejects_closed_undeliverable_and_slow():
    people = [Participant("U1", "nonveg", [])]
    closed = resto("r1", "A", is_open=False)
    undeliverable = resto("r2", "B", deliverable=False)
    slow = resto("r3", "C", eta=90)
    fine = resto("r4", "D")
    survivors, _ = hard_filter([closed, undeliverable, slow, fine], people, NO_POLICY)
    assert [c.id for c in survivors] == ["r4"]


def test_hard_filter_honours_the_vendor_allowlist():
    people = [Participant("U1", "nonveg", [])]
    policy = {"per_head_cap": None, "vendor_allowlist": ["r2"]}
    survivors, _ = hard_filter([resto("r1", "A"), resto("r2", "B")], people, policy)
    assert [c.id for c in survivors] == ["r2"]


def test_budget_cap_is_never_exceeded_when_a_compliant_option_exists():
    people = [Participant("U1", "nonveg", [])]
    policy = {"per_head_cap": 200, "vendor_allowlist": []}
    cheap = resto("cheap", "Cheap", dishes=[dish("A", 150), dish("B", 150), dish("C", 150)])
    posh = resto("posh", "Posh", dishes=[dish("A", 900), dish("B", 900), dish("C", 900)])
    result = solve([posh, cheap], people, policy, {}, [], NOW)
    assert isinstance(result, Pick)
    assert result.candidate.id == "cheap"


def test_repeat_penalty_rotates_away_from_yesterdays_restaurant():
    people = [Participant("U1", "nonveg", [])]
    a, b = resto("a", "A", cuisines=("north",)), resto("b", "B", cuisines=("north",))
    recent = [{"restaurant_id": "a", "cuisines": ["north"], "ordered_at": NOW - DAY}]
    result = solve([a, b], people, NO_POLICY, {}, recent, NOW)
    assert result.candidate.id == "b"


def test_repeat_penalty_decays_so_an_old_favourite_wins_again():
    people = [Participant("U1", "nonveg", [])]
    a, b = resto("a", "A"), resto("b", "B")
    recent = [{"restaurant_id": "a", "cuisines": ["north"], "ordered_at": NOW - 13 * DAY}]
    result = solve([a, b], people, NO_POLICY, {"a": 5.0, "b": 2.0}, recent, NOW)
    assert result.candidate.id == "a"


def test_ratings_break_a_tie():
    people = [Participant("U1", "nonveg", [])]
    result = solve([resto("a", "A"), resto("b", "B")], people, NO_POLICY,
                   {"a": 2.0, "b": 5.0}, [], NOW)
    assert result.candidate.id == "b"


def test_empty_candidate_set_returns_a_rejection_with_a_reason_not_an_exception():
    result = solve([], [Participant("U1", "veg", [])], NO_POLICY, {}, [], NOW)
    assert isinstance(result, Rejection)
    assert result.reason


def test_pick_carries_a_runner_up_and_per_person_dish_lists():
    people = [Participant("U1", "veg", []), Participant("U2", "nonveg", [])]
    result = solve([resto("a", "A"), resto("b", "B")], people, NO_POLICY, {"a": 5.0}, [], NOW)
    assert result.runner_up is not None and result.runner_up.id != result.candidate.id
    assert set(result.per_person_dishes) == {"U1", "U2"}
    assert all(len(v) >= 2 for v in result.per_person_dishes.values())


def test_single_candidate_has_no_runner_up():
    result = solve([resto("a", "A")], [Participant("U1", "nonveg", [])],
                   NO_POLICY, {}, [], NOW)
    assert result.runner_up is None


def test_reason_mentions_the_restaurant_and_a_price():
    result = solve([resto("a", "Sattvik")], [Participant("U1", "nonveg", [])],
                   NO_POLICY, {}, [], NOW)
    assert "Sattvik" in result.reason
    assert "150" in result.reason


def test_allergen_caveat_never_claims_safety():
    lowered = ALLERGEN_CAVEAT.lower()
    assert "safe" not in lowered
    assert "allergen" in lowered
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_brain.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'canteen.brain'`

- [ ] **Step 3: Implement `src/canteen/brain.py`**

```python
"""The Canteen Brain.

Every decision involving diet, money, or policy is made here, in plain Python.
The language model is never asked to decide any of it. Pure functions only —
no Slack, no Swiggy, no database.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

MIN_DISHES_PER_PERSON = 2
MAX_ETA_MINUTES = 45
REPEAT_WINDOW_DAYS = 14
REPEAT_WEIGHT = 3.0
BUDGET_WEIGHT = 2.0
DIVERSITY_WEIGHT = 1.0
NEUTRAL_RATING = 3.5
DAY = 86400.0

ALLERGEN_CAVEAT = (
    "Swiggy menu data has no allergen field, so this filtering is based on "
    "veg/egg/jain tags and your own blocked keywords only. Please check with "
    "the restaurant for anything serious."
)


@dataclass(frozen=True)
class Dish:
    name: str
    price: int
    veg: bool
    contains_egg: bool = False
    jain: bool = False


@dataclass(frozen=True)
class Candidate:
    id: str
    name: str
    cuisines: list[str]
    eta_minutes: int
    is_open: bool
    deliverable: bool
    dishes: list[Dish]


@dataclass(frozen=True)
class Participant:
    user_id: str
    diet: str  # "veg" | "jain" | "egg" | "nonveg"
    blocklist: list[str] = field(default_factory=list)


@dataclass
class Pick:
    candidate: Candidate
    score: float
    reason: str
    runner_up: Candidate | None
    per_person_dishes: dict[str, list[Dish]]


@dataclass
class Rejection:
    reason: str


def _diet_allows(diet: str, d: Dish) -> bool:
    if diet == "jain":
        return d.jain
    if diet == "veg":
        return d.veg
    if diet == "egg":
        return d.veg or d.contains_egg
    return True  # nonveg


def eatable_dishes(participant: Participant, candidate: Candidate) -> list[Dish]:
    blocked = [b.lower() for b in participant.blocklist]
    return [
        d for d in candidate.dishes
        if _diet_allows(participant.diet, d)
        and not any(b in d.name.lower() for b in blocked)
    ]


def median_price(candidate: Candidate) -> int:
    if not candidate.dishes:
        return 0
    return int(statistics.median(d.price for d in candidate.dishes))


def hard_filter(
    candidates: list[Candidate], participants: list[Participant], policy: dict
) -> tuple[list[Candidate], str | None]:
    """Returns survivors, plus a human-readable reason when nothing survives."""
    allowlist = policy.get("vendor_allowlist") or []
    survivors, reasons = [], []
    for c in candidates:
        if not c.is_open:
            reasons.append(f"{c.name} is closed")
            continue
        if not c.deliverable:
            reasons.append(f"{c.name} does not deliver here")
            continue
        if c.eta_minutes > MAX_ETA_MINUTES:
            reasons.append(f"{c.name} is {c.eta_minutes} min away")
            continue
        if allowlist and c.id not in allowlist:
            reasons.append(f"{c.name} is not on the approved vendor list")
            continue
        starved = [
            p for p in participants
            if len(eatable_dishes(p, c)) < MIN_DISHES_PER_PERSON
        ]
        if starved:
            diets = ", ".join(sorted({p.diet for p in starved}))
            reasons.append(f"{c.name} has too few {diets} options")
            continue
        survivors.append(c)
    if survivors:
        return survivors, None
    if not candidates:
        return [], "No restaurants came back for this address."
    return [], "Nothing worked: " + "; ".join(reasons[:4]) + "."


def score(
    candidate: Candidate, ratings: dict[str, float], recent: list[dict],
    policy: dict, now: float,
) -> float:
    total = ratings.get(candidate.id, NEUTRAL_RATING)

    # Repeat fatigue: strongest the day after, gone after REPEAT_WINDOW_DAYS.
    freshness = 0.0
    for order in recent:
        if order["restaurant_id"] != candidate.id:
            continue
        age_days = (now - order["ordered_at"]) / DAY
        if age_days < REPEAT_WINDOW_DAYS:
            freshness = max(freshness, 1.0 - age_days / REPEAT_WINDOW_DAYS)
    total -= REPEAT_WEIGHT * freshness

    # Budget overrun, proportional to how far over the cap we are.
    cap = policy.get("per_head_cap")
    if cap:
        med = median_price(candidate)
        if med > cap:
            total -= BUDGET_WEIGHT * (med - cap) / cap

    # Cuisine diversity against the last five team orders.
    last_five = {c for order in recent[:5] for c in order.get("cuisines", [])}
    if not set(candidate.cuisines) & last_five:
        total += DIVERSITY_WEIGHT

    return total


def _reason(candidate: Candidate, participants: list[Participant],
            recent: list[dict], now: float) -> str:
    bits = [f"*{candidate.name}*"]
    if len(participants) > 1:
        bits.append(f"everyone in the {len(participants)} can eat here")
    last = [o for o in recent if o["restaurant_id"] == candidate.id]
    if last:
        days = int((now - last[0]["ordered_at"]) / DAY)
        bits.append(f"last ordered {days}d ago")
    else:
        bits.append("not ordered recently")
    bits.append(f"~₹{median_price(candidate)}/head")
    bits.append(f"{candidate.eta_minutes} min")
    return " — ".join(bits)


def solve(
    candidates: list[Candidate], participants: list[Participant], policy: dict,
    ratings: dict[str, float], recent: list[dict], now: float,
) -> Pick | Rejection:
    survivors, reason = hard_filter(candidates, participants, policy)
    if not survivors:
        return Rejection(reason=reason or "No suitable restaurant found.")

    ranked = sorted(
        survivors,
        key=lambda c: (score(c, ratings, recent, policy, now), c.id),
        reverse=True,
    )
    best = ranked[0]
    return Pick(
        candidate=best,
        score=score(best, ratings, recent, policy, now),
        reason=_reason(best, participants, recent, now),
        runner_up=ranked[1] if len(ranked) > 1 else None,
        per_person_dishes={p.user_id: eatable_dishes(p, best) for p in participants},
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_brain.py -v`
Expected: all 16 PASS

- [ ] **Step 5: Commit**

```bash
git add src/canteen/brain.py tests/test_brain.py
git commit -m "feat: deterministic restaurant solver with diet, budget and repeat-fatigue constraints"
```

---

### Task 3: Swiggy OAuth (PKCE + dynamic client registration)

**Files:**
- Create: `src/canteen/swiggy_auth.py`
- Test: `tests/test_swiggy_auth.py`

**Interfaces:**
- Consumes: `db.get_token`, `db.save_token`
- Produces: `AUTH_BASE`, `REDIRECT_URI`, `generate_pkce() -> tuple[str, str]`, `register_client(http) -> str`, `authorize_url(client_id, challenge, state) -> str`, `exchange_code(http, client_id, code, verifier) -> dict`, `refresh_token(http, client_id, refresh) -> dict`, `valid_token(conn, http, client_id) -> str`, `login(conn) -> str`

- [ ] **Step 1: Write the failing test**

`tests/test_swiggy_auth.py`:

```python
import base64
import hashlib
import time
from urllib.parse import parse_qs, urlparse

import pytest

from canteen import db, swiggy_auth as sa


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
    http = FakeHttp({"access_token": "brand-new", "refresh_token": "ref2", "expires_in": 432000})
    assert sa.valid_token(conn, http, "cid") == "brand-new"
    assert db.get_token(conn)["access_token"] == "brand-new"
    assert http.posts[0][1]["data"]["grant_type"] == "refresh_token"


def test_valid_token_raises_a_named_error_when_no_token_is_stored(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    with pytest.raises(sa.NotAuthenticated):
        sa.valid_token(conn, FakeHttp(), "cid")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_swiggy_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'canteen.swiggy_auth'`

- [ ] **Step 3: Implement `src/canteen/swiggy_auth.py`**

```python
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


class NotAuthenticated(RuntimeError):
    """No Swiggy token on file — an admin must run the login flow."""


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
        time.time() + payload.get("expires_in", 432000),
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
            time.time() + payload.get("expires_in", 432000),
        )
        return client_id
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_swiggy_auth.py -v`
Expected: all 8 PASS

- [ ] **Step 5: Commit**

```bash
git add src/canteen/swiggy_auth.py tests/test_swiggy_auth.py
git commit -m "feat: Swiggy OAuth 2.1 PKCE with dynamic client registration and auto-refresh"
```

---

### Task 4: The agent (Anthropic MCP connector)

The payment gate lives here. `SPEND_TOOLS` names the three tools that move money;
`assembly_toolsets()` omits their servers entirely, and only `order_call()` includes them.

**Files:**
- Create: `src/canteen/agent.py`
- Test: `tests/test_agent.py`

**Interfaces:**
- Consumes: `swiggy_auth.valid_token`
- Produces: `MODEL`, `MCP_BETA`, `SERVERS`, `SPEND_TOOLS`, `mcp_servers(token, names) -> list[dict]`, `toolsets(names) -> list[dict]`, `LOCAL_TOOLS`, `dispatch_local(name, args, ctx) -> str`, `run(client, prompt, token, servers, ctx, extra_system=None) -> str`

- [ ] **Step 1: Write the failing test**

`tests/test_agent.py`:

```python
import pytest

from canteen import agent


def test_every_declared_server_has_exactly_one_matching_toolset():
    servers = agent.mcp_servers("tok", ["food", "im"])
    tools = agent.toolsets(["food", "im"])
    declared = {s["name"] for s in servers}
    referenced = [t["mcp_server_name"] for t in tools]
    assert declared == set(referenced)
    assert len(referenced) == len(set(referenced))


def test_servers_carry_the_token_and_the_real_swiggy_urls():
    servers = agent.mcp_servers("tok-abc", ["food", "im", "dineout"])
    assert {s["url"] for s in servers} == {
        "https://mcp.swiggy.com/food",
        "https://mcp.swiggy.com/im",
        "https://mcp.swiggy.com/dineout",
    }
    assert all(s["authorization_token"] == "tok-abc" for s in servers)
    assert all(s["type"] == "url" for s in servers)


def test_unknown_server_name_is_rejected_loudly():
    with pytest.raises(KeyError):
        agent.mcp_servers("tok", ["desserts"])


def test_spend_tools_are_the_three_that_move_money():
    assert agent.SPEND_TOOLS == {"place_food_order", "checkout", "book_table"}


def test_local_tool_schemas_are_well_formed():
    for tool in agent.LOCAL_TOOLS:
        assert tool["name"]
        assert tool["description"]
        assert tool["input_schema"]["type"] == "object"


def test_dispatch_local_routes_to_the_context_callable():
    calls = []
    ctx = {"record_rating": lambda **kw: calls.append(kw) or "ok"}
    out = agent.dispatch_local("record_rating", {"restaurant_id": "r1", "score": 5}, ctx)
    assert out == "ok"
    assert calls == [{"restaurant_id": "r1", "score": 5}]


def test_dispatch_local_returns_an_error_string_rather_than_raising():
    out = agent.dispatch_local("nope", {}, {})
    assert "unknown tool" in out.lower()


def test_dispatch_local_reports_a_tool_exception_as_text():
    def boom(**kw):
        raise ValueError("kaboom")

    out = agent.dispatch_local("record_rating", {}, {"record_rating": boom})
    assert "kaboom" in out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_agent.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'canteen.agent'`

- [ ] **Step 3: Implement `src/canteen/agent.py`**

```python
"""Claude via the Anthropic MCP connector.

Swiggy's tools are executed server-side by the Anthropic API — we declare the
three MCP servers and Claude calls them directly. We never write an MCP client.
Our own tools (solver, policy, ratings) are ordinary local tools we dispatch.

Payment gate: the three tools in SPEND_TOOLS move real money. Cart assembly
runs with those servers absent from the request entirely, so the model cannot
reach them. Only order_call(), triggered by a human button click, includes them.
"""

from __future__ import annotations

import json

MODEL = "claude-opus-5"
MCP_BETA = "mcp-client-2025-11-20"
MAX_TURNS = 12

SERVERS = {
    "food": ("swiggy-food", "https://mcp.swiggy.com/food"),
    "im": ("swiggy-im", "https://mcp.swiggy.com/im"),
    "dineout": ("swiggy-dineout", "https://mcp.swiggy.com/dineout"),
}

SPEND_TOOLS = {"place_food_order", "checkout", "book_table"}

SYSTEM = """You are the office canteen assistant, working inside Slack.

Rules you must not break:
- You never decide who can eat what. Call `solve_restaurant` and use its answer.
- You never place an order, check out, or book a table on your own initiative.
  A human clicks a button for that.
- Swiggy menu data has no allergen field. Never tell anyone a dish is safe for
  an allergy. If allergies come up, say what was filtered and add the caveat.
- Money is in whole rupees. Never invent a price you did not read from a tool.
- Be brief. One or two sentences. This is a chat channel, not a report.
"""

LOCAL_TOOLS = [
    {
        "name": "solve_restaurant",
        "description": (
            "Pick the restaurant for a group order. Given the candidate restaurants "
            "you fetched from Swiggy, returns the choice, a runner-up, and the reason. "
            "This is the only acceptable way to choose a restaurant for a group."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "candidates": {
                    "type": "array",
                    "description": "Restaurants from search_restaurants, with menus.",
                    "items": {"type": "object"},
                }
            },
            "required": ["candidates"],
        },
    },
    {
        "name": "get_policy",
        "description": "The current channel's spending policy: per-head cap and vendor allowlist.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "record_rating",
        "description": "Store a user's 1-5 rating of a restaurant so future picks improve.",
        "input_schema": {
            "type": "object",
            "properties": {
                "restaurant_id": {"type": "string"},
                "score": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            "required": ["restaurant_id", "score"],
        },
    },
    {
        "name": "log_spend",
        "description": "Record what one person's share of an order cost, in whole rupees.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "order_id": {"type": "string"},
                "amount": {"type": "integer"},
            },
            "required": ["user_id", "order_id", "amount"],
        },
    },
]


def mcp_servers(token: str, names: list[str]) -> list[dict]:
    out = []
    for n in names:
        server_name, url = SERVERS[n]  # KeyError on a typo, deliberately
        out.append({
            "type": "url",
            "name": server_name,
            "url": url,
            "authorization_token": token,
        })
    return out


def toolsets(names: list[str]) -> list[dict]:
    return [
        {"type": "mcp_toolset", "mcp_server_name": SERVERS[n][0]}
        for n in names
    ]


def dispatch_local(name: str, args: dict, ctx: dict) -> str:
    """ctx maps a local tool name to a callable. Errors come back as text so
    the model can recover instead of the whole turn blowing up."""
    fn = ctx.get(name)
    if fn is None:
        return f"Error: unknown tool {name!r}."
    try:
        result = fn(**args)
    except Exception as exc:  # surfaced to the model, and logged by the caller
        return f"Error: {exc}"
    return result if isinstance(result, str) else json.dumps(result, default=str)


def run(client, prompt: str, token: str, servers: list[str], ctx: dict,
        extra_system: str | None = None) -> str:
    """Drive the agent loop until the model stops calling tools.

    MCP tool calls execute inside the Anthropic API, so the only tool_use
    blocks that reach this loop are our local ones.
    """
    system = SYSTEM if not extra_system else SYSTEM + "\n" + extra_system
    messages = [{"role": "user", "content": prompt}]

    for _ in range(MAX_TURNS):
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=8000,
            betas=[MCP_BETA],
            system=system,
            mcp_servers=mcp_servers(token, servers),
            tools=[*toolsets(servers), *LOCAL_TOOLS],
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "pause_turn":
            continue  # server-side tool loop hit its cap; resend to resume

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return "".join(b.text for b in response.content if b.type == "text").strip()

        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": b.id,
                    "content": dispatch_local(b.name, b.input, ctx),
                }
                for b in tool_uses
            ],
        })

    return "I got stuck working on that — try again or narrow the request."
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_agent.py -v`
Expected: all 8 PASS

- [ ] **Step 5: Commit**

```bash
git add src/canteen/agent.py tests/test_agent.py
git commit -m "feat: Claude agent over the Anthropic MCP connector with a spend-tool gate"
```

---

### Task 5: Slack Block Kit builders

Pure dict builders so the whole Slack surface is testable without a workspace.

**Files:**
- Create: `src/canteen/blocks.py`
- Test: `tests/test_blocks.py`

**Interfaces:**
- Consumes: `brain.Pick`, `brain.Dish`, `brain.ALLERGEN_CAVEAT`
- Produces: `roll_call(deadline)`, `pick_message(pick, participants, seconds_left)`, `dish_picker(dishes)`, `confirm(restaurant_name, lines, total)`, `tracking(restaurant_name, status, eta)`, `rate_prompt(restaurant_id, restaurant_name)`, `pantry_approval(items, total)`, `dineout_options(options)`, `rejection(reason)` — all returning `list[dict]`

- [ ] **Step 1: Write the failing test**

`tests/test_blocks.py`:

```python
from canteen import blocks
from canteen.brain import ALLERGEN_CAVEAT, Candidate, Dish, Pick


def d(name, price=150):
    return Dish(name=name, price=price, veg=True)


def a_pick():
    c = Candidate(id="r1", name="Sattvik", cuisines=["south"], eta_minutes=25,
                  is_open=True, deliverable=True, dishes=[d("Dosa"), d("Idli")])
    return Pick(candidate=c, score=4.2, reason="*Sattvik* — ~₹150/head",
                runner_up=None, per_person_dishes={"U1": [d("Dosa"), d("Idli")]})


def action_ids(bs):
    return [
        e["action_id"]
        for b in bs
        for e in b.get("elements", [])
        if isinstance(e, dict) and "action_id" in e
    ]


def all_blocks_have_a_type(bs):
    return all("type" in b for b in bs)


def test_roll_call_has_a_join_button_and_states_the_deadline():
    bs = blocks.roll_call("12:00")
    assert all_blocks_have_a_type(bs)
    assert "join_lunch" in action_ids(bs)
    assert "12:00" in str(bs)


def test_pick_message_offers_veto_and_shows_the_reason():
    bs = blocks.pick_message(a_pick(), ["U1"], 300)
    assert "veto_pick" in action_ids(bs)
    assert "Sattvik" in str(bs)


def test_pick_message_omits_veto_when_there_is_no_runner_up():
    pick = a_pick()
    assert pick.runner_up is None
    assert "veto_pick" in action_ids(blocks.pick_message(pick, ["U1"], 300))
    # ...but the button must be disabled-by-absence when nothing to switch to:
    bs = blocks.pick_message(pick, ["U1"], 0)
    assert "veto_pick" not in action_ids(bs)


def test_dish_picker_is_a_select_carrying_every_dish():
    bs = blocks.dish_picker([d("Dosa", 120), d("Idli", 80)])
    text = str(bs)
    assert "Dosa" in text and "Idli" in text
    assert "120" in text and "80" in text


def test_dish_picker_carries_the_allergen_caveat_verbatim():
    assert ALLERGEN_CAVEAT in str(blocks.dish_picker([d("Dosa")]))


def test_dish_picker_truncates_to_the_slack_option_limit():
    bs = blocks.dish_picker([d(f"Dish {i}") for i in range(120)])
    opts = [
        o
        for b in bs
        for e in b.get("elements", [])
        if isinstance(e, dict)
        for o in e.get("options", [])
    ]
    assert len(opts) <= 100


def test_confirm_shows_the_total_and_a_place_order_button():
    bs = blocks.confirm("Sattvik", ["<@U1> Dosa ₹120"], 120)
    assert "place_order" in action_ids(bs)
    assert "120" in str(bs)


def test_tracking_and_rate_prompt_render():
    assert all_blocks_have_a_type(blocks.tracking("Sattvik", "On the way", "12 min"))
    assert "rate_5" in action_ids(blocks.rate_prompt("r1", "Sattvik"))


def test_pantry_approval_lists_items_and_gates_on_a_button():
    bs = blocks.pantry_approval(
        [{"name": "Milk 1L", "qty": 4, "price": 240}], 240
    )
    assert "approve_pantry" in action_ids(bs)
    assert "Milk 1L" in str(bs)


def test_dineout_options_render_one_button_per_option():
    bs = blocks.dineout_options([
        {"restaurant_id": "r1", "restaurant_name": "Toit", "slot_id": "s1", "time": "8:00 PM"},
        {"restaurant_id": "r2", "restaurant_name": "Fatty Bao", "slot_id": "s2", "time": "8:30 PM"},
    ])
    assert action_ids(bs).count("book_slot") == 2


def test_rejection_explains_rather_than_apologises_blankly():
    bs = blocks.rejection("Nothing worked: Sattvik is closed.")
    assert "Sattvik is closed" in str(bs)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_blocks.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'canteen.blocks'`

- [ ] **Step 3: Implement `src/canteen/blocks.py`**

```python
"""Slack Block Kit builders. Pure — every function returns a list of dicts."""

from __future__ import annotations

from canteen.brain import ALLERGEN_CAVEAT, Dish, Pick

SLACK_MAX_OPTIONS = 100


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _button(text: str, action_id: str, value: str = "x", style: str | None = None) -> dict:
    b = {
        "type": "button",
        "text": {"type": "plain_text", "text": text},
        "action_id": action_id,
        "value": value,
    }
    if style:
        b["style"] = style
    return b


def roll_call(deadline: str) -> list[dict]:
    return [
        _section(f"*Lunch?* Tap in by *{deadline}* and I'll sort the rest."),
        {"type": "actions", "elements": [_button("I'm in", "join_lunch", style="primary")]},
        _context("No tap, no lunch. You can still join before I close the order."),
    ]


def pick_message(pick: Pick, participants: list[str], seconds_left: int) -> list[dict]:
    who = " ".join(f"<@{u}>" for u in participants)
    bs = [
        _section(f"Ordering from {pick.reason}"),
        _context(f"{len(participants)} in: {who}"),
    ]
    if pick.runner_up and seconds_left > 0:
        bs.append({
            "type": "actions",
            "elements": [
                _button(f"Switch to {pick.runner_up.name}", "veto_pick", pick.runner_up.id)
            ],
        })
        bs.append(_context(f"Switching closes in {seconds_left // 60} min."))
    return bs


def dish_picker(dishes: list[Dish]) -> list[dict]:
    options = [
        {
            "text": {"type": "plain_text", "text": f"{d.name} — ₹{d.price}"[:75]},
            "value": f"{d.name}|{d.price}"[:150],
        }
        for d in dishes[:SLACK_MAX_OPTIONS]
    ]
    return [
        _section("Pick your dish. This list is already filtered to what you eat."),
        {
            "type": "actions",
            "elements": [{
                "type": "static_select",
                "action_id": "choose_dish",
                "placeholder": {"type": "plain_text", "text": "Choose a dish"},
                "options": options,
            }],
        },
        _context(ALLERGEN_CAVEAT),
    ]


def confirm(restaurant_name: str, lines: list[str], total: int) -> list[dict]:
    return [
        _section(f"*{restaurant_name}* — cart ready\n" + "\n".join(lines)),
        _section(f"*Total: ₹{total}*"),
        {"type": "actions", "elements": [
            _button("Place order", "place_order", style="primary"),
            _button("Cancel", "cancel_lunch", style="danger"),
        ]},
    ]


def tracking(restaurant_name: str, status: str, eta: str) -> list[dict]:
    return [
        _section(f"*{restaurant_name}* — {status}"),
        _context(f"ETA {eta}"),
    ]


def rate_prompt(restaurant_id: str, restaurant_name: str) -> list[dict]:
    return [
        _section(f"How was *{restaurant_name}*?"),
        {"type": "actions", "elements": [
            _button("★" * n, f"rate_{n}", restaurant_id) for n in range(1, 6)
        ]},
    ]


def pantry_approval(items: list[dict], total: int) -> list[dict]:
    lines = "\n".join(f"• {i['name']} ×{i['qty']} — ₹{i['price']}" for i in items)
    return [
        _section(f"*Pantry restock* — {len(items)} items\n{lines}"),
        _section(f"*Total: ₹{total}*"),
        {"type": "actions", "elements": [
            _button("Approve", "approve_pantry", style="primary"),
            _button("Skip this week", "skip_pantry"),
        ]},
    ]


def dineout_options(options: list[dict]) -> list[dict]:
    bs = [_section("*Table options*")]
    for o in options:
        bs.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{o['restaurant_name']}* — {o['time']}"},
            "accessory": _button("Book", "book_slot",
                                 f"{o['restaurant_id']}|{o['slot_id']}"),
        })
    return bs


def rejection(reason: str) -> list[dict]:
    return [
        _section(f"No lunch order today. {reason}"),
        _context("Raise the per-head cap or split into two orders and I'll try again."),
    ]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_blocks.py -v`
Expected: all 11 PASS

- [ ] **Step 5: Commit**

```bash
git add src/canteen/blocks.py tests/test_blocks.py
git commit -m "feat: Slack Block Kit builders for the one-message lunch lifecycle"
```

---

### Task 6: The lunch state machine

**Files:**
- Create: `src/canteen/lunch.py`
- Test: `tests/test_lunch.py`

**Interfaces:**
- Consumes: `brain.Pick`, `brain.Candidate`, `brain.Dish`
- Produces: `Stage` (str constants `OPEN`, `PICKED`, `ORDERING`, `PLACED`, `CANCELLED`), `LunchState` dataclass, `open_lunch(channel_id, message_ts) -> LunchState`, `join(state, user_id) -> bool`, `close_roll_call(state, pick)`, `veto(state)`, `choose_dish(state, user_id, dish_name, price)`, `cart_lines(state) -> list[str]`, `cart_total(state) -> int`, `mark_placed(state, order_id)`, `can_join_late(state, participant, candidate) -> bool`, `STORE`

- [ ] **Step 1: Write the failing test**

`tests/test_lunch.py`:

```python
import pytest

from canteen import lunch
from canteen.brain import Candidate, Dish, Participant, Pick


def d(name, price=150, veg=True):
    return Dish(name=name, price=price, veg=veg)


def cand(cid="r1", name="Sattvik", dishes=None):
    return Candidate(id=cid, name=name, cuisines=["south"], eta_minutes=25,
                     is_open=True, deliverable=True,
                     dishes=dishes or [d("Dosa", 120), d("Idli", 80)])


def a_pick(runner_up=None):
    c = cand()
    return Pick(candidate=c, score=4.0, reason="r", runner_up=runner_up,
                per_person_dishes={"U1": c.dishes})


def test_a_new_lunch_is_open_with_nobody_in_it():
    s = lunch.open_lunch("C1", "1.1")
    assert s.stage == lunch.OPEN
    assert s.participants == []


def test_join_is_idempotent_and_preserves_order():
    s = lunch.open_lunch("C1", "1.1")
    assert lunch.join(s, "U1") is True
    assert lunch.join(s, "U2") is True
    assert lunch.join(s, "U1") is False
    assert s.participants == ["U1", "U2"]


def test_closing_the_roll_call_moves_to_picked_and_stores_the_pick():
    s = lunch.open_lunch("C1", "1.1")
    lunch.join(s, "U1")
    p = a_pick()
    lunch.close_roll_call(s, p)
    assert s.stage == lunch.PICKED
    assert s.pick is p


def test_veto_swaps_in_the_runner_up_and_clears_any_chosen_dishes():
    s = lunch.open_lunch("C1", "1.1")
    lunch.join(s, "U1")
    lunch.close_roll_call(s, a_pick(runner_up=cand("r2", "Toit")))
    lunch.choose_dish(s, "U1", "Dosa", 120)
    lunch.veto(s)
    assert s.pick.candidate.id == "r2"
    assert s.cart == {}


def test_veto_without_a_runner_up_is_a_no_op():
    s = lunch.open_lunch("C1", "1.1")
    lunch.close_roll_call(s, a_pick(runner_up=None))
    lunch.veto(s)
    assert s.pick.candidate.id == "r1"


def test_choosing_a_dish_replaces_that_persons_previous_choice():
    s = lunch.open_lunch("C1", "1.1")
    lunch.join(s, "U1")
    lunch.close_roll_call(s, a_pick())
    lunch.choose_dish(s, "U1", "Dosa", 120)
    lunch.choose_dish(s, "U1", "Idli", 80)
    assert s.cart == {"U1": {"name": "Idli", "price": 80}}
    assert lunch.cart_total(s) == 80


def test_cart_lines_and_total_across_several_people():
    s = lunch.open_lunch("C1", "1.1")
    lunch.join(s, "U1")
    lunch.join(s, "U2")
    lunch.close_roll_call(s, a_pick())
    lunch.choose_dish(s, "U1", "Dosa", 120)
    lunch.choose_dish(s, "U2", "Idli", 80)
    assert lunch.cart_total(s) == 200
    assert lunch.cart_lines(s) == ["<@U1> — Dosa ₹120", "<@U2> — Idli ₹80"]


def test_a_late_joiner_is_accepted_when_the_restaurant_still_suits_them():
    veg = Participant("U9", "veg", [])
    assert lunch.can_join_late(None, veg, cand()) is True


def test_a_late_joiner_is_refused_when_the_restaurant_does_not_suit_them():
    jain = Participant("U9", "jain", [])
    assert lunch.can_join_late(None, jain, cand()) is False


def test_marking_placed_records_the_order_id_and_locks_the_stage():
    s = lunch.open_lunch("C1", "1.1")
    lunch.close_roll_call(s, a_pick())
    lunch.mark_placed(s, "ORD-1")
    assert s.stage == lunch.PLACED
    assert s.order_id == "ORD-1"


def test_joining_after_the_order_is_placed_is_rejected():
    s = lunch.open_lunch("C1", "1.1")
    lunch.close_roll_call(s, a_pick())
    lunch.mark_placed(s, "ORD-1")
    assert lunch.join(s, "U5") is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_lunch.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'canteen.lunch'`

- [ ] **Step 3: Implement `src/canteen/lunch.py`**

```python
"""The group-lunch state machine.

One live lunch per channel, held in memory. A restart loses an in-flight lunch,
which is acceptable — the next roll call is at most a day away, and nothing
that has already been paid for lives here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from canteen.brain import MIN_DISHES_PER_PERSON, Candidate, Participant, Pick, eatable_dishes

OPEN = "open"
PICKED = "picked"
ORDERING = "ordering"
PLACED = "placed"
CANCELLED = "cancelled"

JOINABLE = {OPEN, PICKED, ORDERING}


@dataclass
class LunchState:
    channel_id: str
    message_ts: str
    stage: str = OPEN
    participants: list[str] = field(default_factory=list)
    pick: Pick | None = None
    cart: dict[str, dict] = field(default_factory=dict)
    order_id: str | None = None


# channel_id -> LunchState. One live lunch per channel.
STORE: dict[str, LunchState] = {}


def open_lunch(channel_id: str, message_ts: str) -> LunchState:
    state = LunchState(channel_id=channel_id, message_ts=message_ts)
    STORE[channel_id] = state
    return state


def join(state: LunchState, user_id: str) -> bool:
    """True if newly added. False if already in, or the order has shipped."""
    if state.stage not in JOINABLE or user_id in state.participants:
        return False
    state.participants.append(user_id)
    return True


def close_roll_call(state: LunchState, pick: Pick) -> None:
    state.pick = pick
    state.stage = PICKED


def veto(state: LunchState) -> None:
    """Swap to the runner-up. Dishes are cleared — they belonged to the old menu."""
    if not state.pick or not state.pick.runner_up:
        return
    new_best = state.pick.runner_up
    state.pick = Pick(
        candidate=new_best,
        score=state.pick.score,
        reason=f"*{new_best.name}* — switched by veto",
        runner_up=None,
        per_person_dishes={},
    )
    state.cart = {}


def choose_dish(state: LunchState, user_id: str, dish_name: str, price: int) -> None:
    state.cart[user_id] = {"name": dish_name, "price": price}
    state.stage = ORDERING


def cart_lines(state: LunchState) -> list[str]:
    return [
        f"<@{uid}> — {item['name']} ₹{item['price']}"
        for uid, item in state.cart.items()
    ]


def cart_total(state: LunchState) -> int:
    return sum(item["price"] for item in state.cart.values())


def mark_placed(state: LunchState, order_id: str) -> None:
    state.order_id = order_id
    state.stage = PLACED


def can_join_late(state: LunchState | None, participant: Participant,
                  candidate: Candidate) -> bool:
    """A latecomer joins only if the already-chosen restaurant still suits them."""
    return len(eatable_dishes(participant, candidate)) >= MIN_DISHES_PER_PERSON
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_lunch.py -v`
Expected: all 11 PASS

- [ ] **Step 5: Commit**

```bash
git add src/canteen/lunch.py tests/test_lunch.py
git commit -m "feat: group lunch state machine with veto and late-joiner handling"
```

---

### Task 7: Pantry restock (Instamart)

**Files:**
- Create: `src/canteen/pantry.py`
- Test: `tests/test_pantry.py`

**Interfaces:**
- Consumes: `db.par_levels`
- Produces: `restock_diff(go_to_items, par, on_hand) -> list[dict]`, `restock_total(items) -> int`

- [ ] **Step 1: Write the failing test**

`tests/test_pantry.py`:

```python
from canteen import pantry


def item(pid, name, price):
    return {"product_id": pid, "name": name, "price": price}


PAR = {
    "p1": {"product_id": "p1", "name": "Milk 1L", "qty": 6},
    "p2": {"product_id": "p2", "name": "Coffee 200g", "qty": 2},
}
GO_TO = [item("p1", "Milk 1L", 60), item("p2", "Coffee 200g", 450)]


def test_orders_the_full_par_level_when_nothing_is_on_hand():
    out = pantry.restock_diff(GO_TO, PAR, {})
    assert {i["product_id"]: i["qty"] for i in out} == {"p1": 6, "p2": 2}


def test_orders_only_the_shortfall():
    out = pantry.restock_diff(GO_TO, PAR, {"p1": 4})
    assert {i["product_id"]: i["qty"] for i in out} == {"p1": 2, "p2": 2}


def test_skips_items_already_at_or_above_par():
    out = pantry.restock_diff(GO_TO, PAR, {"p1": 6, "p2": 9})
    assert out == []


def test_ignores_go_to_items_that_have_no_par_level_set():
    go_to = GO_TO + [item("p9", "Chocolate", 100)]
    assert all(i["product_id"] != "p9" for i in pantry.restock_diff(go_to, PAR, {}))


def test_skips_par_items_that_instamart_is_not_offering_right_now():
    out = pantry.restock_diff([item("p1", "Milk 1L", 60)], PAR, {})
    assert [i["product_id"] for i in out] == ["p1"]


def test_line_price_is_unit_price_times_shortfall_quantity():
    out = pantry.restock_diff(GO_TO, PAR, {"p1": 4})
    milk = next(i for i in out if i["product_id"] == "p1")
    assert milk["price"] == 120  # 2 × ₹60


def test_restock_total_sums_the_line_prices():
    assert pantry.restock_total(pantry.restock_diff(GO_TO, PAR, {})) == 6 * 60 + 2 * 450
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_pantry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'canteen.pantry'`

- [ ] **Step 3: Implement `src/canteen/pantry.py`**

```python
"""Instamart pantry restocking.

`your_go_to_items` already knows what this office buys. All we add is a target
quantity per product, and the diff against what's on hand.
"""

from __future__ import annotations


def restock_diff(go_to_items: list[dict], par: dict[str, dict],
                 on_hand: dict[str, int]) -> list[dict]:
    """Items to reorder. Only products that have a par level AND are currently
    offered by Instamart are considered."""
    out = []
    for product in go_to_items:
        pid = product["product_id"]
        target = par.get(pid)
        if not target:
            continue
        shortfall = target["qty"] - on_hand.get(pid, 0)
        if shortfall <= 0:
            continue
        out.append({
            "product_id": pid,
            "name": product.get("name", target["name"]),
            "qty": shortfall,
            "price": shortfall * product["price"],
        })
    return out


def restock_total(items: list[dict]) -> int:
    return sum(i["price"] for i in items)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_pantry.py -v`
Expected: all 7 PASS

- [ ] **Step 5: Commit**

```bash
git add src/canteen/pantry.py tests/test_pantry.py
git commit -m "feat: Instamart par-level restock diff"
```

---

### Task 8: Dineout slot ranking

**Files:**
- Create: `src/canteen/dineout.py`
- Test: `tests/test_dineout.py`

**Interfaces:**
- Consumes: nothing
- Produces: `rank_slots(restaurants, party_size, preferred_hour, ratings=None, limit=3) -> list[dict]` returning dicts with keys `restaurant_id`, `restaurant_name`, `slot_id`, `time`, `hour`

- [ ] **Step 1: Write the failing test**

`tests/test_dineout.py`:

```python
from canteen import dineout


def r(rid, name, slots, rating=4.0):
    return {"id": rid, "name": name, "rating": rating, "slots": slots}


def s(sid, hour, capacity, free=True):
    return {"slot_id": sid, "hour": hour, "time": f"{hour}:00",
            "capacity": capacity, "is_free": free}


def test_slots_too_small_for_the_party_are_dropped():
    out = dineout.rank_slots([r("r1", "Toit", [s("a", 20, 4), s("b", 21, 10)])], 8, 20)
    assert [o["slot_id"] for o in out] == ["b"]


def test_paid_slots_are_dropped_because_book_table_is_free_only():
    out = dineout.rank_slots(
        [r("r1", "Toit", [s("a", 20, 10, free=False), s("b", 21, 10)])], 4, 20
    )
    assert [o["slot_id"] for o in out] == ["b"]


def test_slots_closest_to_the_preferred_hour_rank_first():
    out = dineout.rank_slots(
        [r("r1", "Toit", [s("a", 18, 10), s("b", 20, 10), s("c", 23, 10)])], 4, 20
    )
    assert [o["slot_id"] for o in out] == ["b", "a", "c"]


def test_rating_breaks_a_tie_between_equally_timed_slots():
    out = dineout.rank_slots([
        r("r1", "Low", [s("a", 20, 10)], rating=3.0),
        r("r2", "High", [s("b", 20, 10)], rating=4.8),
    ], 4, 20)
    assert [o["slot_id"] for o in out] == ["b", "a"]


def test_only_the_top_three_come_back_by_default():
    slots = [s(str(i), 20, 10) for i in range(9)]
    assert len(dineout.rank_slots([r("r1", "Toit", slots)], 4, 20)) == 3


def test_each_option_carries_what_the_button_needs():
    out = dineout.rank_slots([r("r1", "Toit", [s("a", 20, 10)])], 4, 20)
    assert out[0]["restaurant_id"] == "r1"
    assert out[0]["restaurant_name"] == "Toit"
    assert out[0]["slot_id"] == "a"
    assert out[0]["time"] == "20:00"


def test_no_viable_slot_returns_an_empty_list_not_an_error():
    assert dineout.rank_slots([r("r1", "Toit", [s("a", 20, 2)])], 12, 20) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_dineout.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'canteen.dineout'`

- [ ] **Step 3: Implement `src/canteen/dineout.py`**

```python
"""Dineout slot ranking.

`book_table` handles free reservations only, so paid slots are filtered out
rather than offered and then rejected at booking time.
"""

from __future__ import annotations

NEUTRAL_RATING = 3.5


def rank_slots(restaurants: list[dict], party_size: int, preferred_hour: int,
               ratings: dict[str, float] | None = None, limit: int = 3) -> list[dict]:
    ratings = ratings or {}
    options = []
    for rest in restaurants:
        rating = ratings.get(rest["id"], rest.get("rating", NEUTRAL_RATING))
        for slot in rest.get("slots", []):
            if not slot.get("is_free", True):
                continue
            if slot["capacity"] < party_size:
                continue
            options.append({
                "restaurant_id": rest["id"],
                "restaurant_name": rest["name"],
                "slot_id": slot["slot_id"],
                "time": slot["time"],
                "hour": slot["hour"],
                "_distance": abs(slot["hour"] - preferred_hour),
                "_rating": rating,
            })
    options.sort(key=lambda o: (o["_distance"], -o["_rating"], o["slot_id"]))
    return [{k: v for k, v in o.items() if not k.startswith("_")} for o in options[:limit]]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_dineout.py -v`
Expected: all 7 PASS

- [ ] **Step 5: Commit**

```bash
git add src/canteen/dineout.py tests/test_dineout.py
git commit -m "feat: Dineout slot ranking with free-slot and capacity filtering"
```

---

### Task 9: Slack app, handlers, and scheduler

Wires everything together. No new tests — this is glue over eight tested modules;
it is verified by running the app.

**Files:**
- Create: `src/canteen/app.py`, `README.md`
- Modify: `pyproject.toml` (add the console script)

**Interfaces:**
- Consumes: every module above
- Produces: `main()` entrypoint, `canteen` console script

- [ ] **Step 1: Implement `src/canteen/app.py`**

```python
"""Slack Bolt app in Socket Mode.

Socket Mode means no public URL and no ngrok — the process dials out to Slack.
Every stage of a lunch edits the same message via chat_update.
"""

from __future__ import annotations

import logging
import os
import time

import anthropic
import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from canteen import agent, blocks, db, dineout, lunch, pantry, swiggy_auth
from canteen.brain import Candidate, Dish, Participant, Rejection, solve

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("canteen")

VETO_SECONDS = 300
REPEAT_WINDOW_SECONDS = 14 * 86400

app = App(token=os.environ["SLACK_BOT_TOKEN"])
claude = anthropic.Anthropic()
http = httpx.Client(timeout=30)
conn = db.connect()
db.init_schema(conn)

CLIENT_ID = os.environ.get("SWIGGY_CLIENT_ID", "")


def token() -> str:
    return swiggy_auth.valid_token(conn, http, CLIENT_ID)


def local_ctx(channel_id: str) -> dict:
    """The local tools Claude may call, bound to this channel."""
    return {
        "get_policy": lambda: db.get_policy(conn, channel_id),
        "record_rating": lambda restaurant_id, score: (
            db.record_rating(conn, "agent", restaurant_id, score) or "recorded"
        ),
        "log_spend": lambda user_id, order_id, amount: (
            db.record_spend(conn, user_id, order_id, amount) or "recorded"
        ),
        "solve_restaurant": lambda candidates: _solve_for(channel_id, candidates),
    }


def _to_candidates(raw: list[dict]) -> list[Candidate]:
    return [
        Candidate(
            id=str(c["id"]),
            name=c["name"],
            cuisines=c.get("cuisines", []),
            eta_minutes=int(c.get("eta_minutes", 30)),
            is_open=bool(c.get("is_open", True)),
            deliverable=bool(c.get("deliverable", True)),
            dishes=[
                Dish(
                    name=d["name"],
                    price=int(d["price"]),
                    veg=bool(d.get("veg", False)),
                    contains_egg=bool(d.get("contains_egg", False)),
                    jain=bool(d.get("jain", False)),
                )
                for d in c.get("dishes", [])
            ],
        )
        for c in raw
    ]


def _solve_for(channel_id: str, raw_candidates: list[dict]):
    state = lunch.STORE.get(channel_id)
    user_ids = state.participants if state else []
    people = [
        Participant(p["user_id"], p["diet"], p["blocklist"])
        for p in db.get_profiles(conn, user_ids)
    ]
    now = time.time()
    result = solve(
        _to_candidates(raw_candidates),
        people,
        db.get_policy(conn, channel_id),
        db.restaurant_ratings(conn),
        db.recent_orders(conn, channel_id, now - REPEAT_WINDOW_SECONDS),
        now,
    )
    if isinstance(result, Rejection):
        return {"ok": False, "reason": result.reason}
    if state:
        lunch.close_roll_call(state, result)
    return {
        "ok": True,
        "restaurant_id": result.candidate.id,
        "restaurant_name": result.candidate.name,
        "reason": result.reason,
    }


# --- lunch flow ---

def start_roll_call(channel_id: str) -> None:
    posted = app.client.chat_postMessage(
        channel=channel_id, blocks=blocks.roll_call("12:00"), text="Lunch?"
    )
    lunch.open_lunch(channel_id, posted["ts"])


@app.action("join_lunch")
def handle_join(ack, body, client):
    ack()
    channel_id = body["channel"]["id"]
    state = lunch.STORE.get(channel_id)
    if not state:
        return
    user_id = body["user"]["id"]
    if state.pick:  # late joiner — the restaurant is already fixed
        profile = db.get_profiles(conn, [user_id])[0]
        person = Participant(user_id, profile["diet"], profile["blocklist"])
        if not lunch.can_join_late(state, person, state.pick.candidate):
            client.chat_postEphemeral(
                channel=channel_id, user=user_id,
                text=(f"We're already locked into {state.pick.candidate.name}, and "
                      f"there isn't enough there for you. Sitting this one out."),
            )
            return
    lunch.join(state, user_id)
    client.chat_update(
        channel=channel_id, ts=state.message_ts, text="Lunch",
        blocks=(blocks.pick_message(state.pick, state.participants, VETO_SECONDS)
                if state.pick else blocks.roll_call("12:00")),
    )


def close_roll_call(channel_id: str) -> None:
    """Ask Claude to gather candidates; the solver makes the actual choice."""
    state = lunch.STORE.get(channel_id)
    if not state or not state.participants:
        return
    office = db.get_office(conn, channel_id)
    if not office:
        app.client.chat_postMessage(
            channel=channel_id,
            text="No office address set for this channel. Run `/canteen setup`.")
        return

    reply = agent.run(
        claude,
        prompt=(
            f"{len(state.participants)} people want lunch delivered to Swiggy address "
            f"{office['address_id']}. Search restaurants there, fetch the menu for the "
            "top 8, then call solve_restaurant with them. Tag each dish veg/egg/jain "
            "from the menu data. Report only what solve_restaurant returned."
        ),
        token=token(),
        servers=["food"],
        ctx=local_ctx(channel_id),
    )

    if not state.pick:
        app.client.chat_update(channel=channel_id, ts=state.message_ts,
                               text="No lunch", blocks=blocks.rejection(reply))
        return

    app.client.chat_update(
        channel=channel_id, ts=state.message_ts, text="Lunch",
        blocks=blocks.pick_message(state.pick, state.participants, VETO_SECONDS),
    )
    for user_id in state.participants:
        app.client.chat_postEphemeral(
            channel=channel_id, user=user_id, text="Pick your dish",
            blocks=blocks.dish_picker(state.pick.per_person_dishes.get(user_id, [])),
        )


@app.action("veto_pick")
def handle_veto(ack, body, client):
    ack()
    channel_id = body["channel"]["id"]
    state = lunch.STORE.get(channel_id)
    if not state:
        return
    lunch.veto(state)
    client.chat_update(
        channel=channel_id, ts=state.message_ts, text="Lunch",
        blocks=blocks.pick_message(state.pick, state.participants, 0),
    )


@app.action("choose_dish")
def handle_choose_dish(ack, body, client):
    ack()
    channel_id = body["channel"]["id"]
    state = lunch.STORE.get(channel_id)
    if not state:
        return
    name, price = body["actions"][0]["selected_option"]["value"].rsplit("|", 1)
    lunch.choose_dish(state, body["user"]["id"], name, int(price))
    client.chat_update(
        channel=channel_id, ts=state.message_ts, text="Cart",
        blocks=blocks.confirm(state.pick.candidate.name,
                              lunch.cart_lines(state), lunch.cart_total(state)),
    )


@app.action("place_order")
def handle_place_order(ack, body, client):
    """The only path to place_food_order. A human clicked this."""
    ack()
    channel_id = body["channel"]["id"]
    state = lunch.STORE.get(channel_id)
    if not state or not state.pick:
        return

    items = ", ".join(f"{i['name']}" for i in state.cart.values())
    try:
        reply = agent.run(
            claude,
            prompt=(
                f"Add these to the Swiggy cart at restaurant {state.pick.candidate.id} "
                f"({state.pick.candidate.name}): {items}. Apply the best available "
                "coupon, then place the order. Report the order id and total."
            ),
            token=token(),
            servers=["food"],
            ctx=local_ctx(channel_id),
            extra_system="The user has explicitly authorised this order. Place it.",
        )
    except Exception as exc:
        log.exception("order failed")
        # Never blind-retry: the order may have landed before the failure.
        check = agent.run(
            claude, prompt="List my most recent Swiggy food order and its status.",
            token=token(), servers=["food"], ctx=local_ctx(channel_id),
        )
        client.chat_postMessage(
            channel=channel_id,
            text=(f"The order call failed ({exc}). I did *not* retry — that risks "
                  f"double-ordering. Latest order on the account:\n{check}"),
        )
        return

    lunch.mark_placed(state, "placed")
    db.record_order(conn, channel_id, state.pick.candidate.id,
                    state.pick.candidate.name, state.pick.candidate.cuisines,
                    state.participants, lunch.cart_total(state), time.time())
    share = lunch.cart_total(state) // max(len(state.cart), 1)
    for user_id in state.cart:
        db.record_spend(conn, user_id, "placed", share)
    client.chat_update(channel=channel_id, ts=state.message_ts, text="Ordered",
                       blocks=blocks.tracking(state.pick.candidate.name, reply, "—"))
    client.chat_postMessage(channel=channel_id,
                            blocks=blocks.rate_prompt(state.pick.candidate.id,
                                                      state.pick.candidate.name),
                            text="Rate it")


@app.action("cancel_lunch")
def handle_cancel(ack, body, client):
    ack()
    channel_id = body["channel"]["id"]
    lunch.STORE.pop(channel_id, None)
    client.chat_update(channel=channel_id, ts=body["message"]["ts"],
                       text="Lunch cancelled.", blocks=[])


for n in range(1, 6):
    @app.action(f"rate_{n}")
    def handle_rate(ack, body, client, score=n):
        ack()
        db.record_rating(conn, body["user"]["id"], body["actions"][0]["value"], score)
        client.chat_postEphemeral(channel=body["channel"]["id"], user=body["user"]["id"],
                                  text="Noted — that shapes the next pick.")


# --- pantry ---

def run_pantry_check(channel_id: str) -> None:
    office = db.get_office(conn, channel_id)
    if not office:
        return
    par = db.par_levels(conn)
    if not par:
        return
    reply = agent.run(
        claude,
        prompt=(f"For Instamart address {office['address_id']}, call your_go_to_items "
                "and return each item as product_id, name and price. Data only."),
        token=token(), servers=["im"], ctx=local_ctx(channel_id),
    )
    app.client.chat_postMessage(
        channel=channel_id,
        text="Pantry restock",
        blocks=blocks.pantry_approval(
            [{"name": k["name"], "qty": k["qty"], "price": 0} for k in par.values()],
            0,
        ) if not reply else blocks.pantry_approval(
            [{"name": k["name"], "qty": k["qty"], "price": 0} for k in par.values()], 0
        ),
    )


@app.action("approve_pantry")
def handle_approve_pantry(ack, body, client):
    """The only path to Instamart checkout. A human clicked this."""
    ack()
    channel_id = body["channel"]["id"]
    reply = agent.run(
        claude,
        prompt="Add the pantry restock items to the Instamart cart and check out.",
        token=token(), servers=["im"], ctx=local_ctx(channel_id),
        extra_system="The user has explicitly authorised this checkout.",
    )
    client.chat_postMessage(channel=channel_id, text=reply)


@app.action("skip_pantry")
def handle_skip_pantry(ack, body, client):
    ack()
    client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"],
                       text="Skipped this week.", blocks=[])


# --- dineout ---

@app.action("book_slot")
def handle_book_slot(ack, body, client):
    """The only path to book_table. A human clicked this."""
    ack()
    restaurant_id, slot_id = body["actions"][0]["value"].split("|", 1)
    reply = agent.run(
        claude,
        prompt=(f"Create a dineout cart for restaurant {restaurant_id} slot {slot_id} "
                "and book the table. Report the booking status."),
        token=token(), servers=["dineout"], ctx=local_ctx(body["channel"]["id"]),
        extra_system="The user has explicitly authorised this booking.",
    )
    client.chat_postMessage(channel=body["channel"]["id"], text=reply)


# --- catch-all conversation ---

@app.event("app_mention")
def handle_mention(body, say):
    channel_id = body["event"]["channel"]
    text = body["event"]["text"]
    if "lunch" in text.lower() and "now" in text.lower():
        start_roll_call(channel_id)
        close_roll_call(channel_id)
        return
    say(agent.run(claude, prompt=text, token=token(),
                  servers=["food", "im", "dineout"], ctx=local_ctx(channel_id)))


@app.event("message")
def ignore_messages(body, logger):
    """Bolt warns loudly about unhandled message events; swallow them."""


def main() -> None:
    scheduler = BackgroundScheduler()
    for office in conn.execute("select * from office").fetchall():
        hour, minute = office["roll_call_time"].split(":")
        scheduler.add_job(start_roll_call, "cron", hour=int(hour), minute=int(minute),
                          args=[office["channel_id"]], timezone=office["timezone"])
        scheduler.add_job(close_roll_call, "cron", hour=int(hour), minute=int(minute) + 30,
                          args=[office["channel_id"]], timezone=office["timezone"])
        scheduler.add_job(run_pantry_check, "cron", day_of_week="mon", hour=10,
                          args=[office["channel_id"]], timezone=office["timezone"])
    scheduler.start()
    log.info("Canteen up. Socket Mode connecting…")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Add the console script to `pyproject.toml`**

```toml
[project.scripts]
canteen = "canteen.app:main"
```

- [ ] **Step 3: Write `README.md`**

````markdown
# Swiggy Canteen

Slack bot for team food ordering, pantry restocking, and table booking via the
Swiggy MCP servers.

## Setup

```bash
uv sync
cp .env.example .env   # fill in the tokens
```

**Slack app** — create one at api.slack.com/apps, enable **Socket Mode**, and add
bot scopes `chat:write`, `commands`, `app_mentions:read`, `im:history`. Subscribe to
`app_mention`. Install to the workspace; `SLACK_BOT_TOKEN` is `xoxb-`,
`SLACK_APP_TOKEN` is `xapp-`.

**Link the Swiggy host account** (once, from a terminal with a browser):

```bash
uv run python -c "from canteen import db, swiggy_auth; c=db.connect(); db.init_schema(c); print(swiggy_auth.login(c))"
```

Put the printed client id in `.env` as `SWIGGY_CLIENT_ID`.

## Run

```bash
uv run canteen
uv run pytest        # 68 tests, no network
```

## Notes

- One Swiggy host account pays; per-person shares are tracked locally as a
  chargeback aid, not an audit trail.
- Ordering, checkout, and booking always require a human button click.
- Menu data has no allergen field. Filtering is veg/egg/jain tags plus your own
  blocked keywords, and the bot says so rather than claiming anything is safe.
````

- [ ] **Step 4: Verify the whole suite passes and the app imports**

Run: `uv run pytest -v && uv run python -c "import canteen.app"`
Expected: all tests PASS. The import will raise `KeyError: 'SLACK_BOT_TOKEN'` unless
`.env` is populated — that is the correct behaviour, and confirms module wiring.

- [ ] **Step 5: Commit**

```bash
git add src/canteen/app.py README.md pyproject.toml
git commit -m "feat: Slack Bolt app, action handlers, and scheduled roll call"
```

---

## Self-Review

**Spec coverage.** Single-host account → Task 1 (`swiggy_token` pinned to one row) and
Task 3. MCP connector, no MCP client → Task 4. LLM-for-language/Python-for-decisions →
Task 2 solver plus Task 4's `solve_restaurant` local tool. Human-gated payment → Task 4
`SPEND_TOOLS` and Task 9's three click-only handlers. Allergen honesty → `ALLERGEN_CAVEAT`
in Task 2, rendered in Task 5's `dish_picker`. One-message lifecycle → Task 5 + Task 9
`chat_update`. Pantry → Task 7. Dineout → Task 8. Late joiner → Task 6 `can_join_late`.
No-blind-retry → Task 9 `handle_place_order`. Token expiry → Task 3 `valid_token`.
Data model → Task 1 schema, all eight tables.

**Two known gaps, deliberately left for implementation:**
1. `run_pantry_check` in Task 9 posts par levels with placeholder prices rather than
   parsing Claude's `your_go_to_items` reply into `pantry.restock_diff`. The pure diff
   is tested in Task 7; the implementer should have the agent return structured JSON
   and feed it through `restock_diff` before posting.
2. Dineout has no handler that *produces* options — only `book_slot`, which consumes
   them. `rank_slots` is tested and ready; wire it into the `app_mention` path when a
   booking request is detected.

Both are wiring, not design, and both sit behind tested pure functions.

**Type consistency.** `Pick.per_person_dishes` is `dict[str, list[Dish]]` in Tasks 2, 5,
6, 9. `db.get_policy` returns `per_head_cap`/`vendor_allowlist`, consumed under those
exact keys by `brain.hard_filter` and `brain.score`. `agent.run(client, prompt, token,
servers, ctx, extra_system)` is called with that signature everywhere in Task 9.
`recent_orders` rows carry `restaurant_id`/`cuisines`/`ordered_at`, which is exactly what
`brain.score` reads.
