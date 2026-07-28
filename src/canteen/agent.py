"""Gemini via the Interactions API remote-MCP tool.

Swiggy's tools are executed server-side by Google — we declare the three MCP
servers and the model calls them directly. We never write an MCP client.
Our own three tools — the two that ask a human to approve spending, and the one
that remembers a preference — are ordinary function tools we dispatch.

Payment gate: the three tools in SPEND_TOOLS move real money. `allowed_tools`
omits them entirely unless the caller passed the AUTHORISED preamble, which only
handlers behind a Slack button click do. The gate is therefore enforced by the
API's allowlist, not by asking the model nicely.
"""

from __future__ import annotations

import json
import os

# Override with CANTEEN_MODEL if your key has access to something newer.
MODEL = os.environ.get("CANTEEN_MODEL", "gemini-3.6-flash")
MAX_TURNS = 12

# Gemini rejects an MCP server name that is not lowercase snake_case.
SERVERS = {
    "food": ("swiggy_food", "https://mcp.swiggy.com/food"),
    "im": ("swiggy_im", "https://mcp.swiggy.com/im"),
    "dineout": ("swiggy_dineout", "https://mcp.swiggy.com/dineout"),
}

# allowed_tools is an allowlist, so the full inventory has to be written down.
# If Swiggy adds a tool it stays invisible until it is listed here.
SERVER_TOOLS = {
    "food": [
        "search_restaurants", "get_restaurant_menu", "search_menu", "get_addresses",
        "get_food_cart", "update_food_cart", "flush_food_cart", "fetch_food_coupons",
        "apply_food_coupon", "place_food_order", "get_food_orders",
        "get_food_order_details", "track_food_order", "report_error",
    ],
    "im": [
        "search_products", "your_go_to_items", "get_addresses", "create_address",
        "delete_address", "get_cart", "update_cart", "clear_cart", "checkout",
        "get_orders", "get_order_details", "track_order", "report_error",
    ],
    "dineout": [
        "search_restaurants_dineout", "get_restaurant_details", "get_saved_locations",
        "get_available_slots", "create_cart", "book_table", "get_booking_status",
        "report_error",
    ],
}

SPEND_TOOLS = {"place_food_order", "checkout", "book_table"}

# Swiggy v1 limits, enforced here rather than left to the model.
FOOD_CAP_RUPEES = 1000
INSTAMART_MIN_RUPEES = 99


def blocked_reason(service: str, total: int) -> str | None:
    """Why this cart may not be ordered yet, or None if it may."""
    if service == "food" and total > FOOD_CAP_RUPEES:
        return (f"That cart is ₹{total}, over Swiggy's ₹{FOOD_CAP_RUPEES} limit "
                "for this kind of order. Drop an item or two.")
    if service == "instamart" and total < INSTAMART_MIN_RUPEES:
        return (f"Instamart needs at least ₹{INSTAMART_MIN_RUPEES} and this cart "
                f"is ₹{total}. Add something else.")
    return None


SYSTEM = """You are a Swiggy assistant living in Slack. You order food, order
groceries from Instamart, and book restaurant tables — by talking, not by
making people learn commands.

How to work:
- The cart lives on Swiggy's servers, not in this conversation. Before you talk
  about a cart or change it, call get_food_cart or get_cart and use what comes
  back. Never quote a total you did not just read from a tool.
- Only suggest restaurants whose availabilityStatus is OPEN, and dineout
  restaurants whose availability is AVAILABLE.
- The food cart holds one restaurant at a time. Changing restaurant empties it —
  say so and get a yes before you do it.
- Payment is cash on delivery only, so ignore coupons that need online payment.

How orders actually happen:
- You never place an order, check out, or book a table yourself. When everything
  is ready, call propose_purchase or propose_booking. A human then clicks a
  button, and only then are you asked to complete it.
- Confirm the date, party size and time out loud before proposing a booking.

Being honest:
- Swiggy's menu data has no allergen information. Never say a dish is safe for
  an allergy. Say what you filtered on and that you cannot verify ingredients.
- Money is in whole rupees.
- If someone tells you a lasting preference — diet, a dislike, a usual budget —
  call remember_preference so you do not ask again.

How to write:
- Slack, not email. Two or three sentences. No tables, no headings.
"""

AUTHORISED = (
    "The user has explicitly authorised this transaction by clicking a button. "
    "Re-read the cart first. If the total differs materially from what they "
    "approved, stop and say so instead of ordering. Otherwise complete it now "
    "and report the id."
)


def system_for(preference: str | None) -> str:
    """The system instruction, with this person's standing preferences."""
    if not preference:
        return SYSTEM
    return f"{SYSTEM}\nWhat this person has told you before: {preference}"


LOCAL_TOOLS = [
    {
        "type": "function",
        "name": "propose_purchase",
        "description": (
            "Call when a Swiggy cart is ready to be paid for. Shows the human a "
            "confirm button. This is the only way an order can happen — you "
            "cannot place it yourself. Read the total from get_food_cart or "
            "get_cart immediately before calling this."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "enum": ["food", "instamart"]},
                "total": {"type": "integer",
                          "description": "Cart total in whole rupees, from the cart tool."},
                "summary": {"type": "string",
                            "description": "One line: what is in the cart and from where."},
            },
            "required": ["service", "total", "summary"],
        },
    },
    {
        "type": "function",
        "name": "propose_booking",
        "description": (
            "Call when a specific dineout slot is ready to be booked. Shows the "
            "human a confirm button. You cannot book a table yourself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "restaurant_id": {"type": "string"},
                "restaurant_name": {"type": "string"},
                "slot_id": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "time": {"type": "string", "description": "As shown to the user, IST"},
                "guest_count": {"type": "integer"},
            },
            "required": ["restaurant_id", "restaurant_name", "slot_id", "date",
                         "time", "guest_count"],
        },
    },
    {
        "type": "function",
        "name": "remember_preference",
        "description": (
            "Store a lasting fact about this person — diet, dislikes, usual "
            "budget — so it is not asked again. Pass the whole preference line, "
            "not just the new part."
        ),
        "parameters": {
            "type": "object",
            "properties": {"note": {"type": "string"}},
            "required": ["note"],
        },
    },
]


def mcp_tools(token: str, names: list[str], allow_spend: bool = False) -> list[dict]:
    """One Interactions-API `mcp_server` tool per Swiggy server.

    The Swiggy OAuth token rides in the Authorization header Google sends to the
    MCP endpoint. Google does not store it, so it is resupplied every request.
    """
    out = []
    for n in names:
        label, url = SERVERS[n]  # KeyError on a typo, deliberately
        allowed = [
            t for t in SERVER_TOOLS[n] if allow_spend or t not in SPEND_TOOLS
        ]
        out.append({
            "type": "mcp_server",
            "name": label,
            "url": url,
            "headers": {"Authorization": f"Bearer {token}"},
            "allowed_tools": [{"mode": "auto", "tools": allowed}],
        })
    return out


def dispatch_local(name: str, args: dict, ctx: dict) -> str:
    """ctx maps a local tool name to a callable. Errors come back as text so the
    model can recover instead of the whole turn blowing up."""
    fn = ctx.get(name)
    if fn is None:
        return f"Error: unknown tool {name!r}."
    try:
        result = fn(**args)
    except Exception as exc:  # surfaced to the model, and logged by the caller
        return f"Error: {exc}"
    return result if isinstance(result, str) else json.dumps(result, default=str)


def run(client, prompt: str, token: str, servers: list[str], ctx: dict,
        extra_system: str | None = None,
        previous_id: str | None = None) -> tuple[str, str]:
    """Drive the agent loop until the model stops calling our function tools.

    Returns (reply_text, interaction_id). The caller stores the id and passes it
    back as previous_id next turn — that is the entire multi-turn state model,
    because Google keeps the transcript and Swiggy keeps the cart.

    MCP tool calls execute inside the Gemini API, so the only calls reaching
    this loop are local ones. Spending tools are visible to the model only when
    the caller supplied the AUTHORISED preamble.
    """
    settings = {
        "model": MODEL,
        "system_instruction": extra_system or SYSTEM,
        "tools": [
            *mcp_tools(token, servers, allow_spend=AUTHORISED in (extra_system or "")),
            *LOCAL_TOOLS,
        ],
    }
    payload: str | list = prompt
    interaction_id = previous_id

    for _ in range(MAX_TURNS):
        interaction = client.interactions.create(
            input=payload, previous_interaction_id=interaction_id, **settings
        )
        interaction_id = interaction.id

        calls = [s for s in (interaction.steps or []) if s.type == "function_call"]
        if not calls:
            return (interaction.output_text or "").strip(), interaction_id

        payload = [
            {
                "type": "function_result",
                "call_id": call.id,
                "name": call.name,
                # arguments arrives already decoded, unlike the other providers
                "result": dispatch_local(call.name, call.arguments or {}, ctx),
            }
            for call in calls
        ]

    return ("I got stuck working on that — try again or narrow the request.",
            interaction_id)
