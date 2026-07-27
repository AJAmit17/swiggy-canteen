"""The group-lunch state machine.

One live lunch per channel, held in memory. A restart loses an in-flight lunch,
which is acceptable — the next roll call is at most a day away, and nothing
that has already been paid for lives here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from canteen.brain import (
    MIN_DISHES_PER_PERSON,
    Candidate,
    Participant,
    Pick,
    eatable_dishes,
)

OPEN = "open"
PICKED = "picked"
ORDERING = "ordering"
PLACED = "placed"
CANCELLED = "cancelled"

JOINABLE = {OPEN, PICKED, ORDERING}


@dataclass
class LunchState:
    channel_id: str
    message_ts: str
    stage: str = OPEN
    participants: list[str] = field(default_factory=list)
    pick: Pick | None = None
    cart: dict[str, dict] = field(default_factory=dict)
    order_id: str | None = None


# channel_id -> LunchState. One live lunch per channel.
STORE: dict[str, LunchState] = {}


def open_lunch(channel_id: str, message_ts: str) -> LunchState:
    state = LunchState(channel_id=channel_id, message_ts=message_ts)
    STORE[channel_id] = state
    return state


def join(state: LunchState, user_id: str) -> bool:
    """True if newly added. False if already in, or the order has shipped."""
    if state.stage not in JOINABLE or user_id in state.participants:
        return False
    state.participants.append(user_id)
    return True


def close_roll_call(state: LunchState, pick: Pick) -> None:
    state.pick = pick
    state.stage = PICKED


def veto(state: LunchState, participants: list[Participant] | None = None) -> None:
    """Swap to the runner-up. Dishes are cleared — they belonged to the old menu."""
    if not state.pick or not state.pick.runner_up:
        return
    new_best = state.pick.runner_up
    state.pick = Pick(
        candidate=new_best,
        score=state.pick.score,
        reason=f"*{new_best.name}* — switched by veto",
        runner_up=None,
        per_person_dishes={
            p.user_id: eatable_dishes(p, new_best) for p in (participants or [])
        },
    )
    state.cart = {}


def choose_dish(state: LunchState, user_id: str, dish_name: str, price: int) -> None:
    state.cart[user_id] = {"name": dish_name, "price": price}
    state.stage = ORDERING


def cart_lines(state: LunchState) -> list[str]:
    return [
        f"<@{uid}> — {item['name']} ₹{item['price']}"
        for uid, item in state.cart.items()
    ]


def cart_total(state: LunchState) -> int:
    return sum(item["price"] for item in state.cart.values())


def mark_placed(state: LunchState, order_id: str) -> None:
    state.order_id = order_id
    state.stage = PLACED


def can_join_late(participant: Participant, candidate: Candidate) -> bool:
    """A latecomer joins only if the already-chosen restaurant still suits them."""
    return len(eatable_dishes(participant, candidate)) >= MIN_DISHES_PER_PERSON
