"""Slack Bolt app in Socket Mode.

Socket Mode means no public URL and no ngrok — the process dials out to Slack.
Every stage of a lunch edits the same message via chat_update.

The three tools that move money (place_food_order, checkout, book_table) are
only ever reached from a handler behind a button click. The scheduled and
conversational paths assemble carts and stop.
"""

from __future__ import annotations

import datetime as dt
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
from canteen.brain import Participant, Rejection, eatable_dishes, solve
from canteen.parsing import close_time, parse_profile, to_candidates

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("canteen")

VETO_SECONDS = 300
ROLL_CALL_WINDOW_MINUTES = 30
REPEAT_WINDOW_SECONDS = 14 * 86400
DEFAULT_TZ = "Asia/Kolkata"
DEFAULT_ROLL_CALL = "11:30"

# Bolt calls auth.test at construction, which needs the network and a real token.
# CANTEEN_VERIFY_SLACK=0 skips it so the module can be imported in CI.
app = App(
    token=os.environ["SLACK_BOT_TOKEN"],
    token_verification_enabled=os.environ.get("CANTEEN_VERIFY_SLACK", "1") == "1",
)
claude = anthropic.Anthropic()
http = httpx.Client(timeout=30)  # httpx.Client is thread-safe; sqlite3 is not
db.init_schema(db.connect())

CLIENT_ID = os.environ.get("SWIGGY_CLIENT_ID", "")


def store():
    """The DB handle for whichever thread is asking.

    Bolt dispatches listeners on a pool and APScheduler uses its own thread, so
    this must never be hoisted into a module-level variable.
    """
    return db.connect()


# Per-channel scratch space for the agent's structured tool reports.
PANTRY_DRAFT: dict[str, list[dict]] = {}
DINEOUT_DRAFT: dict[str, list[dict]] = {}


def token() -> str:
    return swiggy_auth.valid_token(store(), http, CLIENT_ID)


@app.error
def handle_uncaught(error, body, logger):
    """Without this, a failed listener is silent in Slack and only visible in
    the server log — the user is left staring at a message that never updates."""
    logger.exception("listener failed: %s", error)
    channel_id = (body or {}).get("channel_id") or (
        (body or {}).get("channel") or {}
    ).get("id")
    if not channel_id:
        return
    if isinstance(error, swiggy_auth.NotAuthenticated):
        text = ("No Swiggy account is linked yet. An admin needs to run the login "
                "flow — see the README.")
    else:
        text = f"That didn't work: `{error}`. Nothing was ordered."
    try:
        app.client.chat_postMessage(channel=channel_id, text=text)
    except Exception:
        logger.exception("could not report the error back to Slack")


def _participants(user_ids: list[str]) -> list[Participant]:
    return [
        Participant(p["user_id"], p["diet"], p["blocklist"])
        for p in db.get_profiles(store(), user_ids)
    ]


def _solve_for(channel_id: str, candidates: list[dict]) -> dict:
    """The `solve_restaurant` local tool. Claude gathers, this decides."""
    state = lunch.STORE.get(channel_id)
    people = _participants(state.participants if state else [])
    now = time.time()
    result = solve(
        to_candidates(candidates),
        people,
        db.get_policy(store(), channel_id),
        db.restaurant_ratings(store()),
        db.recent_orders(store(), channel_id, now - REPEAT_WINDOW_SECONDS),
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
        "runner_up": result.runner_up.name if result.runner_up else None,
    }


def _record_pantry_items(channel_id: str, items: list[dict]) -> dict:
    """The `record_pantry_items` local tool. Claude fetches, this diffs."""
    needed = pantry.restock_diff(items, db.par_levels(store()), {})
    PANTRY_DRAFT[channel_id] = needed
    return {"ok": True, "restock_count": len(needed),
            "total": pantry.restock_total(needed)}


def _record_dineout_slots(channel_id: str, restaurants: list[dict],
                          party_size: int, preferred_hour: int) -> dict:
    """The `record_dineout_slots` local tool. Claude fetches, this ranks."""
    options = dineout.rank_slots(restaurants, party_size, preferred_hour,
                                 db.restaurant_ratings(store()))
    DINEOUT_DRAFT[channel_id] = options
    return {"ok": True, "options": options}


def local_ctx(channel_id: str) -> dict:
    """The local tools Claude may call, bound to this channel."""
    return {
        "solve_restaurant": lambda candidates: _solve_for(channel_id, candidates),
        "get_policy": lambda: db.get_policy(store(), channel_id),
        "record_rating": lambda restaurant_id, score: (
            db.record_rating(store(), "agent", restaurant_id, score) or "recorded"
        ),
        "log_spend": lambda user_id, order_id, amount: (
            db.record_spend(store(), user_id, order_id, amount) or "recorded"
        ),
        "record_pantry_items": lambda items: _record_pantry_items(channel_id, items),
        "record_dineout_slots": lambda restaurants, party_size, preferred_hour: (
            _record_dineout_slots(channel_id, restaurants, party_size, preferred_hour)
        ),
    }


def ask(channel_id: str, prompt: str, servers: list[str],
        extra_system: str | None = None) -> str:
    return agent.run(claude, prompt=prompt, token=token(), servers=servers,
                     ctx=local_ctx(channel_id), extra_system=extra_system)


# ---------------------------------------------------------------- lunch flow

def start_roll_call(channel_id: str) -> None:
    office = db.get_office(store(), channel_id) or {}
    deadline = close_time(office.get("roll_call_time", DEFAULT_ROLL_CALL),
                          ROLL_CALL_WINDOW_MINUTES)
    posted = app.client.chat_postMessage(
        channel=channel_id, blocks=blocks.roll_call(deadline), text="Lunch?"
    )
    lunch.open_lunch(channel_id, posted["ts"])


def close_roll_call(channel_id: str) -> None:
    """Claude gathers candidates; solve_restaurant makes the actual choice."""
    state = lunch.STORE.get(channel_id)
    if not state or not state.participants:
        return
    office = db.get_office(store(), channel_id)
    if not office:
        app.client.chat_postMessage(
            channel=channel_id,
            text="No office address set for this channel. Run `/canteen setup`.",
        )
        return

    reply = ask(
        channel_id,
        f"{len(state.participants)} people want lunch delivered to Swiggy address "
        f"{office['address_id']}. Search restaurants there, fetch the menu for the "
        "top 8, and tag every dish veg/contains_egg/jain from the menu data. Then "
        "call solve_restaurant with all of them. Report only what it returned.",
        servers=["food"],
    )

    if not state.pick:
        app.client.chat_update(channel=channel_id, ts=state.message_ts,
                               text="No lunch today", blocks=blocks.rejection(reply))
        return

    app.client.chat_update(
        channel=channel_id, ts=state.message_ts, text="Lunch",
        blocks=blocks.pick_message(state.pick, state.participants, VETO_SECONDS),
    )
    _send_dish_pickers(channel_id, state)


def _send_dish_pickers(channel_id: str, state) -> None:
    for user_id in state.participants:
        app.client.chat_postEphemeral(
            channel=channel_id, user=user_id, text="Pick your dish",
            blocks=blocks.dish_picker(state.pick.per_person_dishes.get(user_id, [])),
        )


@app.action("join_lunch")
def handle_join(ack, body, client):
    ack()
    channel_id = body["channel"]["id"]
    user_id = body["user"]["id"]
    state = lunch.STORE.get(channel_id)
    if not state:
        return

    if state.pick:  # late joiner — the restaurant is already fixed
        person = _participants([user_id])[0]
        if not lunch.can_join_late(person, state.pick.candidate):
            client.chat_postEphemeral(
                channel=channel_id, user=user_id,
                text=(f"We're already locked into {state.pick.candidate.name} and "
                      "there isn't enough there for you — sitting this one out."),
            )
            return

    if not lunch.join(state, user_id):
        return

    if state.pick:
        person = _participants([user_id])[0]
        state.pick.per_person_dishes[user_id] = eatable_dishes(
            person, state.pick.candidate
        )
        client.chat_postEphemeral(
            channel=channel_id, user=user_id, text="Pick your dish",
            blocks=blocks.dish_picker(state.pick.per_person_dishes[user_id]),
        )

    client.chat_update(
        channel=channel_id, ts=state.message_ts, text="Lunch",
        blocks=(blocks.pick_message(state.pick, state.participants, VETO_SECONDS)
                if state.pick
                else blocks.roll_call(
                    close_time(DEFAULT_ROLL_CALL, ROLL_CALL_WINDOW_MINUTES))),
    )


@app.action("veto_pick")
def handle_veto(ack, body, client):
    ack()
    channel_id = body["channel"]["id"]
    state = lunch.STORE.get(channel_id)
    if not state:
        return
    lunch.veto(state, _participants(state.participants))
    client.chat_update(
        channel=channel_id, ts=state.message_ts, text="Lunch",
        blocks=blocks.pick_message(state.pick, state.participants, 0),
    )
    _send_dish_pickers(channel_id, state)


@app.action("choose_dish")
def handle_choose_dish(ack, body, client):
    ack()
    channel_id = body["channel"]["id"]
    state = lunch.STORE.get(channel_id)
    if not state or not state.pick:
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
    if not state or not state.pick or not state.cart:
        return

    items = ", ".join(i["name"] for i in state.cart.values())
    try:
        reply = ask(
            channel_id,
            f"Add these to the Swiggy cart at restaurant {state.pick.candidate.id} "
            f"({state.pick.candidate.name}): {items}. Fetch coupons, apply the best "
            "one, then place the order. Report the order id and the final total.",
            servers=["food"],
            extra_system=agent.AUTHORISED,
        )
    except Exception as exc:
        log.exception("order call failed")
        # Never blind-retry: the order may have landed before the failure.
        status = ask(
            channel_id,
            "List my most recent Swiggy food order with its id and status. "
            "Do not place anything.",
            servers=["food"],
        )
        client.chat_postMessage(
            channel=channel_id,
            text=(f"The order call failed (`{exc}`). I did *not* retry — that risks "
                  f"double-ordering. Latest order on the account:\n{status}"),
        )
        return

    lunch.mark_placed(state, "placed")
    db.record_order(store(), channel_id, state.pick.candidate.id,
                    state.pick.candidate.name, state.pick.candidate.cuisines,
                    state.participants, lunch.cart_total(state), time.time())
    for user_id, item in state.cart.items():
        db.record_spend(store(), user_id, state.order_id or "placed", item["price"])

    client.chat_update(channel=channel_id, ts=state.message_ts, text="Ordered",
                       blocks=blocks.tracking(state.pick.candidate.name, reply, "—"))
    client.chat_postMessage(
        channel=channel_id, text="Rate it",
        blocks=blocks.rate_prompt(state.pick.candidate.id, state.pick.candidate.name),
    )


@app.action("cancel_lunch")
def handle_cancel(ack, body, client):
    ack()
    channel_id = body["channel"]["id"]
    lunch.STORE.pop(channel_id, None)
    client.chat_update(channel=channel_id, ts=body["message"]["ts"],
                       text="Lunch cancelled.", blocks=[])


def _register_rating(score: int):
    @app.action(f"rate_{score}")
    def handler(ack, body, client, _score=score):
        ack()
        db.record_rating(store(), body["user"]["id"], body["actions"][0]["value"], _score)
        client.chat_postEphemeral(channel=body["channel"]["id"],
                                  user=body["user"]["id"],
                                  text="Noted — that shapes the next pick.")
    return handler


for _n in range(1, 6):
    _register_rating(_n)


# -------------------------------------------------------------------- pantry

def run_pantry_check(channel_id: str) -> None:
    office = db.get_office(store(), channel_id)
    par = db.par_levels(store())
    if not office or not par:
        return
    ask(
        channel_id,
        f"For Instamart delivery address {office['address_id']}, call "
        "your_go_to_items and then report every item through record_pantry_items "
        "with its product_id, name and unit price in whole rupees.",
        servers=["im"],
    )
    needed = PANTRY_DRAFT.get(channel_id) or []
    if not needed:
        log.info("pantry: nothing below par in %s", channel_id)
        return
    app.client.chat_postMessage(
        channel=channel_id, text="Pantry restock",
        blocks=blocks.pantry_approval(needed, pantry.restock_total(needed)),
    )


@app.action("approve_pantry")
def handle_approve_pantry(ack, body, client):
    """The only path to Instamart checkout. A human clicked this."""
    ack()
    channel_id = body["channel"]["id"]
    needed = PANTRY_DRAFT.get(channel_id) or []
    if not needed:
        client.chat_postMessage(channel=channel_id,
                                text="That restock list has expired — re-run the check.")
        return
    lines = ", ".join(f"{i['name']} x{i['qty']}" for i in needed)
    try:
        reply = ask(
            channel_id,
            f"Update the Instamart cart to exactly these items: {lines}. Then check "
            "out and report the order id and total.",
            servers=["im"], extra_system=agent.AUTHORISED,
        )
    except Exception as exc:
        log.exception("pantry checkout failed")
        status = ask(channel_id,
                     "List my most recent Instamart order with its id and status. "
                     "Do not check anything out.", servers=["im"])
        client.chat_postMessage(
            channel=channel_id,
            text=(f"Checkout failed (`{exc}`). I did *not* retry. Latest order:\n{status}"),
        )
        return
    PANTRY_DRAFT.pop(channel_id, None)
    client.chat_postMessage(channel=channel_id, text=reply)


@app.action("skip_pantry")
def handle_skip_pantry(ack, body, client):
    ack()
    PANTRY_DRAFT.pop(body["channel"]["id"], None)
    client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"],
                       text="Skipped this week.", blocks=[])


# ------------------------------------------------------------------- dineout

def propose_tables(channel_id: str, request_text: str) -> None:
    ask(
        channel_id,
        f"A team wants a restaurant table. Request: {request_text!r}. Use "
        "search_restaurants_dineout and get_available_slots for the top candidates, "
        "then report everything through record_dineout_slots — each restaurant with "
        "id, name, rating and a slots array of slot_id, hour, time, capacity, "
        "is_free. Infer party_size and preferred_hour from the request.",
        servers=["dineout"],
    )
    options = DINEOUT_DRAFT.get(channel_id) or []
    if not options:
        app.client.chat_postMessage(
            channel=channel_id,
            text="No free tables matched that party size and time.")
        return
    app.client.chat_postMessage(channel=channel_id, text="Table options",
                                blocks=blocks.dineout_options(options))


@app.action("book_slot")
def handle_book_slot(ack, body, client):
    """The only path to book_table. A human clicked this."""
    ack()
    channel_id = body["channel"]["id"]
    restaurant_id, slot_id = body["actions"][0]["value"].split("|", 1)
    try:
        reply = ask(
            channel_id,
            f"Create a dineout cart for restaurant {restaurant_id} slot {slot_id}, "
            "book the table, then report the booking status.",
            servers=["dineout"], extra_system=agent.AUTHORISED,
        )
    except Exception as exc:
        log.exception("booking failed")
        client.chat_postMessage(
            channel=channel_id,
            text=f"Booking failed (`{exc}`). I did not retry — check Dineout directly.")
        return
    DINEOUT_DRAFT.pop(channel_id, None)
    client.chat_postMessage(channel=channel_id, text=reply)


# ------------------------------------------------------- setup and onboarding

@app.command("/canteen")
def handle_command(ack, body, client, respond):
    ack()
    parts = (body.get("text") or "").split()
    sub = parts[0] if parts else "help"
    channel_id = body["channel_id"]

    if sub == "setup" and len(parts) >= 2:
        db.upsert_office(store(), channel_id, parts[1],
                         parts[2] if len(parts) > 2 else DEFAULT_TZ,
                         parts[3] if len(parts) > 3 else DEFAULT_ROLL_CALL)
        respond("Office saved. Restart me to pick up the new roll-call schedule.")
    elif sub == "policy" and len(parts) >= 2:
        db.upsert_policy(store(), channel_id, int(parts[1]), parts[2:])
        respond(f"Per-head cap ₹{parts[1]}. Allowlist: {parts[2:] or 'any vendor'}.")
    elif sub == "par" and len(parts) >= 4:
        db.set_par_level(store(), parts[1], " ".join(parts[2:-1]), int(parts[-1]))
        respond("Par level saved.")
    elif sub == "now":
        start_roll_call(channel_id)
        respond("Roll call open — tap in.")
    elif sub == "close":
        close_roll_call(channel_id)
    elif sub == "me":
        _start_onboarding(body["user_id"], client)
        respond("Sent you a DM.")
    elif sub == "addresses":
        respond(ask(channel_id, "List my saved Swiggy delivery addresses with ids.",
                    servers=["food"]))
    else:
        respond(
            "`/canteen setup <address_id> [tz] [HH:MM]` — link this channel to an office\n"
            "`/canteen policy <per_head_cap> [restaurant_id ...]` — spending policy\n"
            "`/canteen par <product_id> <name> <qty>` — pantry target quantity\n"
            "`/canteen now` — open a roll call · `/canteen close` — order now\n"
            "`/canteen me` — set your diet · `/canteen addresses` — list Swiggy addresses"
        )


ONBOARD_PROMPT = (
    "Hi — I run lunch for the team.\n\n"
    "Reply here with one line so I never pick something you can't eat, like:\n"
    "`veg, no mushroom, 250` — diet, things to avoid, your usual per-meal budget.\n\n"
    "Diet can be `veg`, `jain`, `egg` or `nonveg`."
)


def _start_onboarding(user_id: str, client) -> None:
    client.chat_postMessage(channel=user_id, text=ONBOARD_PROMPT)


@app.event("app_mention")
def handle_mention(body, say):
    channel_id = body["event"]["channel"]
    text = body["event"]["text"]
    low = text.lower()
    if "lunch" in low and ("now" in low or "today" in low):
        start_roll_call(channel_id)
        return
    if any(w in low for w in ("table", "book", "dinner", "reserve")):
        propose_tables(channel_id, text)
        return
    if "pantry" in low or "restock" in low:
        run_pantry_check(channel_id)
        return
    say(ask(channel_id, text, servers=["food", "im", "dineout"]))


@app.event("message")
def handle_dm(body, client, logger):
    """Onboarding replies arrive as DMs. Everything else is ignored."""
    event = body.get("event", {})
    if event.get("channel_type") != "im" or event.get("bot_id"):
        return
    profile = parse_profile(event.get("text", ""))
    db.upsert_profile(store(), event["user"], profile["diet"],
                      profile["blocklist"], profile["budget"])
    avoid = ", ".join(profile["blocklist"]) or "nothing"
    client.chat_postMessage(
        channel=event["channel"],
        text=(f"Got it — {profile['diet']}, avoiding {avoid}. "
              "I'll filter every menu for you from now on."),
    )


# ---------------------------------------------------------------------- main

def _schedule(scheduler: BackgroundScheduler) -> int:
    offices = store().execute("select * from office").fetchall()
    for office in offices:
        channel_id = office["channel_id"]
        tz = office["timezone"]
        hour, minute = (int(x) for x in office["roll_call_time"].split(":"))
        closes = dt.datetime(2000, 1, 1, hour, minute) + dt.timedelta(
            minutes=ROLL_CALL_WINDOW_MINUTES
        )
        scheduler.add_job(start_roll_call, "cron", hour=hour, minute=minute,
                          args=[channel_id], timezone=tz,
                          id=f"rollcall-{channel_id}", replace_existing=True)
        scheduler.add_job(close_roll_call, "cron", hour=closes.hour,
                          minute=closes.minute, args=[channel_id], timezone=tz,
                          id=f"close-{channel_id}", replace_existing=True)
        scheduler.add_job(run_pantry_check, "cron", day_of_week="mon", hour=10,
                          args=[channel_id], timezone=tz,
                          id=f"pantry-{channel_id}", replace_existing=True)
    return len(offices)


def main() -> None:
    scheduler = BackgroundScheduler()
    count = _schedule(scheduler)
    scheduler.start()
    log.info("Canteen up — %d office(s) scheduled. Connecting to Slack…", count)
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()
