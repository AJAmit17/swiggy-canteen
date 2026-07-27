"""Integration checks for the bridge functions in app.py.

app.py builds a Bolt App at import, so the environment is set up first.
CANTEEN_VERIFY_SLACK=0 skips Bolt's live auth.test call.
"""

import os
import tempfile
import threading

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test")
os.environ.setdefault("GEMINI_API_KEY", "gemini-test")
os.environ.setdefault("CANTEEN_VERIFY_SLACK", "0")
os.environ.setdefault(
    "CANTEEN_DB", os.path.join(tempfile.mkdtemp(), "wiring.db")
)

from canteen import app, db, lunch  # noqa: E402

CHANNEL = "C-TEST"

MENU = [
    {"name": "Dal", "price": 140, "veg": True},
    {"name": "Roti", "price": 40, "veg": True},
    {"name": "Chicken Tikka", "price": 320, "veg": False},
    {"name": "Jain Thali", "price": 200, "veg": True, "jain": True},
]

CANDIDATES = [
    {"id": "r1", "name": "Sattvik", "cuisines": ["south"], "eta_minutes": 20,
     "is_open": True, "deliverable": True, "dishes": MENU},
    {"id": "r2", "name": "Grill House", "cuisines": ["north"], "eta_minutes": 25,
     "is_open": True, "deliverable": True,
     "dishes": [{"name": "Chicken Tikka", "price": 320, "veg": False},
                {"name": "Mutton Seekh", "price": 380, "veg": False}]},
]


def setup_function():
    lunch.STORE.clear()
    app.PANTRY_DRAFT.clear()
    app.DINEOUT_DRAFT.clear()
    for table in ("user_profile", "policy", "team_order", "rating", "par_level"):
        app.store().execute(f"delete from {table}")
    app.store().commit()


def test_solve_tool_excludes_the_restaurant_a_jain_teammate_cannot_eat_at():
    db.upsert_profile(app.store(), "U1", "jain", [], None)
    db.upsert_profile(app.store(), "U2", "nonveg", [], None)
    state = lunch.open_lunch(CHANNEL, "1.1")
    lunch.join(state, "U1")
    lunch.join(state, "U2")

    result = app._solve_for(CHANNEL, CANDIDATES)

    # Grill House has zero jain dishes, so it cannot win.
    assert result["ok"] is False
    assert "jain" in result["reason"].lower()


def test_solve_tool_picks_and_advances_the_state_machine():
    db.upsert_profile(app.store(), "U1", "veg", [], None)
    state = lunch.open_lunch(CHANNEL, "1.1")
    lunch.join(state, "U1")

    result = app._solve_for(CHANNEL, CANDIDATES)

    assert result["ok"] is True
    assert result["restaurant_id"] == "r1"
    assert state.stage == lunch.PICKED
    # The veg teammate is offered only veg dishes.
    assert all(d.veg for d in state.pick.per_person_dishes["U1"])


def test_solve_tool_respects_a_channel_budget_cap():
    db.upsert_profile(app.store(), "U1", "nonveg", [], None)
    db.upsert_policy(app.store(), CHANNEL, 150, [])
    state = lunch.open_lunch(CHANNEL, "1.1")
    lunch.join(state, "U1")

    result = app._solve_for(CHANNEL, CANDIDATES)

    # Both are over cap, but Sattvik's median is far closer to it.
    assert result["restaurant_id"] == "r1"


def test_solve_tool_reports_a_reason_when_nothing_survives():
    db.upsert_profile(app.store(), "U1", "jain", [], None)
    state = lunch.open_lunch(CHANNEL, "1.1")
    lunch.join(state, "U1")
    closed = [{**CANDIDATES[0], "is_open": False}]

    result = app._solve_for(CHANNEL, closed)

    assert result["ok"] is False
    assert "closed" in result["reason"].lower()


def test_pantry_tool_diffs_against_par_and_stores_the_draft():
    db.set_par_level(app.store(), "p1", "Milk 1L", 6)
    db.set_par_level(app.store(), "p2", "Coffee 200g", 2)

    result = app._record_pantry_items(CHANNEL, [
        {"product_id": "p1", "name": "Milk 1L", "price": 60},
        {"product_id": "p9", "name": "Chocolate", "price": 100},
    ])

    assert result["restock_count"] == 1  # p2 not offered, p9 has no par level
    assert result["total"] == 360
    assert app.PANTRY_DRAFT[CHANNEL][0]["product_id"] == "p1"


def test_dineout_tool_ranks_and_stores_the_draft():
    result = app._record_dineout_slots(CHANNEL, [{
        "id": "r1", "name": "Toit", "rating": 4.5,
        "slots": [
            {"slot_id": "s1", "hour": 18, "time": "18:00", "capacity": 10, "is_free": True},
            {"slot_id": "s2", "hour": 20, "time": "20:00", "capacity": 10, "is_free": True},
            {"slot_id": "s3", "hour": 20, "time": "20:00", "capacity": 2, "is_free": True},
        ],
    }], party_size=8, preferred_hour=20)

    assert [o["slot_id"] for o in result["options"]] == ["s2", "s1"]
    assert app.DINEOUT_DRAFT[CHANNEL] == result["options"]


def test_db_work_succeeds_from_a_pool_thread_like_bolt_uses():
    """Regression: Bolt dispatches every listener on a worker thread, and a
    module-level sqlite3 connection raises ProgrammingError there."""
    db.upsert_profile(app.store(), "U1", "veg", [], None)
    state = lunch.open_lunch(CHANNEL, "1.1")
    lunch.join(state, "U1")

    outcome = {}

    def worker():
        try:
            outcome["result"] = app._solve_for(CHANNEL, CANDIDATES)
        except Exception as exc:  # the bug this test exists for
            outcome["error"] = exc

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert "error" not in outcome, outcome.get("error")
    assert outcome["result"]["restaurant_id"] == "r1"


def test_every_local_tool_claude_can_call_is_actually_dispatchable():
    ctx = app.local_ctx(CHANNEL)
    declared = {t["name"] for t in app.agent.LOCAL_TOOLS}
    assert declared == set(ctx), declared.symmetric_difference(set(ctx))


class FakeClient:
    """Records what the handler would have sent to Slack."""

    def __init__(self):
        self.sent = []

    def chat_postMessage(self, **kw):
        self.sent.append(kw)
        return {"ts": "1.1"}


def test_admin_reply_leaves_plain_language_alone():
    """Returning None is what routes a message to the model instead."""
    for text in ("what's good for lunch?", "book a table for six",
                 "setup", "policy loads", "par"):
        assert app.admin_reply(text, CHANNEL, "U1", FakeClient()) is None, text


def test_admin_reply_handles_the_setup_commands():
    client = FakeClient()
    assert "Office saved" in app.admin_reply("setup addr-1", CHANNEL, "U1", client)
    assert db.get_office(app.store(), CHANNEL)["address_id"] == "addr-1"
    assert "250" in app.admin_reply("policy 250", CHANNEL, "U1", client)
    assert db.get_policy(app.store(), CHANNEL)["per_head_cap"] == 250


def test_an_empty_mention_gets_help():
    assert app.admin_reply("", CHANNEL, "U1", FakeClient()) == app.HELP


def test_the_bot_handle_is_stripped_before_the_command_is_read():
    assert app.MENTION.sub("", "<@U09CANTEEN> setup addr-1").strip() == "setup addr-1"


def test_save_profile_keeps_what_the_new_line_did_not_mention():
    """Reported bug: any later DM reset diet to nonveg and dropped the
    blocklist. A new line should add to a profile, not replace it."""
    db.upsert_profile(app.store(), "U1", "veg", ["mushroom"], 250)

    app.save_profile("U1", "no paneer")

    saved = db.get_profile(app.store(), "U1")
    assert saved["diet"] == "veg"                       # not reset
    assert saved["blocklist"] == ["mushroom", "paneer"]  # added, not replaced
    assert saved["budget"] == 250                        # kept


def test_save_profile_states_the_diet_it_stored():
    db.upsert_profile(app.store(), "U1", "veg", [], None)
    assert "*veg*" in app.save_profile("U1", "no okra")


def test_a_new_person_who_only_names_a_dislike_gets_the_permissive_default():
    assert "nonveg" in app.save_profile("U-NEW", "no okra")
    assert db.get_profile(app.store(), "U-NEW")["blocklist"] == ["okra"]
