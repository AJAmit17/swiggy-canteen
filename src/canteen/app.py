"""Slack Bolt app in Socket Mode.

Routing is the whole job: a DM is a personal Swiggy assistant on that person's
own account, a mention starts a group flow. Socket Mode means no public URL.

Money moves only from a button handler. Everything else assembles and stops.
"""

from __future__ import annotations

import logging
import os
import time

import anthropic
import httpx
from dotenv import load_dotenv
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
claude = anthropic.Anthropic()
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


_bot_user_id: str | None = None


def bot_user_id(client) -> str:
    """The bot's own user id, fetched once and cached.

    Slack delivers a message that mentions us as *both* an `app_mention` event
    and this plain `message` event — this lets the message handler recognise
    and skip the ones app_mention already handled.
    """
    global _bot_user_id
    if _bot_user_id is None:
        _bot_user_id = client.auth_test()["user_id"]
    return _bot_user_id


def action_thread(channel_id: str, message: dict) -> str | None:
    """The thread a button click's reply belongs in, or None for a DM (which
    never threads its own replies — see conv_key).

    Slack only adds `thread_ts` to a root message once something has actually
    replied to it — until then it's absent even though the message *is* the
    root. `thread_ts or its own ts` is correct either way, so this must never
    be read as a bare `message.get("thread_ts")`.
    """
    if channel_id.startswith("D"):
        return None
    return message.get("thread_ts") or message["ts"]


def conv_key(channel_id: str, thread_ts: str | None) -> str:
    """The key conversation state (history, a pending proposal) is filed
    under. A DM channel id always starts with 'D' (a stable Slack convention)
    and is one continuous conversation regardless of Slack's own thread
    nesting — every DM turn posts a fresh top-level message, so keying by
    thread there would fragment history every turn. A channel can run several
    independent threads at once, so those get their own key each."""
    if channel_id.startswith("D") or not thread_ts:
        return channel_id
    return f"{channel_id}:{thread_ts}"


# ------------------------------------------------------------- local tools

def _propose_purchase(key: str, service: str, total: int, summary: str) -> str:
    blocked = agent.blocked_reason(service, total)
    if blocked:
        return f"Not offering that yet: {blocked}"
    PROPOSALS[key] = {"service": service, "total": total, "summary": summary}
    return "A confirm button has been shown to the user. Stop and wait."


def _propose_booking(key: str, **proposal) -> str:
    PROPOSALS[key] = {"service": "dineout", **proposal}
    return "A confirm button has been shown to the user. Stop and wait."


def _remember_preference(user_id: str, note: str) -> str:
    store.set_preference(db(), user_id, note)
    return "Saved."


def local_ctx(user_id: str, key: str) -> dict:
    """The local tools the model may call, bound to this person and conversation."""
    return {
        "propose_purchase": lambda service, total, summary: _propose_purchase(
            key, service, total, summary),
        "propose_booking": lambda **kw: _propose_booking(key, **kw),
        "remember_preference": lambda note: _remember_preference(user_id, note),
    }


# ------------------------------------------------------------ model access

def converse(channel_id: str, user_id: str, prompt: str, servers: list[str],
             extra_system: str | None = None, allow_spend: bool = False,
             thread_ts: str | None = None) -> str:
    """One turn of conversation, continuing whatever came before in this
    channel (or, inside a channel, this specific thread).

    allow_spend is what actually unlocks the money tools, and only a button
    handler passes it.
    """
    key = conv_key(channel_id, thread_ts)
    instruction = extra_system or agent.system_for(
        store.get_preference(db(), user_id))
    reply, messages = agent.run(
        claude,
        prompt=prompt,
        token=token_for(user_id),
        servers=servers,
        ctx=local_ctx(user_id, key),
        extra_system=instruction,
        allow_spend=allow_spend,
        history=store.get_history(db(), key),
    )
    store.set_history(db(), key, messages, time.time())
    return to_mrkdwn(reply)


def progress(channel_id: str, thread_ts: str | None = None):
    """Post a placeholder now; return a function that turns it into the answer,
    plus the thread root this turn's conversation state should be filed under.

    A Swiggy round trip takes ten to thirty seconds and Slack shows nothing at
    all meanwhile — the channel just looks broken.
    """
    posted = app.client.chat_postMessage(channel=channel_id, text=THINKING,
                                         thread_ts=thread_ts)
    # Whatever we just posted becomes a thread others can reply into without
    # re-mentioning us — a fresh top-level post is its own future thread root.
    root_ts = thread_ts or posted["ts"]
    store.mark_bot_thread(db(), channel_id, root_ts, time.time())

    def finish(text: str, block_kit: list | None = None) -> None:
        app.client.chat_update(channel=channel_id, ts=posted["ts"], text=text,
                               blocks=block_kit or [])

    return finish, root_ts


def respond(channel_id: str, user_id: str, prompt: str,
            servers: list[str] | None = None,
            thread_ts: str | None = None) -> None:
    """Converse, showing progress, and render any proposal the model produced."""
    finish, root_ts = progress(channel_id, thread_ts)
    try:
        reply = converse(channel_id, user_id, prompt, servers or SERVERS_ALL,
                         thread_ts=root_ts)
    except auth.NotConnected:
        finish("Connect your Swiggy account first.",
               blocks.connect_prompt(auth.begin_link(db(), user_id, time.time())))
        return
    except Exception as exc:
        log.exception("conversation failed")
        finish(f":warning: That didn't work: `{exc}`. Nothing was ordered.")
        return

    proposal = PROPOSALS.get(conv_key(channel_id, root_ts))
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
            # The pending record is consumed either way, so hand them a fresh
            # link rather than leaving them with a dead end.
            client.chat_postMessage(
                channel=channel_id, text=f":warning: {exc} Here's a fresh link.",
                blocks=blocks.connect_prompt(
                    auth.begin_link(db(), user_id, time.time())))
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
        store.clear_history(db(), channel_id)
        PROPOSALS.pop(channel_id, None)
        client.chat_postMessage(channel=channel_id, text="Fresh start. Go ahead.")
        return

    respond(channel_id, user_id, text)


# ----------------------------------------------------------- spend handlers

@app.action("cancel_purchase")
def handle_cancel_purchase(ack, body, client):
    ack()
    channel_id = body["channel"]["id"]
    thread_ts = action_thread(channel_id, body["message"])
    PROPOSALS.pop(conv_key(channel_id, thread_ts), None)
    client.chat_update(channel=channel_id, ts=body["message"]["ts"],
                       text="Cancelled. Nothing was ordered.", blocks=[])


@app.action("confirm_purchase")
def handle_confirm_purchase(ack, body, client):
    """The only path to place_food_order and checkout. A human clicked this."""
    ack()
    channel_id = body["channel"]["id"]
    user_id = body["user"]["id"]
    thread_ts = action_thread(channel_id, body["message"])
    proposal = PROPOSALS.pop(conv_key(channel_id, thread_ts), None)
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
            allow_spend=True,
            thread_ts=thread_ts,
        )
    except Exception as exc:
        log.exception("order failed")
        # Not idempotent: the order may have landed before the failure, so
        # never retry — look first.
        status = converse(
            channel_id, user_id,
            f"Call {recent} and report my most recent order and its status. "
            "Do not order anything.",
            servers=servers, thread_ts=thread_ts)
        client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts,
            text=(f"The order call failed (`{exc}`). I did *not* retry — that "
                  f"risks ordering twice. Latest on your account:\n{status}"))
        return

    client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=reply)


@app.action("confirm_booking")
def handle_confirm_booking(ack, body, client):
    """The only path to book_table. A human clicked this."""
    ack()
    channel_id = body["channel"]["id"]
    user_id = body["user"]["id"]
    thread_ts = action_thread(channel_id, body["message"])
    proposal = PROPOSALS.pop(conv_key(channel_id, thread_ts), None)
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
            allow_spend=True,
            thread_ts=thread_ts,
        )
    except Exception as exc:
        log.exception("booking failed")
        status = converse(
            channel_id, user_id,
            f"Check get_booking_status for restaurant "
            f"{proposal['restaurant_id']} slot {proposal['slot_id']} and report "
            "what you find. Do not book anything.",
            servers=["dineout"], thread_ts=thread_ts)
        client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts,
            text=(f"The booking call failed (`{exc}`). I did *not* retry. "
                  f"What Swiggy shows:\n{status}"))
        return

    client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=reply)


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
