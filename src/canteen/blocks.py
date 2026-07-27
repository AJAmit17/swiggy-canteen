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
        _context("No tap, no lunch. You can still join before the order goes in."),
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
        bs.append(_context(f"Switching closes in {max(seconds_left // 60, 1)} min."))
    return bs


def dish_picker(dishes: list[Dish]) -> list[dict]:
    if not dishes:
        return [
            _section("Nothing on this menu matches what you eat, so I've left you out."),
            _context(ALLERGEN_CAVEAT),
        ]
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
