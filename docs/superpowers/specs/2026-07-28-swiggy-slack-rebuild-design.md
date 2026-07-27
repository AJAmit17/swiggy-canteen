# Swiggy Slack Assistant — Rebuild Design

**Status:** approved for planning
**Supersedes:** `2026-07-27-swiggy-canteen-design.md`

## Why a rebuild

The first build guessed at Swiggy's contract and invented an "autonomous canteen
brain" around the guess. Reading the recipes showed three of its load-bearing
assumptions were wrong:

1. **The cart is server-side.** `lunch.py` kept an in-memory cart and treated it
   as truth. The docs are explicit: *"your agent doesn't need to carry cart IDs
   or contents between turns. Just call `get_*_cart` at the top of any turn that
   might touch the cart, and you'll see the truth."*
2. **Field names are camelCase.** `parsing.to_candidates` reads `id`, `is_open`,
   `eta_minutes`. Swiggy returns `restaurantId`, `availabilityStatus`, `spinId`.
   Nothing in the group-order path could ever have worked.
3. **A constraint solver was never the product.** Scheduled roll calls, veto
   windows, par levels and a diet solver made a Slack app that an ordinary
   employee has to be taught. The three Swiggy services are the product.

This design deletes the invented machinery and builds the three flows Swiggy
actually documents, with a personal assistant in DMs and group work in channels.

## Ground truth

Three MCP servers, OAuth 2.1 + PKCE with registration at `POST /auth/register`.
`http://localhost` redirect URIs are allowed. Access tokens last 5 days;
sessions 30 days sliding.

Registration returns the **static** `client_id: "swiggy-mcp"` regardless of the
`client_name` or `redirect_uris` sent. Verified: any `http`/`https` redirect URI
is accepted at registration; `urn:ietf:wg:oauth:2.0:oob` is rejected with
`redirect_uri protocol must be one of: http:, https:, cursor:`.

### Food (`https://mcp.swiggy.com/food`)

Canonical order, with the argument and response names the recipe gives:

| Step | Tool | Arguments | Response fields used |
|---|---|---|---|
| 1 | `get_addresses` | — | `label`, `addressId` |
| 2 | `search_restaurants` | `addressId`, `query` | `restaurantId`, `availabilityStatus` |
| 3 | `get_restaurant_menu` | `restaurantId` | categories, items, variants, add-ons |
| 4 | `update_food_cart` | `restaurantId`, `items:[{itemId, quantity}]` | — |
| 5 | `fetch_food_coupons` | — | `code`, `requiresOnlinePayment` |
| 6 | `apply_food_coupon` | `code` | — |
| 7 | `get_food_cart` | — | `total`, items |
| 8 | `place_food_order` | `paymentMethod: "COD"` | `orderId` |
| 9 | `track_food_order` | `orderId` | status |

Also available: `search_menu`, `flush_food_cart`, `get_food_orders`,
`get_food_order_details`, `report_error`.

Constraints: only `availabilityStatus == "OPEN"` restaurants may be
recommended; the cart is tied to a single restaurant and changing it flushes
the cart; **₹1000 cart cap**; COD only in v1, so only coupons with
`requiresOnlinePayment == false` may be applied.

### Instamart (`https://mcp.swiggy.com/im`)

| Step | Tool | Arguments | Response fields used |
|---|---|---|---|
| 1 | `get_addresses` | — | `addressId` |
| 2 | `your_go_to_items` / `search_products` | `addressId` (+ `query`) | `variants[].spinId` |
| 3 | `update_cart` | `items:[{spinId, quantity}]` | — |
| 4 | `get_cart` | — | `items[]`, bill, payment methods |
| 5 | `checkout` | `paymentMethod: "COD"` | `orderId` |
| 6 | `track_order` | `orderId` | status |

Also: `create_address`, `delete_address`, `clear_cart`, `get_orders`,
`get_order_details`, `report_error`.

Constraints: **₹99 minimum order**; COD only; the cart is address-locked, so
`clear_cart` before switching address; expect `ADDRESS_NOT_SERVICEABLE` and
`MIN_ORDER_NOT_MET`.

### Dineout (`https://mcp.swiggy.com/dineout`)

| Step | Tool | Arguments | Response fields used |
|---|---|---|---|
| 1 | `get_saved_locations` | — | `lat`, `lng` |
| 2 | `search_restaurants_dineout` | `lat`, `lng`, `query` | `id`, `availability` |
| 3 | `get_restaurant_details` | `restaurantId` | ratings, amenities, deals |
| 4 | `get_available_slots` | `restaurantId`, `date` (YYYY-MM-DD), `guestCount` | `slotId` |
| 5 | `book_table` | `restaurantId`, `slotId`, `guestCount` | `bookingId` |
| 6 | `get_booking_status` | `bookingId` | confirmation, address |

Constraints: filter `availability == "AVAILABLE"`; 7-day forward window; all
times IST; expect `SLOT_UNAVAILABLE`, `RESTAURANT_NOT_BOOKABLE`,
`BOOKING_WINDOW_CLOSED`.

### Not idempotent

`place_food_order`, `checkout` and `book_table` are **not idempotent**. On a 5xx
the client must call `get_food_orders` / `get_orders` / `get_booking_status`
before doing anything else, and must never retry automatically.

## The two surfaces

**DM — your personal Swiggy assistant.** Free-form conversation against your own
Swiggy account. Order food, order groceries, book a table, track an order, ask
what you ordered last week. No commands to learn.

**Channel — group work.** Three flows, each started by a mention:

- **Group food order** — `@Canteen lunch`
- **Group table booking** — `@Canteen table for 8 at 8pm`
- **Office pantry restock** — `@Canteen restock the pantry`

The rule that keeps this comprehensible: *a DM spends your money, a channel
spends the money of whoever started the flow.* Nothing is ever charged to a
person who did not click a button.

## Per-user authentication

Every person connects their own Swiggy account, so carts, order history and
payment are genuinely private. The obstacle is that OAuth needs a redirect the
bot can read, and Socket Mode gives us no public URL.

**The paste flow.** Uniform for everyone, needs no whitelisting:

1. The bot DMs a personal authorize URL built with a fresh PKCE verifier and
   `state`, both held against the Slack user id with a 10-minute expiry.
2. The person signs in to Swiggy in their browser.
3. Swiggy redirects to `http://localhost:8765/callback?code=…&state=…`. Nothing
   is listening, so the browser shows a connection error. **This is expected**
   and the bot says so in advance.
4. The person copies the URL from the address bar and pastes it into the DM.
5. The bot extracts `code` and `state`, checks `state` against the pending
   record, exchanges the code with the stored verifier, and saves the token
   against that Slack user.

A pasted code is safe to the extent that matters: PKCE binds it to a verifier
the bot never transmitted, it is single-use, and it expires in 120 seconds. The
bot accepts a paste **only in a DM** and deletes the pending record on first
use, success or failure.

No local callback listener. It only ever works for whoever runs the process, and
a second auth path is a second thing to break.

Tokens refresh automatically within 5 minutes of expiry. A refresh failure
clears the token and prompts the person to reconnect; it never silently retries.

## State

Three systems hold state, and the split is what keeps this small:

| Holder | What it holds |
|---|---|
| Swiggy | the cart, the orders, the bookings |
| Gemini | the conversation transcript |
| Us | one token, one preferences line, one interaction id, per person |

We never cache cart contents. Any turn that may touch a cart begins by calling
`get_food_cart` or `get_cart`. Multi-turn continuity is a single
`previous_interaction_id` per conversation, passed back to the Interactions API.

## Architecture

```
Slack (Socket Mode)
      │
   app.py ──────── routing only: DM -> personal, mention -> group
      │
   ┌──┴────────────┐
  (DM path)        group.py ── group order / table / pantry lifecycle
      │                │
      └────────┬───────┘
            agent.py ── Gemini Interactions API
               │           └─ mcp_server tools -> mcp.swiggy.com (server-side)
               │
   ┌───────────┼───────────┐
store.py    auth.py     blocks.py + slackfmt.py
(SQLite)  (per-user OAuth)  (Block Kit + mrkdwn)
```

Seven modules. `agent.py` and `slackfmt.py` carry over essentially unchanged —
both are verified working against the live APIs.

### Module responsibilities

- **`app.py`** — Slack handlers, routing, and the DM path. The DM path needs no
  module of its own: it is one call into `agent.py` with the person's token
  and preference line. Target < 300 lines.
- **`agent.py`** — Gemini Interactions API over the Swiggy MCP servers; the
  spend-tool allowlist gate; the local tool dispatch loop.
- **`auth.py`** — PKCE, the paste flow, per-user token storage and refresh.
- **`store.py`** — SQLite. Five tables (below).
- **`group.py`** — the three group flows as pure-ish state transitions.
- **`blocks.py`** — Block Kit builders. Pure.
- **`slackfmt.py`** — markdown to mrkdwn. Pure. Unchanged.

### Data model

```sql
create table swiggy_token (          -- one row per Slack user
    user_id text primary key,
    access_token text not null,
    refresh_token text,
    expires_at real not null
);
create table pending_auth (          -- in-flight paste flows
    user_id text primary key,
    verifier text not null,
    state text not null,
    created_at real not null
);
create table preference (            -- one free-text line per person
    user_id text primary key,
    note text not null
);
create table conversation (          -- Gemini continuity
    key text primary key,            -- slack channel id (a DM or a channel)
    interaction_id text not null,
    updated_at real not null
);
create table group_order (           -- one live group flow per channel
    channel_id text primary key,
    kind text not null,              -- food | table | pantry
    host_user_id text not null,
    message_ts text not null,
    context text not null,           -- json: restaurantId, guestCount, etc.
    created_at real not null
);
```

Dropped from the old schema: `policy`, `par_level`, `rating`, `spend`,
`team_order` and `user_profile`. `swiggy_token` survives in name only — it
was a single row pinned to `id = 1`, and is now keyed by Slack user.

Preferences are one free-text line, injected into the system instruction. "I'm
vegetarian, no mushroom, usually spend around 300" is more useful to a language
model than a diet enum, and it deletes the solver.

## Group food order

1. `@Canteen lunch` → bot creates a `group_order` row and posts a live message:
   who is hosting, that it will be charged to their Swiggy account, and a
   **Join** button.
2. The host settles the restaurant conversationally in the thread. The bot uses
   `search_restaurants` filtered to `availabilityStatus == "OPEN"` and writes
   `restaurantId` into the row's `context`.
3. Each joiner gets an ephemeral **Add my dish** picker, backed by `search_menu`
   on that restaurant. Their selection calls `update_food_cart` on the **host's**
   token.
4. After every mutation the bot calls `get_food_cart` and rewrites the live
   message from the response. The displayed total is always Swiggy's, never ours.
5. The host clicks **Place order (₹X, COD)**. Only this path may reach
   `place_food_order`.
6. The bot reports `orderId` and offers tracking.

Concurrent joiners mutate one server-side cart, so cart-mutating calls for a
given channel are serialised behind a per-channel lock. This is the only lock in
the system.

If the cart exceeds ₹1000 the bot refuses to offer the button and says which
items push it over. If the host tries to change restaurant mid-order the bot
warns that the cart will be flushed and requires a second confirmation, per the
recipe's mutation-safety rule.

## Group table booking

`@Canteen table for 8 at 8pm` → `get_saved_locations` for the requester's lat/lng
→ `search_restaurants_dineout` filtered to `availability == "AVAILABLE"` →
`get_available_slots` for the top few with the parsed `date` and `guestCount` →
a message listing restaurants and slot buttons → whoever clicks owns the booking
and it is made on their token → `bookingId` and confirmation.

Date and party size are echoed back for confirmation before `book_table`, as the
recipe requires.

## Office pantry restock

`@Canteen restock the pantry` → `your_go_to_items` on the starter's account →
a checklist of the suggested items with quantities → the starter unchecks what
is not needed and clicks **Order (₹X, COD)** → `update_cart` then `checkout`.

No par levels. The list comes from Swiggy's own reorder signal, and a human
edits it. If the total is under ₹99 the bot says so instead of offering the
button.

## Money

Two independent gates, both already proven in the current build:

1. **The allowlist gate.** `place_food_order`, `checkout` and `book_table` are
   omitted from `allowed_tools` on every request that was not triggered by a
   button click. The model cannot spend money it was never shown.
2. **The button gate.** Every purchase is a Slack button whose label carries the
   exact total read back from `get_food_cart` / `get_cart` immediately before
   rendering.

COD only, so the button *is* the payment decision. Caps are enforced in our code
before the button is offered, not left to the model.

## Error handling

| Situation | Behaviour |
|---|---|
| No token for this person | "Connect your Swiggy account" button, flow paused |
| Token expired, refresh works | Silent |
| Token expired, refresh fails | Token cleared, reconnect prompt |
| Tool error (`ADDRESS_NOT_SERVICEABLE`, `SLOT_UNAVAILABLE`, …) | Surfaced in plain language with the next useful action |
| 5xx from a spend tool | **Never retried.** Verify with `get_food_orders` / `get_orders` / `get_booking_status`, then report what actually happened |
| Any uncaught handler exception | Global Bolt error handler posts it to the channel |
| Model call in flight | Placeholder message that becomes the answer, including on failure |

Allergen honesty carries over: Swiggy's menu data has no allergen field, so the
bot never states that a dish is safe for an allergy. It says what it filtered on
and names the limitation.

## Testing

pytest, plain asserts, no network. Pure modules (`slackfmt`, `blocks`, `group`
transitions, cap and minimum checks, the redirect-URL parser) are tested
directly. Slack, Gemini and Swiggy are faked at their client boundaries — the
same approach that caught the threading and mrkdwn regressions.

Specific guards worth naming:

- Spend tools absent from `allowed_tools` unless authorised, asserted on both paths.
- A pasted callback URL with a mismatched `state` is rejected.
- A pending auth record is consumed exactly once.
- Cart totals shown to the user come from a Swiggy response, never computed locally.
- The ₹1000 cap and ₹99 minimum block the button.
- A paste in a channel is refused; only DMs are accepted.

## Out of scope

Online payment (Swiggy v1 is COD only). Split billing and per-person expense
reconciliation. Scheduled roll calls. Restaurant rating memory. Par levels.
Multi-workspace deployment. Any Swiggy call not in the three recipes.
