from canteen import lunch
from canteen.brain import Candidate, Dish, Participant, Pick


def d(name, price=150, veg=True):
    return Dish(name=name, price=price, veg=veg)


def cand(cid="r1", name="Sattvik", dishes=None):
    return Candidate(id=cid, name=name, cuisines=["south"], eta_minutes=25,
                     is_open=True, deliverable=True,
                     dishes=dishes or [d("Dosa", 120), d("Idli", 80)])


def a_pick(runner_up=None):
    c = cand()
    return Pick(candidate=c, score=4.0, reason="r", runner_up=runner_up,
                per_person_dishes={"U1": c.dishes})


def test_a_new_lunch_is_open_with_nobody_in_it():
    s = lunch.open_lunch("C1", "1.1")
    assert s.stage == lunch.OPEN
    assert s.participants == []


def test_join_is_idempotent_and_preserves_order():
    s = lunch.open_lunch("C1", "1.1")
    assert lunch.join(s, "U1") is True
    assert lunch.join(s, "U2") is True
    assert lunch.join(s, "U1") is False
    assert s.participants == ["U1", "U2"]


def test_closing_the_roll_call_moves_to_picked_and_stores_the_pick():
    s = lunch.open_lunch("C1", "1.1")
    lunch.join(s, "U1")
    p = a_pick()
    lunch.close_roll_call(s, p)
    assert s.stage == lunch.PICKED
    assert s.pick is p


def test_veto_swaps_in_the_runner_up_and_clears_any_chosen_dishes():
    s = lunch.open_lunch("C1", "1.1")
    lunch.join(s, "U1")
    lunch.close_roll_call(s, a_pick(runner_up=cand("r2", "Toit")))
    lunch.choose_dish(s, "U1", "Dosa", 120)
    lunch.veto(s)
    assert s.pick.candidate.id == "r2"
    assert s.cart == {}


def test_veto_without_a_runner_up_is_a_no_op():
    s = lunch.open_lunch("C1", "1.1")
    lunch.close_roll_call(s, a_pick(runner_up=None))
    lunch.veto(s)
    assert s.pick.candidate.id == "r1"


def test_veto_recomputes_the_per_person_dish_lists_for_the_new_menu():
    s = lunch.open_lunch("C1", "1.1")
    lunch.join(s, "U1")
    toit = cand("r2", "Toit", dishes=[d("Wings", veg=False), d("Fries")])
    lunch.close_roll_call(s, a_pick(runner_up=toit))
    lunch.veto(s, [Participant("U1", "veg", [])])
    assert [x.name for x in s.pick.per_person_dishes["U1"]] == ["Fries"]


def test_choosing_a_dish_replaces_that_persons_previous_choice():
    s = lunch.open_lunch("C1", "1.1")
    lunch.join(s, "U1")
    lunch.close_roll_call(s, a_pick())
    lunch.choose_dish(s, "U1", "Dosa", 120)
    lunch.choose_dish(s, "U1", "Idli", 80)
    assert s.cart == {"U1": {"name": "Idli", "price": 80}}
    assert lunch.cart_total(s) == 80


def test_cart_lines_and_total_across_several_people():
    s = lunch.open_lunch("C1", "1.1")
    lunch.join(s, "U1")
    lunch.join(s, "U2")
    lunch.close_roll_call(s, a_pick())
    lunch.choose_dish(s, "U1", "Dosa", 120)
    lunch.choose_dish(s, "U2", "Idli", 80)
    assert lunch.cart_total(s) == 200
    assert lunch.cart_lines(s) == ["<@U1> — Dosa ₹120", "<@U2> — Idli ₹80"]


def test_a_late_joiner_is_accepted_when_the_restaurant_still_suits_them():
    assert lunch.can_join_late(Participant("U9", "veg", []), cand()) is True


def test_a_late_joiner_is_refused_when_the_restaurant_does_not_suit_them():
    assert lunch.can_join_late(Participant("U9", "jain", []), cand()) is False


def test_marking_placed_records_the_order_id_and_locks_the_stage():
    s = lunch.open_lunch("C1", "1.1")
    lunch.close_roll_call(s, a_pick())
    lunch.mark_placed(s, "ORD-1")
    assert s.stage == lunch.PLACED
    assert s.order_id == "ORD-1"


def test_joining_after_the_order_is_placed_is_rejected():
    s = lunch.open_lunch("C1", "1.1")
    lunch.close_roll_call(s, a_pick())
    lunch.mark_placed(s, "ORD-1")
    assert lunch.join(s, "U5") is False


def test_opening_a_second_lunch_replaces_the_first_for_that_channel():
    first = lunch.open_lunch("C1", "1.1")
    second = lunch.open_lunch("C1", "2.2")
    assert lunch.STORE["C1"] is second and second is not first
