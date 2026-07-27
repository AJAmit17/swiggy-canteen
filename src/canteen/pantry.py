"""Instamart pantry restocking.

`your_go_to_items` already knows what this office buys. All we add is a target
quantity per product, and the diff against what's on hand.
"""

from __future__ import annotations


def restock_diff(go_to_items: list[dict], par: dict[str, dict],
                 on_hand: dict[str, int]) -> list[dict]:
    """Items to reorder. Only products that have a par level AND are currently
    offered by Instamart are considered."""
    out = []
    for product in go_to_items:
        pid = product["product_id"]
        target = par.get(pid)
        if not target:
            continue
        shortfall = target["qty"] - on_hand.get(pid, 0)
        if shortfall <= 0:
            continue
        out.append({
            "product_id": pid,
            "name": product.get("name", target["name"]),
            "qty": shortfall,
            "price": shortfall * product["price"],
        })
    return out


def restock_total(items: list[dict]) -> int:
    return sum(i["price"] for i in items)
