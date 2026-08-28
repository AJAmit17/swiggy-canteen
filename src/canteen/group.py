"""The three things a channel does: group food order, table booking, pantry.

A DM spends your money. A channel spends the money of whoever started the flow,
and only after they click a button. Joiners add dishes to the starter's real
Swiggy cart — there is no local cart, because Swiggy owns it.
"""

from __future__ import annotations

import json
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

    def start(channel_id: str, user_id: str, text: str, kind: str) -> None:
        if kind == FOOD:
            posted = app.client.chat_postMessage(
                channel=channel_id, text="Group lunch",
                blocks=blocks.group_food(user_id, None, [], 0, [user_id]))
            store.save_group(db(), channel_id, FOOD, user_id, posted["ts"],
                             {"joined": [user_id]}, time.time())
            return

        if kind == TABLE:
            finish, _ = progress(channel_id)
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

        finish, _ = progress(channel_id)
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

    @app.event("message")
    def handle_thread_reply(body, client):
        """A reply, with no @mention, inside a thread the assistant already
        started. Slack only ever tells us about a plain mention via
        app_mention — everything else here is a generic message event, so
        this is the only way to keep a conversation going without making
        someone re-mention the bot every single turn.
        """
        event = body["event"]
        if event.get("channel_type") == "im" or event.get("bot_id") or event.get("subtype"):
            return  # DMs are app.py's job; ignore edits/deletes/bot echoes

        thread_ts = event.get("thread_ts")
        if not thread_ts:
            return  # a fresh top-level message, not a reply

        from canteen.app import bot_user_id, respond
        text = (event.get("text") or "").strip()
        if not text or f"<@{bot_user_id(client)}>" in text:
            return  # a real mention here already fired app_mention above

        channel_id = event["channel"]
        if not store.is_bot_thread(db(), channel_id, thread_ts):
            return  # a thread we're not part of — not ours to answer

        respond(channel_id, event["user"], text, thread_ts=thread_ts)

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
        store.clear_history(db(), channel_id)
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
                  "`@Swiggy add a masala dosa` — I'll put it in the shared cart."))

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
                    extra_system=agent.SYSTEM + "\n" + agent.AUTHORISED,
                    allow_spend=True)
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
        ack()
        channel_id = body["channel"]["id"]
        from canteen.app import PROPOSALS, action_thread, conv_key
        thread_ts = action_thread(channel_id, body["message"])
        proposal = json.loads(body["actions"][0]["value"])
        PROPOSALS[conv_key(channel_id, thread_ts)] = {"service": "dineout", **proposal}
        client.chat_postMessage(channel=channel_id, thread_ts=thread_ts,
                                text="Confirm this booking",
                                blocks=blocks.confirm_booking(proposal))
