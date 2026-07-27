"""The Canteen Brain.

Every decision involving diet, money, or policy is made here, in plain Python.
The language model is never asked to decide any of it. Pure functions only —
no Slack, no Swiggy, no database.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

MIN_DISHES_PER_PERSON = 2
MAX_ETA_MINUTES = 45
REPEAT_WINDOW_DAYS = 14
REPEAT_WEIGHT = 3.0
BUDGET_WEIGHT = 2.0
DIVERSITY_WEIGHT = 1.0
NEUTRAL_RATING = 3.5
DAY = 86400.0

ALLERGEN_CAVEAT = (
    "Swiggy menu data has no allergen field, so this filtering is based on "
    "veg/egg/jain tags and your own blocked keywords only. Please check with "
    "the restaurant for anything serious."
)


@dataclass(frozen=True)
class Dish:
    name: str
    price: int
    veg: bool
    contains_egg: bool = False
    jain: bool = False


@dataclass(frozen=True)
class Candidate:
    id: str
    name: str
    cuisines: list[str]
    eta_minutes: int
    is_open: bool
    deliverable: bool
    dishes: list[Dish]


@dataclass(frozen=True)
class Participant:
    user_id: str
    diet: str  # "veg" | "jain" | "egg" | "nonveg"
    blocklist: list[str] = field(default_factory=list)


@dataclass
class Pick:
    candidate: Candidate
    score: float
    reason: str
    runner_up: Candidate | None
    per_person_dishes: dict[str, list[Dish]]


@dataclass
class Rejection:
    reason: str


def _diet_allows(diet: str, d: Dish) -> bool:
    if diet == "jain":
        return d.jain
    if diet == "veg":
        return d.veg
    if diet == "egg":
        return d.veg or d.contains_egg
    return True  # nonveg


def eatable_dishes(participant: Participant, candidate: Candidate) -> list[Dish]:
    blocked = [b.lower() for b in participant.blocklist]
    return [
        d for d in candidate.dishes
        if _diet_allows(participant.diet, d)
        and not any(b in d.name.lower() for b in blocked)
    ]


def median_price(candidate: Candidate) -> int:
    if not candidate.dishes:
        return 0
    return int(statistics.median(d.price for d in candidate.dishes))


def hard_filter(
    candidates: list[Candidate], participants: list[Participant], policy: dict
) -> tuple[list[Candidate], str | None]:
    """Returns survivors, plus a human-readable reason when nothing survives."""
    allowlist = policy.get("vendor_allowlist") or []
    survivors, reasons = [], []
    for c in candidates:
        if not c.is_open:
            reasons.append(f"{c.name} is closed")
            continue
        if not c.deliverable:
            reasons.append(f"{c.name} does not deliver here")
            continue
        if c.eta_minutes > MAX_ETA_MINUTES:
            reasons.append(f"{c.name} is {c.eta_minutes} min away")
            continue
        if allowlist and c.id not in allowlist:
            reasons.append(f"{c.name} is not on the approved vendor list")
            continue
        starved = [
            p for p in participants
            if len(eatable_dishes(p, c)) < MIN_DISHES_PER_PERSON
        ]
        if starved:
            diets = ", ".join(sorted({p.diet for p in starved}))
            reasons.append(f"{c.name} has too few {diets} options")
            continue
        survivors.append(c)
    if survivors:
        return survivors, None
    if not candidates:
        return [], "No restaurants came back for this address."
    return [], "Nothing worked: " + "; ".join(reasons[:4]) + "."


def score(
    candidate: Candidate, ratings: dict[str, float], recent: list[dict],
    policy: dict, now: float,
) -> float:
    total = ratings.get(candidate.id, NEUTRAL_RATING)

    # Repeat fatigue: strongest the day after, gone after REPEAT_WINDOW_DAYS.
    freshness = 0.0
    for order in recent:
        if order["restaurant_id"] != candidate.id:
            continue
        age_days = (now - order["ordered_at"]) / DAY
        if age_days < REPEAT_WINDOW_DAYS:
            freshness = max(freshness, 1.0 - age_days / REPEAT_WINDOW_DAYS)
    total -= REPEAT_WEIGHT * freshness

    # Budget overrun, proportional to how far over the cap we are.
    cap = policy.get("per_head_cap")
    if cap:
        med = median_price(candidate)
        if med > cap:
            total -= BUDGET_WEIGHT * (med - cap) / cap

    # Cuisine diversity against the last five team orders.
    last_five = {c for order in recent[:5] for c in order.get("cuisines", [])}
    if not set(candidate.cuisines) & last_five:
        total += DIVERSITY_WEIGHT

    return total


def _reason(candidate: Candidate, participants: list[Participant],
            recent: list[dict], now: float) -> str:
    bits = [f"*{candidate.name}*"]
    if len(participants) > 1:
        bits.append(f"all {len(participants)} of you can eat here")
    last = [o for o in recent if o["restaurant_id"] == candidate.id]
    if last:
        days = int((now - last[0]["ordered_at"]) / DAY)
        bits.append(f"last ordered {days}d ago")
    else:
        bits.append("not ordered recently")
    bits.append(f"~₹{median_price(candidate)}/head")
    bits.append(f"{candidate.eta_minutes} min")
    return " — ".join(bits)


def solve(
    candidates: list[Candidate], participants: list[Participant], policy: dict,
    ratings: dict[str, float], recent: list[dict], now: float,
) -> Pick | Rejection:
    survivors, reason = hard_filter(candidates, participants, policy)
    if not survivors:
        return Rejection(reason=reason or "No suitable restaurant found.")

    ranked = sorted(
        survivors,
        key=lambda c: (score(c, ratings, recent, policy, now), c.id),
        reverse=True,
    )
    best = ranked[0]
    return Pick(
        candidate=best,
        score=score(best, ratings, recent, policy, now),
        reason=_reason(best, participants, recent, now),
        runner_up=ranked[1] if len(ranked) > 1 else None,
        per_person_dishes={p.user_id: eatable_dishes(p, best) for p in participants},
    )
