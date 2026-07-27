"""Parsing helpers shared by the Slack layer.

These live outside app.py so they can be tested without Slack credentials.
"""

from __future__ import annotations

import datetime as dt

from canteen.brain import Candidate, Dish

DIETS = {"veg", "jain", "egg", "nonveg"}


def to_candidates(raw: list[dict]) -> list[Candidate]:
    """Turn the agent's JSON restaurants into solver dataclasses.

    Anything missing gets a permissive default — a menu that omits `is_open`
    should not silently disappear from consideration.
    """
    out = []
    for c in raw:
        out.append(Candidate(
            id=str(c.get("id") or c.get("restaurant_id") or ""),
            name=c.get("name", "Unknown"),
            cuisines=c.get("cuisines") or [],
            eta_minutes=int(c.get("eta_minutes", 30)),
            is_open=bool(c.get("is_open", True)),
            deliverable=bool(c.get("deliverable", True)),
            dishes=[
                Dish(
                    name=d["name"],
                    price=int(d.get("price", 0)),
                    veg=bool(d.get("veg", False)),
                    contains_egg=bool(d.get("contains_egg", False)),
                    jain=bool(d.get("jain", False)),
                )
                for d in (c.get("dishes") or [])
                if d.get("name")
            ],
        ))
    return out


def parse_profile(text: str) -> dict:
    """`veg, no mushroom, 250` -> diet, blocklist, budget.

    Unrecognised words become blocklist entries rather than being dropped —
    over-filtering is recoverable, feeding someone the wrong thing is not.
    """
    diet = "nonveg"
    blocklist: list[str] = []
    budget = None
    for part in (p.strip() for p in text.split(",")):
        if not part:
            continue
        low = part.lower()
        if low == "non-veg":
            diet = "nonveg"
        elif low in DIETS:
            diet = low
        elif (digits := low.replace("₹", "").replace("rs", "").strip()).isdigit():
            budget = int(digits)
        else:
            blocklist.append(low.removeprefix("no ").strip())
    return {"diet": diet, "blocklist": blocklist, "budget": budget}


def close_time(roll_call_time: str, window_minutes: int) -> str:
    """`11:30` + 30 -> `12:00`. Handles rollover past the hour."""
    hour, minute = (int(x) for x in roll_call_time.split(":"))
    closes = dt.datetime(2000, 1, 1, hour, minute) + dt.timedelta(minutes=window_minutes)
    return closes.strftime("%H:%M")
