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
import re
import time

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from google import genai
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from canteen import agent, blocks, db, dineout, lunch, pantry, swiggy_auth
from canteen.brain import Participant, Rejection, eatable_dishes, solve
from canteen.parsing import (close_time, looks_like_profile, parse_profile,
                             to_candidates)
from canteen.slackfmt import to_mrkdwn

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
gemini = genai.Client()
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
    """Every model reply reaches Slack through here, so mrkdwn conversion does
    too. Models write CommonMark; Slack renders `**bold**` as literal asterisks."""
    return to_mrkdwn(
        agent.run(gemini, prompt=prompt, token=token(), servers=servers,
                  ctx=local_ctx(channel_id), extra_system=extra_system)
    )


THINKING = ":hourglass_flowing_sand: _Working on it…_"


def progress(channel_id: str, thread_ts: str | None = None):
    """Post a placeholder now; return a function that turns it into the answer.

    A Swiggy round-trip takes ten to thirty seconds, and Slack shows nothing at
    all while it runs — the channel just looks broken.
    """
    posted = app.client.chat_postMessage(
        channel=channel_id, text=THINKING, thread_ts=thread_ts
    )

    def finish(text: str, block_kit: list | None = None) -> None:
        app.client.chat_update(channel=channel_id, ts=posted["ts"],
                               text=text, blocks=block_kit or [])

    return finish


def ask_visibly(channel_id: str, prompt: str, servers: list[str],
                thread_ts: str | None = None) -> None:
    """ask(), with the placeholder replaced by the answer — or by the failure.

    The error is reported here rather than re-raised so the placeholder never
    strands on "Working on it…" while the real message goes only to the log.
    """
    finish = progress(channel_id, thread_ts)
    try:
        finish(ask(channel_id, prompt, servers))
    except Exception as exc:
        log.exception("ask failed")
        finish(f":warning: That didn't work: `{exc}`. Nothing was ordered.")


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
    client.chat_update(channel=channel_id, ts=state.message_ts, blocks=[],
                       text=":hourglass_flowing_sand: _Placing the order…_")
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
        client.chat_update(
            channel=channel_id, ts=state.message_ts, blocks=[],
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
    finish = progress(channel_id)
    try:
        ask(
            channel_id,
            f"For Instamart delivery address {office['address_id']}, call "
            "your_go_to_items and then report every item through "
            "record_pantry_items with its product_id, name and unit price in "
            "whole rupees.",
            servers=["im"],
        )
    except Exception as exc:
        log.exception("pantry check failed")
        finish(f":warning: Pantry check failed: `{exc}`. Nothing was ordered.")
        return
    needed = PANTRY_DRAFT.get(channel_id) or []
    if not needed:
        log.info("pantry: nothing below par in %s", channel_id)
        finish("Pantry's fine — everything is at or above par.")
        return
    finish("Pantry restock",
           blocks.pantry_approval(needed, pantry.restock_total(needed)))


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
    finish = progress(channel_id)
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
        finish(f"Checkout failed (`{exc}`). I did *not* retry. Latest order:\n{status}")
        return
    PANTRY_DRAFT.pop(channel_id, None)
    finish(reply)


@app.action("skip_pantry")
def handle_skip_pantry(ack, body, client):
    ack()
    PANTRY_DRAFT.pop(body["channel"]["id"], None)
    client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"],
                       text="Skipped this week.", blocks=[])


# ------------------------------------------------------------------- dineout

def propose_tables(channel_id: str, request_text: str) -> None:
    finish = progress(channel_id)
    try:
        ask(
            channel_id,
            f"A team wants a restaurant table. Request: {request_text!r}. Use "
            "search_restaurants_dineout and get_available_slots for the top "
            "candidates, then report everything through record_dineout_slots — each "
            "restaurant with id, name, rating and a slots array of slot_id, hour, "
            "time, capacity, "
            "is_free. Infer party_size and preferred_hour from the request.",
            servers=["dineout"],
        )
    except Exception as exc:
        log.exception("dineout search failed")
        finish(f":warning: Table search failed: `{exc}`. Nothing was booked.")
        return
    options = DINEOUT_DRAFT.get(channel_id) or []
    if not options:
        finish("No free tables matched that party size and time.")
        return
    finish("Table options", blocks.dineout_options(options))


@app.action("book_slot")
def handle_book_slot(ack, body, client):
    """The only path to book_table. A human clicked this."""
    ack()
    channel_id = body["channel"]["id"]
    restaurant_id, slot_id = body["actions"][0]["value"].split("|", 1)
    finish = progress(channel_id)
    try:
        reply = ask(
            channel_id,
            f"Create a dineout cart for restaurant {restaurant_id} slot {slot_id}, "
            "book the table, then report the booking status.",
            servers=["dineout"], extra_system=agent.AUTHORISED,
        )
    except Exception as exc:
        log.exception("booking failed")
        finish(f"Booking failed (`{exc}`). I did not retry — check Dineout directly.")
        return
    DINEOUT_DRAFT.pop(channel_id, None)
    finish(reply)


# ------------------------------------------------------- setup and onboarding

HELP = (
    "Mention me and say what you want — `@Canteen what's good for lunch?`, "
    "`@Canteen book a table for six at 8pm`, `@Canteen check the pantry`.\n\n"
    "Set-up commands:\n"
    "`@Canteen setup <address_id> [tz] [HH:MM]` — link this channel to an office\n"
    "`@Canteen policy <per_head_cap> [restaurant_id ...]` — spending policy\n"
    "`@Canteen par <product_id> <name> <qty>` — pantry target quantity\n"
    "`@Canteen now` — open a roll call · `@Canteen close` — order now\n"
    "`@Canteen me` — set your diet · `@Canteen addresses` — list Swiggy addresses"
)

ADDRESS_PROMPT = "List my saved Swiggy delivery addresses with their ids."

SKIP_WORDS = {"skip", "none", "nothing", "no restrictions", "anything"}

MENTION = re.compile(r"<@[^>]+>")


def admin_reply(text: str, channel_id: str, user_id: str, client) -> str | None:
    """The set-up commands, shared by `@Canteen` and the legacy slash command.

    Returns the text to post, `""` when the command posted its own message, or
    None when this is not a command at all and should be treated as language.
    """
    parts = text.split()
    sub = parts[0].lower() if parts else "help"

    if sub == "setup" and len(parts) >= 2:
        db.upsert_office(store(), channel_id, parts[1],
                         parts[2] if len(parts) > 2 else DEFAULT_TZ,
                         parts[3] if len(parts) > 3 else DEFAULT_ROLL_CALL)
        return "Office saved. Restart me to pick up the new roll-call schedule."
    if sub == "policy" and len(parts) >= 2 and parts[1].isdigit():
        db.upsert_policy(store(), channel_id, int(parts[1]), parts[2:])
        return f"Per-head cap ₹{parts[1]}. Allowlist: {parts[2:] or 'any vendor'}."
    if sub == "par" and len(parts) >= 4 and parts[-1].isdigit():
        db.set_par_level(store(), parts[1], " ".join(parts[2:-1]), int(parts[-1]))
        return "Par level saved."
    if sub == "now" and len(parts) == 1:
        start_roll_call(channel_id)
        return ""
    if sub == "close" and len(parts) == 1:
        close_roll_call(channel_id)
        return ""
    if sub == "me" and len(parts) == 1:
        _start_onboarding(user_id, client)
        return "Sent you a DM."
    if sub == "help" or not parts:
        return HELP
    return None


@app.event("app_mention")
def handle_mention(body, client):
    """`@Canteen ...` — the main way in. Set-up commands and plain language."""
    event = body["event"]
    channel_id = event["channel"]
    thread_ts = event.get("thread_ts")
    text = MENTION.sub("", event.get("text", "")).strip()

    reply = admin_reply(text, channel_id, event["user"], client)
    if reply is not None:
        if reply:
            client.chat_postMessage(channel=channel_id, text=reply,
                                    thread_ts=thread_ts)
        return

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
    if low.startswith("address"):
        text = ADDRESS_PROMPT
    ask_visibly(channel_id, text, servers=["food", "im", "dineout"],
                thread_ts=thread_ts)


@app.command("/canteen")
def handle_command(ack, body, client, respond):
    """Kept working for anyone with muscle memory; `@Canteen` is the real surface."""
    ack()
    text = (body.get("text") or "").strip()
    channel_id = body["channel_id"]

    reply = admin_reply(text, channel_id, body["user_id"], client)
    if reply is not None:
        if reply:
            respond(reply)
        return

    respond(THINKING)
    prompt = ADDRESS_PROMPT if text.lower().startswith("address") else text
    try:
        respond(text=ask(channel_id, prompt, servers=["food", "im", "dineout"]),
                replace_original=True)
    except Exception as exc:
        log.exception("slash command failed")
        respond(text=f":warning: That didn't work: `{exc}`. Nothing was ordered.",
                replace_original=True)


ONBOARD_PROMPT = (
    "Hi — I run lunch for the team.\n\n"
    "Reply here with one line so I never pick something you can't eat, like:\n"
    "`veg, no mushroom, 250` — diet, things to avoid, your usual per-meal budget.\n\n"
    "Diet can be `veg`, `jain`, `egg` or `nonveg`. "
    "You can also just ask me things here — `what's good for lunch today?`"
)

# Who we have just sent the diet prompt to. Their next line is read as a
# profile even if it is only "no mushroom"; after that they are back to normal
# conversation. Losing this on restart costs one re-prompt, nothing more.
ONBOARDING: set[str] = set()


def _start_onboarding(user_id: str, client) -> None:
    ONBOARDING.add(user_id)
    client.chat_postMessage(channel=user_id, text=ONBOARD_PROMPT)


def save_profile(user_id: str, text: str) -> str:
    """Apply a diet line to a profile, keeping anything it did not mention."""
    parsed = parse_profile(text)
    existing = db.get_profile(store(), user_id) or {}
    diet = parsed["diet"] or existing.get("diet") or db.DEFAULT_DIET
    blocklist = sorted(set(existing.get("blocklist") or []) | set(parsed["blocklist"]))
    budget = parsed["budget"] if parsed["budget"] is not None else existing.get("budget")
    db.upsert_profile(store(), user_id, diet, blocklist, budget)

    avoid = ", ".join(blocklist) or "nothing"
    cap = f", budget ₹{budget}" if budget else ""
    return (f"Got it — *{diet}*, avoiding {avoid}{cap}. "
            "I'll filter every menu for you from now on.")


@app.event("message")
def handle_dm(body, client):
    """DMs are onboarding *and* conversation.

    Reading every message as a diet line is what turned "yes please." into a
    blocked dish and reset the sender to nonveg. A line is only a profile if it
    unambiguously looks like one, or if we just asked for one.
    """
    event = body.get("event", {})
    if (event.get("channel_type") != "im" or event.get("bot_id")
            or event.get("subtype")):
        return

    user_id = event["user"]
    channel_id = event["channel"]
    text = (event.get("text") or "").strip()
    if not text:
        return

    if looks_like_profile(text):
        ONBOARDING.discard(user_id)
        client.chat_postMessage(channel=channel_id, text=save_profile(user_id, text))
        return

    if user_id in ONBOARDING:
        if text.lower().strip(".!") in SKIP_WORDS:
            ONBOARDING.discard(user_id)
            client.chat_postMessage(
                channel=channel_id,
                text=save_profile(user_id, db.DEFAULT_DIET))
        else:
            client.chat_postMessage(
                channel=channel_id,
                text=("I didn't catch a diet in that. One line like "
                      "`veg, no mushroom, 250` — or say `skip` and I'll assume "
                      "you eat anything."),
            )
        return

    # Set-up commands are channel-scoped, so in a DM they would silently
    # configure the DM itself. Only questions belong here.
    if text.lower().strip("?!. ") in ("help", "what can you do"):
        client.chat_postMessage(channel=channel_id, text=HELP)
        return
    prompt = ADDRESS_PROMPT if text.lower().startswith("address") else text
    ask_visibly(channel_id, prompt, servers=["food", "im", "dineout"])


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
