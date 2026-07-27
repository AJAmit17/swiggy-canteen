from canteen import dineout


def r(rid, name, slots, rating=4.0):
    return {"id": rid, "name": name, "rating": rating, "slots": slots}


def s(sid, hour, capacity, free=True):
    return {"slot_id": sid, "hour": hour, "time": f"{hour}:00",
            "capacity": capacity, "is_free": free}


def test_slots_too_small_for_the_party_are_dropped():
    out = dineout.rank_slots([r("r1", "Toit", [s("a", 20, 4), s("b", 21, 10)])], 8, 20)
    assert [o["slot_id"] for o in out] == ["b"]


def test_paid_slots_are_dropped_because_book_table_is_free_only():
    out = dineout.rank_slots(
        [r("r1", "Toit", [s("a", 20, 10, free=False), s("b", 21, 10)])], 4, 20
    )
    assert [o["slot_id"] for o in out] == ["b"]


def test_slots_closest_to_the_preferred_hour_rank_first():
    out = dineout.rank_slots(
        [r("r1", "Toit", [s("a", 18, 10), s("b", 20, 10), s("c", 23, 10)])], 4, 20
    )
    assert [o["slot_id"] for o in out] == ["b", "a", "c"]


def test_rating_breaks_a_tie_between_equally_timed_slots():
    out = dineout.rank_slots([
        r("r1", "Low", [s("a", 20, 10)], rating=3.0),
        r("r2", "High", [s("b", 20, 10)], rating=4.8),
    ], 4, 20)
    assert [o["slot_id"] for o in out] == ["b", "a"]


def test_only_the_top_three_come_back_by_default():
    slots = [s(str(i), 20, 10) for i in range(9)]
    assert len(dineout.rank_slots([r("r1", "Toit", slots)], 4, 20)) == 3


def test_each_option_carries_what_the_button_needs():
    out = dineout.rank_slots([r("r1", "Toit", [s("a", 20, 10)])], 4, 20)
    assert out[0]["restaurant_id"] == "r1"
    assert out[0]["restaurant_name"] == "Toit"
    assert out[0]["slot_id"] == "a"
    assert out[0]["time"] == "20:00"


def test_no_viable_slot_returns_an_empty_list_not_an_error():
    assert dineout.rank_slots([r("r1", "Toit", [s("a", 20, 2)])], 12, 20) == []


def test_internal_ranking_keys_do_not_leak_into_the_result():
    out = dineout.rank_slots([r("r1", "Toit", [s("a", 20, 10)])], 4, 20)
    assert not any(k.startswith("_") for k in out[0])
