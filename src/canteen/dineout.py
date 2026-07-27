"""Dineout slot ranking.

`book_table` handles free reservations only, so paid slots are filtered out
rather than offered and then rejected at booking time.
"""

from __future__ import annotations

NEUTRAL_RATING = 3.5


def rank_slots(restaurants: list[dict], party_size: int, preferred_hour: int,
               ratings: dict[str, float] | None = None, limit: int = 3) -> list[dict]:
    ratings = ratings or {}
    options = []
    for rest in restaurants:
        rating = ratings.get(rest["id"], rest.get("rating", NEUTRAL_RATING))
        for slot in rest.get("slots", []):
            if not slot.get("is_free", True):
                continue
            if slot["capacity"] < party_size:
                continue
            options.append({
                "restaurant_id": rest["id"],
                "restaurant_name": rest["name"],
                "slot_id": slot["slot_id"],
                "time": slot["time"],
                "hour": slot["hour"],
                "_distance": abs(slot["hour"] - preferred_hour),
                "_rating": rating,
            })
    options.sort(key=lambda o: (o["_distance"], -o["_rating"], o["slot_id"]))
    return [
        {k: v for k, v in o.items() if not k.startswith("_")}
        for o in options[:limit]
    ]
