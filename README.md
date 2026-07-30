# Swiggy for Slack

Order food, order groceries, and book tables from Slack — by talking, through the
[Swiggy MCP servers](https://mcp.swiggy.com/builders/docs/start/).

Everyone uses their own Swiggy account. There are no commands to learn.

## What it does

**DM the bot** to use your own Swiggy account:

> order me a masala dosa from somewhere south indian
> what did I order last week?
> we're out of milk and coffee — get some
> book a table for four on Saturday at 8

**Mention it in a channel** for group things:

> @Swiggy lunch — group food order on your account
> @Swiggy book a table for 8 at 8pm
> @Swiggy restock the pantry

## Design

**Money needs a human.** `place_food_order`, `checkout` and `book_table` are only
reachable from a handler behind a button click — the API is never even told those
tools exist until then. Everything else assembles a cart and stops.

**We hold almost no state.** Swiggy owns the cart and the orders. Claude's
Messages API is stateless, so we hold the conversation transcript ourselves —
that's the only reason it's on disk at all. What's left is a token per person, a
preference line, that transcript per channel, and any group flow currently
running.

**No MCP client here.** Claude's Messages API MCP connector talks to
`mcp.swiggy.com` server-side; we pass each person's OAuth token and the model
calls the Swiggy tools directly.

On allergens: Swiggy menu data has no allergen field. The bot says what it
filtered on and that it cannot verify ingredients, rather than claiming anything
is safe.

## Setup

1. Create the Slack app from `slack-app-manifest.yaml`, install it, and copy the
   bot token (`xoxb-`) and an app-level token with `connections:write` (`xapp-`).
2. Get an Anthropic API key at <https://console.anthropic.com>.
3. Copy `.env.example` to `.env` and fill in the three values.
4. `uv sync && uv run canteen`

## Connecting Swiggy

Each person connects their own account, once. DM the bot and it walks you
through it:

1. Click the sign-in link it sends you.
2. Sign in to Swiggy.
3. Your browser lands on a page that **fails to load**. That is expected —
   nothing is listening on that address.
4. Copy that page's URL from the address bar and paste it back into the DM.

Your token is stored against your Slack user id. Carts, orders and addresses are
yours alone. Say `reset` in the DM to start a fresh conversation.

## Layout

| File | Responsibility |
|---|---|
| `store.py` | SQLite — tokens, preferences, conversation history, group flows |
| `auth.py` | Per-user OAuth 2.1 + PKCE, the paste flow, auto-refresh |
| `agent.py` | Claude over the Messages API MCP connector; money guards |
| `blocks.py` | Slack Block Kit builders. Pure |
| `slackfmt.py` | Markdown → Slack mrkdwn. Pure |
| `app.py` | Bolt app: DM assistant, spend handlers, error reporting |
| `group.py` | The three channel flows: group food, table booking, pantry |

`uv run pytest` — 94 tests, no network and no Slack workspace needed.

## Known limits

- **The host pays for a group order.** It goes on the Swiggy account of whoever
  started the flow, and only they can click Place order.
- **One restaurant per food cart.** Swiggy's constraint, not ours — switching
  restaurants empties the cart, and the bot asks first.
- **Food orders are COD-only and capped at ₹1000; Instamart needs ₹99 minimum.**
  Both are checked before a confirm button is ever shown.
- **A pending confirmation is in memory.** A restart drops it, deliberately —
  nobody should be able to confirm an hour-old cart that has changed underneath.
- **Order failures are never blind-retried.** On a failure the bot reads back
  recent orders and reports what it finds. Double-ordering is the worst thing
  this system could do.

Design notes: `docs/superpowers/specs/` · implementation plan: `docs/superpowers/plans/`
