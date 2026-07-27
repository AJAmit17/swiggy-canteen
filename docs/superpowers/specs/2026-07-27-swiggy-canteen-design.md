# Swiggy Canteen — Design

Date: 2026-07-27
Status: Approved, ready for implementation planning

## Problem

An autonomous Slack bot for ordering food, groceries, and restaurant tables through
Swiggy's MCP servers. Two audiences: an individual user, and an enterprise team
ordering together. Slack is the only interface; there is no web UI.

The Swiggy app already serves the individual well. It has no group surface, no memory
of who on a team eats what, and no policy layer. That gap is the product.

## North star

Enterprise canteen. A team orders lunch together from a Slack channel, and the bot
picks the restaurant, respects everyone's dietary constraints and the company's budget
policy, assembles one cart, and tracks the delivery — all inside one Slack message.
Individual ordering is the same pipeline with a group of one.

## Ground truth: the Swiggy MCP surface

Three servers, 35 tools, OAuth 2.1 + PKCE with Dynamic Client Registration
(RFC 7591) at `POST /auth/register`. `http://localhost` redirect URIs are allowed for
development. Access tokens last 5 days; user sessions 30 days on a sliding window.

- **Food** (`POST mcp.swiggy.com/food`, 14 tools) — `search_restaurants`,
  `get_restaurant_menu`, `search_menu`, `get_addresses`, `get_food_cart`,
  `update_food_cart`, `flush_food_cart`, `fetch_food_coupons`, `apply_food_coupon`,
  `place_food_order`, `get_food_orders`, `get_food_order_details`, `track_food_order`,
  `report_error`
- **Instamart** (`POST mcp.swiggy.com/im`, 13 tools) — `search_products`,
  `your_go_to_items`, `get_addresses`, `create_address`, `delete_address`, `get_cart`,
  `update_cart`, `clear_cart`, `checkout`, `get_orders`, `get_order_details`,
  `track_order`, `report_error`
- **Dineout** (`POST mcp.swiggy.com/dineout`, 8 tools) — `search_restaurants_dineout`,
  `get_restaurant_details`, `get_saved_locations`, `get_available_slots`,
  `create_cart`, `book_table`, `get_booking_status`, `report_error`

Two constraints drive the whole design:

1. **The food cart is per-account and per-restaurant.** A group order is physically
   one Swiggy account's cart at one restaurant.
2. **There is no delegated auth without an enterprise agreement.** Each Swiggy account
   authenticates for itself.

## Account model

**Single host account.** One office/admin Swiggy account completes OAuth once. Every
Slack participant contributes items to that account's cart; the host account pays with
the corporate card.

Consequences, accepted deliberately:

- Per-person spend tracking lives in our SQLite DB, not in Swiggy. It is a
  chargeback aid, not an audit trail, and can drift from the actual Swiggy bill.
- Everyone in a group order eats from the same restaurant. When the team splits on
  cuisine, the bot picks one and says so — it does not silently please the majority.

## Architecture

```
Slack (Socket Mode)
      │
   app.py ── APScheduler (11:30 roll call, weekly pantry cron)
      │
   agent.py ─────────────────► Anthropic Messages API (claude-opus-5)
      │                          ├─ mcp_servers: /food /im /dineout
   ┌──┴──┐                       │    authorization_token = host's Swiggy token
brain.py  db.py                  └─ local tools: solve_restaurant, get_policy,
(solver)  (SQLite)                                record_rating, log_spend
      │
swiggy_auth.py ── OAuth 2.1 PKCE @ http://localhost:8765/callback
```

### We do not write an MCP client

The Anthropic MCP connector connects to remote MCP servers server-side. We declare the
three Swiggy servers in `mcp_servers` with the host's OAuth token as
`authorization_token`, and pair each with an `mcp_toolset` entry in `tools`. Claude
calls Swiggy's 35 tools directly. Beta header: `mcp-client-2025-11-20`.

```python
client.beta.messages.create(
    model="claude-opus-5",
    max_tokens=16000,
    betas=["mcp-client-2025-11-20"],
    mcp_servers=[
        {"type": "url", "name": "swiggy-food", "url": "https://mcp.swiggy.com/food",
         "authorization_token": token},
        {"type": "url", "name": "swiggy-im", "url": "https://mcp.swiggy.com/im",
         "authorization_token": token},
        {"type": "url", "name": "swiggy-dineout", "url": "https://mcp.swiggy.com/dineout",
         "authorization_token": token},
    ],
    tools=[
        {"type": "mcp_toolset", "mcp_server_name": "swiggy-food"},
        {"type": "mcp_toolset", "mcp_server_name": "swiggy-im"},
        {"type": "mcp_toolset", "mcp_server_name": "swiggy-dineout"},
        *LOCAL_TOOLS,
    ],
    messages=messages,
)
```

Both `mcp_servers` and the matching `mcp_toolset` entries are required; omitting either
is a validation error.

### The governing principle

**LLM for language, Python for decisions.**

Claude parses natural language ("something light, no dairy, under 200") and writes the
one-line rationale users read. Claude never decides whether a dietary constraint is
satisfied, whether an order is under budget, or when to spend money. Those are
deterministic Python in `brain.py`, called as local tools.

Two hard rules follow:

- **Money is human-gated.** `place_food_order`, `checkout`, and `book_table` are never
  reached by an autonomous model turn. The agent assembles the cart; a Slack button
  triggers the call. Enforced in code: the agent loop runs with the food/instamart
  toolsets configured in allowlist mode (`default_config: {enabled: false}` plus
  per-tool `enabled: true`) so the spending tools are not in context during
  assembly, and a separate single-purpose call performs the order after the click.
  Per-tool enablement on `mcp_toolset` is observed to work but is not in the published
  API reference — verify it during implementation. If it is unsupported, fall back to
  the guaranteed mechanism: run assembly and ordering as two separate API calls and
  drop the food/instamart `mcp_toolset` entries entirely from the assembly call,
  reintroducing them only for the post-click order call.
- **Allergen claims are not overstated.** Swiggy menu data has no reliable allergen
  field. Hard filtering runs on the structured veg/non-veg/egg tags and on each
  user's own dish-keyword blocklist. For declared allergies the bot filters what it
  can and shows the allergen caveat inline. It never states that a dish is safe.

## The Canteen Brain

A pure function in `brain.py`. Input: candidate restaurants (dicts), participant
profiles, team order history, policy. Output: a ranked list with a rationale string.
No I/O, no Slack, no Swiggy.

```
candidates = search_restaurants(office_address)

HARD filters (a candidate failing any is discarded):
  - every participant has >= 2 eatable dishes
    (veg / jain / egg tags + per-user keyword blocklist)
  - restaurant is open and deliverable, ETA < 45 min
  - restaurant is in the vendor allowlist, when policy defines one

SCORE (higher is better):
  + mean historical team rating for this restaurant
  - repeat penalty: ordered within 14 days, decaying with age
  - budget overrun: |median dish price - per_head_cap|
  + cuisine diversity vs the last 5 team orders

Pick argmax. Retain the runner-up for the veto button.
```

Repeat fatigue and team taste memory require a group and a history. Swiggy has
neither, which is why this is the part the app cannot replicate.

## Slack surface

### Group lunch (Food) — one message, edited in place

Every stage is a `chat.update` on the same message. Per-person dish selection is
ephemeral and in-thread, so the channel never fills with noise.

1. **11:30 roll call** — scheduled post in `#lunch`. "I'm in" button; no click means out.
2. **12:00 close** — participant list frozen.
3. **Solve** — brain picks a restaurant; the message shows the pick plus a one-line
   *why* ("Everyone can eat here, first time in 3 weeks, ₹190/head").
4. **Veto window** — 5 minutes, one button to switch to the runner-up.
5. **Dish selection** — each participant gets an ephemeral menu filtered to what they
   can eat; picks flow into `update_food_cart`.
6. **Coupons** — `fetch_food_coupons` + `apply_food_coupon`, best available applied.
7. **Confirm** — cart summary and total. A human clicks Place Order.
8. **Track** — `track_food_order` polls; ETA is edited into the same message.
9. **Rate** — one-click rating on delivery, written to history and fed back into
   the solver's score.

`/canteen now` runs the identical pipeline with the timer skipped, which covers the
reactive "lunch for 6, veg-friendly, under ₹200" case with no extra code.

### Pantry (Instamart)

Weekly cron reads `your_go_to_items` for the office address, diffs against a par-level
table in SQLite, and posts "restock 12 items, ₹1,840" with Approve / Edit. Approve
calls `update_cart` then `checkout`. The par-level table is the only new concept;
`your_go_to_items` supplies the rest.

### Team dinner (Dineout)

"Book a table for 8 next Friday" → the agent fans `get_available_slots` across
candidate restaurants from `search_restaurants_dineout`, posts the three best
venue/slot combinations as buttons, calls `create_cart` + `book_table` on click, and
polls `get_booking_status` into the same message. Free reservations only, per the
Dineout tool surface.

## Data model (SQLite)

| Table | Purpose |
|---|---|
| `swiggy_token` | Host account access token, refresh token, expiry |
| `user_profile` | Slack user → diet tag, dish keyword blocklist, spice level, usual budget |
| `office` | Slack channel → Swiggy address id, timezone, roll-call time |
| `policy` | Per-head cap, vendor allowlist, blocked categories |
| `team_order` | Restaurant, timestamp, participants, total — feeds repeat penalty |
| `rating` | Slack user, restaurant, score — feeds the taste memory term |
| `spend` | Slack user, order, amount — chargeback aid, not an audit trail |
| `par_level` | Instamart product id → target quantity |

## Onboarding

First interaction in a DM. The bot asks, one question at a time: diet (veg / jain /
egg / non-veg), anything you never want to see (free-text keyword blocklist),
usual per-meal budget. The office address is picked from `get_addresses` on the host
account. An admin sets policy with `/canteen policy`.

## Error handling

- **Token expiry mid-flow** — the cart is preserved; the bot posts a re-auth link and
  resumes on the same message once the token refreshes.
- **Late joiner** — added if the chosen restaurant still satisfies their constraints;
  told explicitly why not if it doesn't.
- **Swiggy 5xx on `place_food_order`** — never blind-retry. Poll `get_food_orders`
  first to determine whether the order actually landed, then retry only if it did not.
  Double-ordering is the worst failure this system can produce.
- **No candidate survives the hard filters** — the bot says which constraint eliminated
  everything and offers to relax the budget cap or split into two orders.
- **Tool failure inside the agent loop** — surfaced to the channel in plain language,
  and reported upstream via the servers' `report_error` tool.

## Testing

One `test_brain.py`, asserts only, no framework:

- a participant's diet constraint is never violated by the chosen restaurant
- the per-head budget cap is never exceeded when policy defines one
- the repeat penalty actually rotates restaurants across consecutive simulated days
- an empty candidate set after filtering returns the "why nothing survived" reason
  rather than raising

The solver takes plain dicts, so no Slack or Swiggy mocking is required. Everything
else is glue and is exercised by running the app against the localhost OAuth flow.

## Explicitly out of scope for v1

- Delegated / multi-account auth (needs a Swiggy enterprise agreement)
- Splitting a group order across multiple restaurants (Swiggy cart constraint)
- Real payment settlement between teammates — the DB tracks who owes what; moving
  money is not in scope
- Any web UI

## Repository note

This directory is not a git repository, so the spec is written to disk but not
committed. Run `git init` if the spec should be version-controlled.
