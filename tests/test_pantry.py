from canteen import pantry


def item(pid, name, price):
    return {"product_id": pid, "name": name, "price": price}


PAR = {
    "p1": {"product_id": "p1", "name": "Milk 1L", "qty": 6},
    "p2": {"product_id": "p2", "name": "Coffee 200g", "qty": 2},
}
GO_TO = [item("p1", "Milk 1L", 60), item("p2", "Coffee 200g", 450)]


def test_orders_the_full_par_level_when_nothing_is_on_hand():
    out = pantry.restock_diff(GO_TO, PAR, {})
    assert {i["product_id"]: i["qty"] for i in out} == {"p1": 6, "p2": 2}


def test_orders_only_the_shortfall():
    out = pantry.restock_diff(GO_TO, PAR, {"p1": 4})
    assert {i["product_id"]: i["qty"] for i in out} == {"p1": 2, "p2": 2}


def test_skips_items_already_at_or_above_par():
    out = pantry.restock_diff(GO_TO, PAR, {"p1": 6, "p2": 9})
    assert out == []


def test_ignores_go_to_items_that_have_no_par_level_set():
    go_to = GO_TO + [item("p9", "Chocolate", 100)]
    assert all(i["product_id"] != "p9" for i in pantry.restock_diff(go_to, PAR, {}))


def test_skips_par_items_that_instamart_is_not_offering_right_now():
    out = pantry.restock_diff([item("p1", "Milk 1L", 60)], PAR, {})
    assert [i["product_id"] for i in out] == ["p1"]


def test_line_price_is_unit_price_times_shortfall_quantity():
    out = pantry.restock_diff(GO_TO, PAR, {"p1": 4})
    milk = next(i for i in out if i["product_id"] == "p1")
    assert milk["price"] == 120  # 2 x Rs 60


def test_restock_total_sums_the_line_prices():
    assert pantry.restock_total(pantry.restock_diff(GO_TO, PAR, {})) == 6 * 60 + 2 * 450


def test_restock_total_of_nothing_is_zero():
    assert pantry.restock_total([]) == 0
