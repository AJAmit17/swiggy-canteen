"""GPT via the OpenAI Responses API remote-MCP tool.

Swiggy's tools are executed server-side by OpenAI — we declare the three MCP
servers and the model calls them directly. We never write an MCP client.
Our own tools (solver, policy, ratings) are ordinary function tools we dispatch.

Payment gate: the three tools in SPEND_TOOLS move real money. `allowed_tools`
omits them entirely unless the caller passed the AUTHORISED preamble, which only
handlers behind a Slack button click do. The gate is therefore enforced by the
API's allowlist, not by asking the model nicely.
"""

from __future__ import annotations

import json
import os

# Override with CANTEEN_MODEL if your key has access to something newer.
MODEL = os.environ.get("CANTEEN_MODEL", "gpt-5.5")
MAX_TURNS = 12

SERVERS = {
    "food": ("swiggy-food", "https://mcp.swiggy.com/food"),
    "im": ("swiggy-im", "https://mcp.swiggy.com/im"),
    "dineout": ("swiggy-dineout", "https://mcp.swiggy.com/dineout"),
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

SYSTEM = """You are the office canteen assistant, working inside Slack.

Rules you must not break:
- You never decide who can eat what. Call `solve_restaurant` and use its answer.
- You never place an order, check out, or book a table on your own initiative.
  A human clicks a button for that. Assemble the cart and stop.
- Swiggy menu data has no allergen field. Never tell anyone a dish is safe for
  an allergy. If allergies come up, say what was filtered and add the caveat.
- Money is in whole rupees. Never invent a price you did not read from a tool.
- Be brief. One or two sentences. This is a chat channel, not a report.
"""

AUTHORISED = (
    "The user has explicitly authorised this transaction by clicking a button. "
    "You may complete it now."
)

LOCAL_TOOLS = [
    {
        "type": "function",
        "name": "solve_restaurant",
        "description": (
            "Pick the restaurant for a group order. Given the candidate restaurants "
            "you fetched from Swiggy, returns the choice, a runner-up, and the reason. "
            "This is the only acceptable way to choose a restaurant for a group."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "candidates": {
                    "type": "array",
                    "description": (
                        "Restaurants from search_restaurants, each with id, name, "
                        "cuisines, eta_minutes, is_open, deliverable, and a dishes "
                        "array of {name, price, veg, contains_egg, jain}."
                    ),
                    "items": {"type": "object"},
                }
            },
            "required": ["candidates"],
        },
    },
    {
        "type": "function",
        "name": "get_policy",
        "description": (
            "The current channel's spending policy: per-head cap and vendor allowlist."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "record_rating",
        "description": "Store a 1-5 rating of a restaurant so future picks improve.",
        "parameters": {
            "type": "object",
            "properties": {
                "restaurant_id": {"type": "string"},
                "score": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            "required": ["restaurant_id", "score"],
        },
    },
    {
        "type": "function",
        "name": "log_spend",
        "description": "Record what one person's share of an order cost, in whole rupees.",
        "parameters": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "order_id": {"type": "string"},
                "amount": {"type": "integer"},
            },
            "required": ["user_id", "order_id", "amount"],
        },
    },
    {
        "type": "function",
        "name": "record_pantry_items",
        "description": (
            "Report the Instamart go-to items you fetched, so the par-level diff can "
            "run. Pass every item with its product_id, name and unit price in rupees."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "product_id": {"type": "string"},
                            "name": {"type": "string"},
                            "price": {"type": "integer"},
                        },
                        "required": ["product_id", "name", "price"],
                    },
                }
            },
            "required": ["items"],
        },
    },
    {
        "type": "function",
        "name": "record_dineout_slots",
        "description": (
            "Report the dineout restaurants and their available slots you fetched, so "
            "the ranking can run. Each restaurant needs id, name, rating and a slots "
            "array of {slot_id, hour, time, capacity, is_free}."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "restaurants": {"type": "array", "items": {"type": "object"}},
                "party_size": {"type": "integer"},
                "preferred_hour": {"type": "integer"},
            },
            "required": ["restaurants", "party_size", "preferred_hour"],
        },
    },
]


def mcp_tools(token: str, names: list[str], allow_spend: bool = False) -> list[dict]:
    """One Responses-API `mcp` tool per Swiggy server.

    OpenAI does not store the token, so it is resupplied on every request.
    """
    out = []
    for n in names:
        label, url = SERVERS[n]  # KeyError on a typo, deliberately
        allowed = [
            t for t in SERVER_TOOLS[n] if allow_spend or t not in SPEND_TOOLS
        ]
        out.append({
            "type": "mcp",
            "server_label": label,
            "server_url": url,
            "authorization": token,
            "require_approval": "never",  # the human gate is the Slack button
            "allowed_tools": allowed,
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
        extra_system: str | None = None) -> str:
    """Drive the agent loop until the model stops calling our function tools.

    MCP tool calls execute inside the OpenAI API, so the only calls that reach
    this loop are the local ones. Spending tools are only even visible to the
    model when the caller supplied the AUTHORISED preamble.
    """
    instructions = SYSTEM if not extra_system else SYSTEM + "\n" + extra_system
    tools = [
        *mcp_tools(token, servers, allow_spend=extra_system == AUTHORISED),
        *LOCAL_TOOLS,
    ]
    conversation: list = [{"role": "user", "content": prompt}]

    for _ in range(MAX_TURNS):
        response = client.responses.create(
            model=MODEL,
            instructions=instructions,
            tools=tools,
            input=conversation,
        )
        conversation = conversation + list(response.output)

        calls = [item for item in response.output if item.type == "function_call"]
        if not calls:
            return (response.output_text or "").strip()

        for call in calls:
            try:
                args = json.loads(call.arguments or "{}")
            except json.JSONDecodeError as exc:
                output = f"Error: arguments were not valid JSON ({exc})."
            else:
                output = dispatch_local(call.name, args, ctx)
            conversation.append({
                "type": "function_call_output",
                "call_id": call.call_id,
                "output": output,
            })

    return "I got stuck working on that — try again or narrow the request."
