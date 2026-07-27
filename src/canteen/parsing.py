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


AVOID_PREFIXES = ("no ", "not ", "avoid ", "without ", "skip ", "allergic to ")

# An ingredient is a couple of words. A sentence is not an ingredient, and
# treating one as an allergy is how "yes please." became a blocked dish.
MAX_BLOCKLIST_WORDS = 4


def _budget(part: str) -> int | None:
    digits = part.replace("₹", "").replace("rs.", "").replace("rs", "").strip()
    return int(digits) if digits.isdigit() else None


def parse_profile(text: str) -> dict:
    """`veg, no mushroom, 250` -> diet, blocklist, budget.

    `diet` is None when the line did not state one, so the caller can keep
    whatever the person already had instead of silently resetting them to
    nonveg. Short unrecognised parts become blocklist entries — over-filtering
    is recoverable, feeding someone the wrong thing is not — but anything
    sentence-length is ignored, because it is prose, not an ingredient.
    """
    diet = None
    blocklist: list[str] = []
    budget = None
    if not looks_like_profile(text):
        # Nothing in the line is unambiguously dietary, so it is conversation.
        # Guessing here is what turned "yes please." into a blocked dish.
        return {"diet": diet, "blocklist": blocklist, "budget": budget}
    for part in (p.strip() for p in text.split(",")):
        low = part.lower().strip(".!")
        if not low:
            continue
        if low in DIETS or low == "non-veg":
            diet = "nonveg" if low == "non-veg" else low
        elif (amount := _budget(low)) is not None:
            budget = amount
        elif (stripped := _strip_avoid(low)) and len(stripped.split()) <= MAX_BLOCKLIST_WORDS:
            blocklist.append(stripped)
    return {"diet": diet, "blocklist": blocklist, "budget": budget}


def _strip_avoid(part: str) -> str:
    for prefix in AVOID_PREFIXES:
        if part.startswith(prefix):
            return part[len(prefix):].strip()
    return part


def looks_like_profile(text: str) -> bool:
    """Is this DM a diet line, or just conversation?

    Requires something unambiguous — a diet word, a bare number, or an explicit
    'no X'. Without this the bot rewrites your profile every time you say
    anything to it.
    """
    for part in (p.strip().lower().strip(".!") for p in text.split(",")):
        if not part:
            continue
        if part in DIETS or part == "non-veg":
            return True
        if _budget(part) is not None:
            return True
        if part.startswith(AVOID_PREFIXES) and len(_strip_avoid(part).split()) <= MAX_BLOCKLIST_WORDS:
            return True
    return False


def close_time(roll_call_time: str, window_minutes: int) -> str:
    """`11:30` + 30 -> `12:00`. Handles rollover past the hour."""
    hour, minute = (int(x) for x in roll_call_time.split(":"))
    closes = dt.datetime(2000, 1, 1, hour, minute) + dt.timedelta(minutes=window_minutes)
    return closes.strftime("%H:%M")
