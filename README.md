# Swiggy Canteen

A Slack bot that runs a team's food ordering, pantry restocking, and table
booking through the [Swiggy MCP servers](https://mcp.swiggy.com/builders/docs/start/).

The Swiggy app already serves one person well. It has no group surface, no memory
of who on a team eats what, and no policy layer. That gap is what this fills.

## What it does

**Group lunch.** At 11:30 the bot posts one message in `#lunch`. People tap in.
At 12:00 it closes, then solves: which single restaurant can feed everyone given
their diets, the per-head cap, and what the team already ate this fortnight. It
posts the pick with a one-line reason, opens a five-minute veto window, sends each
person a menu filtered to what they can eat, assembles the cart, applies the best
coupon, and — after somebody clicks **Place order** — tracks the delivery. Every
stage edits the same message.

**Pantry.** A weekly job reads Instamart's `your_go_to_items`, diffs it against
your par levels, and posts a restock list to approve.

**Tables.** Ask for a table and it fans out across restaurants and slots, ranks the
three best, and books the one you click.

## Design

Two rules the code enforces rather than merely intends:

**The model handles language; Python handles decisions.** Claude parses requests
and writes the copy. It never decides whether a diet is satisfied, whether an order
is under budget, or when to spend money. That is `brain.py` — pure, tested Python.

**Money needs a human.** `place_food_order`, `checkout`, and `book_table` are only
ever called from a handler behind a button click. The scheduled and conversational
paths assemble carts and stop.

On allergens: Swiggy menu data has no allergen field. Filtering runs on the
structured veg/egg/jain tags and each person's own blocked keywords, and the bot
says exactly that rather than claiming anything is safe.

There is no MCP client here. The Anthropic MCP connector talks to
`mcp.swiggy.com` server-side; we pass the host account's OAuth token and Claude
calls all 35 Swiggy tools directly.

## Setup

```bash
uv sync
cp .env.example .env      # fill in the tokens
```

**Slack app** — create one at [api.slack.com/apps](https://api.slack.com/apps):

- Enable **Socket Mode** (no public URL or ngrok needed)
- Bot scopes: `chat:write`, `commands`, `app_mentions:read`, `im:history`, `im:write`
- Event subscriptions: `app_mention`, `message.im`
- Slash command: `/canteen`
- Install to the workspace — `SLACK_BOT_TOKEN` is the `xoxb-` token,
  `SLACK_APP_TOKEN` the `xapp-` one

**Link the Swiggy host account** (once, from a terminal with a browser):

```bash
uv run python -c "from canteen import db, swiggy_auth; c = db.connect(); db.init_schema(c); print('SWIGGY_CLIENT_ID=' + swiggy_auth.login(c))"
```

This does dynamic client registration, opens the consent page, and captures the
callback on `http://localhost:8765/callback`. Put the printed client id in `.env`.

## Run

```bash
uv run canteen
uv run pytest          # 101 tests, no network
```

Then in Slack:

```
/canteen setup <swiggy_address_id> Asia/Kolkata 11:30   # link this channel to an office
/canteen policy 250 <restaurant_id> ...                 # per-head cap + vendor allowlist
/canteen par <product_id> Milk 1L 6                     # pantry target quantity
/canteen me                                             # set your diet via DM
/canteen now                                            # open a roll call immediately
/canteen addresses                                      # list the account's Swiggy addresses
```

`/canteen addresses` is the easiest way to find the address id for `setup`.

Onboarding is one line in a DM: `veg, no mushroom, 250` — diet, things to avoid,
usual per-meal budget. Diet is `veg`, `jain`, `egg`, or `nonveg`.

## Layout

| File | Responsibility |
|---|---|
| `brain.py` | The solver — diet, budget, repeat fatigue, taste memory. Pure |
| `agent.py` | Claude over the MCP connector; local tool dispatch |
| `swiggy_auth.py` | OAuth 2.1 PKCE, dynamic client registration, auto-refresh |
| `db.py` | SQLite — profiles, policy, history, ratings, par levels |
| `lunch.py` | Group-lunch state machine |
| `blocks.py` | Slack Block Kit builders. Pure |
| `parsing.py` | Agent JSON → solver types; profile text parsing. Pure |
| `pantry.py` / `dineout.py` | Par-level diff; slot ranking. Pure |
| `app.py` | Bolt handlers, scheduler, and the bridge between the above |

Every module except `app.py` is pure or I/O-isolated, which is why the test suite
needs no network and no Slack workspace.

## Known limits

- **One Swiggy account pays.** Per-person shares are tracked locally as a
  chargeback aid, not an audit trail — they can drift from the actual Swiggy bill.
- **One restaurant per group order.** That is Swiggy's cart constraint, not ours.
  When a team splits on cuisine the bot picks one and says so.
- **An in-flight lunch is in memory.** A restart loses it; nothing already paid for
  lives there.
- **Order failures are never blind-retried.** On a failure the bot polls recent
  orders to check whether it actually landed, then tells you. Double-ordering is
  the worst thing this system could do.

Design notes: `docs/superpowers/specs/` · implementation plan: `docs/superpowers/plans/`
