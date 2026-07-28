"""Block Kit builders. Pure — no Slack client, no database, no model."""

from __future__ import annotations

import json


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _button(text: str, action_id: str, value: str = "1",
            style: str | None = None) -> dict:
    element = {
        "type": "button",
        "text": {"type": "plain_text", "text": text},
        "action_id": action_id,
        "value": value,
    }
    if style:
        element["style"] = style
    return element


def _actions(*elements: dict) -> dict:
    return {"type": "actions", "elements": list(elements)}


def connect_prompt(url: str) -> list:
    return [
        _section(
            "*Connect your Swiggy account*\nOrders, carts and addresses stay "
            "yours — I never see anyone else's."
        ),
        _section(
            f"1. <{url}|Sign in to Swiggy>\n"
            "2. Afterwards your browser lands on a page that *won't load* — "
            "that is expected.\n"
            "3. Copy that page's address from the URL bar and paste it here."
        ),
    ]


def confirm_purchase(service: str, total: int, summary: str) -> list:
    label = "Place order" if service == "food" else "Check out"
    return [
        _section(f"{summary}\n*Total ₹{total}* · cash on delivery (COD)"),
        _actions(
            _button(f"{label} · ₹{total}", "confirm_purchase", service, "primary"),
            _button("Cancel", "cancel_purchase", service),
        ),
    ]


def confirm_booking(proposal: dict) -> list:
    return [
        _section(
            f"*{proposal['restaurant_name']}*\n"
            f"{proposal['date']} at {proposal['time']} · "
            f"{proposal['guest_count']} people"
        ),
        _actions(
            _button("Book it", "confirm_booking", "1", "primary"),
            _button("Cancel", "cancel_purchase", "dineout"),
        ),
    ]


def group_food(host_user_id: str, restaurant_name: str | None, lines: list[str],
               total: int, joined: list[str]) -> list:
    who = ", ".join(f"<@{u}>" for u in joined) or "nobody yet"
    header = (f"*Group lunch* — on <@{host_user_id}>'s Swiggy account\n"
              f"In: {who}")
    payload = [_section(header)]

    if not restaurant_name:
        payload.append(_section(
            f"<@{host_user_id}>, tell me in this thread where we're ordering from."))
        payload.append(_actions(
            _button("Join", "join_group", "1"),
            _button("Cancel", "cancel_group", "1"),
        ))
        return payload

    body = "\n".join(lines) if lines else "_Cart is empty._"
    payload.append(_section(f"*{restaurant_name}*\n{body}\n\n*Total ₹{total}*"))
    payload.append(_actions(
        _button("Add my dish", "add_my_dish", "1"),
        _button(f"Place order · ₹{total}", "place_group_order", "1", "primary"),
        _button("Cancel", "cancel_group", "1"),
    ))
    return payload


def pantry_list(items: list[dict], total: int) -> list:
    lines = "\n".join(
        f"• {i['name']} ×{i['quantity']} — ₹{i['price']}" for i in items
    ) or "_Nothing suggested._"
    return [
        _section(f"*Pantry restock*\n{lines}\n\n*Total ₹{total}* · cash on delivery"),
        _actions(
            _button(f"Order · ₹{total}", "confirm_purchase", "instamart", "primary"),
            _button("Cancel", "cancel_purchase", "instamart"),
        ),
    ]


def table_options(options: list[dict]) -> list:
    """One button per slot. The whole proposal rides in the button value so the
    click handler needs no lookup — Slack caps that value at 2000 characters."""
    payload = [_section("*Tables I can book*")]
    for option in options:
        value = json.dumps(option, separators=(",", ":"))[:2000]
        payload.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": (f"*{option['restaurant_name']}* · "
                              f"{option['date']} at {option['time']} · "
                              f"{option['guest_count']} people")},
        })
        payload.append(_actions(_button("Pick this", "pick_slot", value)))
    return payload
