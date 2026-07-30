"""app.py builds a Bolt App at import, so the environment is set up first."""

import os
import tempfile

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test")
os.environ.setdefault("ANTHROPIC_API_KEY", "anthropic-test")
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


def test_converse_stores_the_full_message_history_for_the_next_turn(monkeypatch):
    history = [{"role": "user", "content": "hello"},
               {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}]
    monkeypatch.setattr(agent, "run", lambda *a, **k: ("**hi**", history))
    store.save_token(app.db(), USER, "acc", "ref", 9e9)

    reply = app.converse(CHANNEL, USER, "hello", servers=["food"])

    assert reply == "*hi*"  # mrkdwn conversion happened on the way out
    assert store.get_history(app.db(), CHANNEL) == history


def test_converse_passes_the_stored_history_back(monkeypatch):
    seen = {}

    def fake_run(client, **kw):
        seen.update(kw)
        return "ok", []

    monkeypatch.setattr(agent, "run", fake_run)
    store.save_token(app.db(), USER, "acc", "ref", 9e9)
    prior = [{"role": "user", "content": "hi"}]
    store.set_history(app.db(), CHANNEL, prior, 0.0)

    app.converse(CHANNEL, USER, "and then?", servers=["food"])

    assert seen["history"] == prior


def test_converse_injects_the_persons_preference(monkeypatch):
    seen = {}

    def fake_run(client, **kw):
        seen.update(kw)
        return "ok", []

    monkeypatch.setattr(agent, "run", fake_run)
    store.save_token(app.db(), USER, "acc", "ref", 9e9)
    store.set_preference(app.db(), USER, "jain")

    app.converse(CHANNEL, USER, "lunch?", servers=["food"])

    assert "jain" in seen["extra_system"]


def test_an_unconnected_person_gets_NotConnected_rather_than_a_crash():
    import pytest
    with pytest.raises(auth.NotConnected):
        app.token_for("U-NOBODY")
