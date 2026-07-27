"""Claude via the Anthropic MCP connector.

Swiggy's tools are executed server-side by the Anthropic API — we declare the
three MCP servers and Claude calls them directly. We never write an MCP client.
Our own tools (solver, policy, ratings) are ordinary local tools we dispatch.

Payment gate: the three tools in SPEND_TOOLS move real money. Cart assembly
runs without an explicit authorisation in the system prompt, and only the
handlers behind a Slack button click pass `extra_system` authorising an order.
"""

from __future__ import annotations

import json

MODEL = "claude-opus-5"
MCP_BETA = "mcp-client-2025-11-20"
MAX_TURNS = 12

SERVERS = {
    "food": ("swiggy-food", "https://mcp.swiggy.com/food"),
    "im": ("swiggy-im", "https://mcp.swiggy.com/im"),
    "dineout": ("swiggy-dineout", "https://mcp.swiggy.com/dineout"),
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
        "name": "solve_restaurant",
        "description": (
            "Pick the restaurant for a group order. Given the candidate restaurants "
            "you fetched from Swiggy, returns the choice, a runner-up, and the reason. "
            "This is the only acceptable way to choose a restaurant for a group."
        ),
        "input_schema": {
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
        "name": "get_policy",
        "description": (
            "The current channel's spending policy: per-head cap and vendor allowlist."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "record_rating",
        "description": "Store a 1-5 rating of a restaurant so future picks improve.",
        "input_schema": {
            "type": "object",
            "properties": {
                "restaurant_id": {"type": "string"},
                "score": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            "required": ["restaurant_id", "score"],
        },
    },
    {
        "name": "log_spend",
        "description": "Record what one person's share of an order cost, in whole rupees.",
        "input_schema": {
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
        "name": "record_pantry_items",
        "description": (
            "Report the Instamart go-to items you fetched, so the par-level diff can "
            "run. Pass every item with its product_id, name and unit price in rupees."
        ),
        "input_schema": {
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
        "name": "record_dineout_slots",
        "description": (
            "Report the dineout restaurants and their available slots you fetched, so "
            "the ranking can run. Each restaurant needs id, name, rating and a slots "
            "array of {slot_id, hour, time, capacity, is_free}."
        ),
        "input_schema": {
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


def mcp_servers(token: str, names: list[str]) -> list[dict]:
    out = []
    for n in names:
        server_name, url = SERVERS[n]  # KeyError on a typo, deliberately
        out.append({
            "type": "url",
            "name": server_name,
            "url": url,
            "authorization_token": token,
        })
    return out


def toolsets(names: list[str]) -> list[dict]:
    return [{"type": "mcp_toolset", "mcp_server_name": SERVERS[n][0]} for n in names]


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
    """Drive the agent loop until the model stops calling tools.

    MCP tool calls execute inside the Anthropic API, so the only tool_use blocks
    that reach this loop are our local ones.
    """
    system = SYSTEM if not extra_system else SYSTEM + "\n" + extra_system
    messages: list[dict] = [{"role": "user", "content": prompt}]

    for _ in range(MAX_TURNS):
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=8000,
            betas=[MCP_BETA],
            system=system,
            mcp_servers=mcp_servers(token, servers),
            tools=[*toolsets(servers), *LOCAL_TOOLS],
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "pause_turn":
            continue  # server-side tool loop hit its cap; resend to resume

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return "".join(b.text for b in response.content if b.type == "text").strip()

        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": b.id,
                    "content": dispatch_local(b.name, b.input, ctx),
                }
                for b in tool_uses
            ],
        })

    return "I got stuck working on that — try again or narrow the request."
