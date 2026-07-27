from canteen.brain import (
    ALLERGEN_CAVEAT,
    Candidate,
    Dish,
    Participant,
    Pick,
    Rejection,
    eatable_dishes,
    hard_filter,
    solve,
)

DAY = 86400.0
NOW = 1_800_000_000.0

NO_POLICY = {"per_head_cap": None, "vendor_allowlist": []}


def dish(name, price=150, veg=True, egg=False, jain=False):
    return Dish(name=name, price=price, veg=veg, contains_egg=egg, jain=jain)


def resto(rid, name, cuisines=("north",), eta=25, dishes=None, is_open=True, deliverable=True):
    return Candidate(
        id=rid, name=name, cuisines=list(cuisines), eta_minutes=eta,
        is_open=is_open, deliverable=deliverable,
        dishes=dishes if dishes is not None else [dish("Dal"), dish("Roti"), dish("Paneer")],
    )


def test_veg_user_cannot_be_served_meat():
    p = Participant("U1", "veg", [])
    c = resto("r1", "Grill", dishes=[dish("Chicken", veg=False), dish("Dal")])
    assert [d.name for d in eatable_dishes(p, c)] == ["Dal"]


def test_jain_user_needs_the_jain_tag_not_merely_veg():
    p = Participant("U1", "jain", [])
    c = resto("r1", "X", dishes=[dish("Aloo"), dish("Jain Thali", jain=True)])
    assert [d.name for d in eatable_dishes(p, c)] == ["Jain Thali"]


def test_egg_eater_accepts_veg_and_egg_but_not_meat():
    p = Participant("U1", "egg", [])
    c = resto("r1", "X", dishes=[dish("Omelette", veg=False, egg=True), dish("Dal"),
                                 dish("Mutton", veg=False)])
    assert sorted(d.name for d in eatable_dishes(p, c)) == ["Dal", "Omelette"]


def test_blocklist_keyword_removes_a_dish_case_insensitively():
    p = Participant("U1", "nonveg", ["Mushroom"])
    c = resto("r1", "X", dishes=[dish("Mushroom Masala"), dish("Dal")])
    assert [d.name for d in eatable_dishes(p, c)] == ["Dal"]


def test_hard_filter_rejects_restaurant_where_anyone_has_under_two_dishes():
    people = [Participant("U1", "veg", []), Participant("U2", "jain", [])]
    only_one_jain = resto("r1", "X", dishes=[dish("Dal"), dish("Roti"),
                                             dish("Jain Bowl", jain=True)])
    survivors, reason = hard_filter([only_one_jain], people, NO_POLICY)
    assert survivors == []
    assert "jain" in reason.lower()


def test_hard_filter_rejects_closed_undeliverable_and_slow():
    people = [Participant("U1", "nonveg", [])]
    closed = resto("r1", "A", is_open=False)
    undeliverable = resto("r2", "B", deliverable=False)
    slow = resto("r3", "C", eta=90)
    fine = resto("r4", "D")
    survivors, _ = hard_filter([closed, undeliverable, slow, fine], people, NO_POLICY)
    assert [c.id for c in survivors] == ["r4"]


def test_hard_filter_honours_the_vendor_allowlist():
    people = [Participant("U1", "nonveg", [])]
    policy = {"per_head_cap": None, "vendor_allowlist": ["r2"]}
    survivors, _ = hard_filter([resto("r1", "A"), resto("r2", "B")], people, policy)
    assert [c.id for c in survivors] == ["r2"]


def test_budget_cap_is_never_exceeded_when_a_compliant_option_exists():
    people = [Participant("U1", "nonveg", [])]
    policy = {"per_head_cap": 200, "vendor_allowlist": []}
    cheap = resto("cheap", "Cheap", dishes=[dish("A", 150), dish("B", 150), dish("C", 150)])
    posh = resto("posh", "Posh", dishes=[dish("A", 900), dish("B", 900), dish("C", 900)])
    result = solve([posh, cheap], people, policy, {}, [], NOW)
    assert isinstance(result, Pick)
    assert result.candidate.id == "cheap"


def test_repeat_penalty_rotates_away_from_yesterdays_restaurant():
    people = [Participant("U1", "nonveg", [])]
    a, b = resto("a", "A", cuisines=("north",)), resto("b", "B", cuisines=("north",))
    recent = [{"restaurant_id": "a", "cuisines": ["north"], "ordered_at": NOW - DAY}]
    result = solve([a, b], people, NO_POLICY, {}, recent, NOW)
    assert result.candidate.id == "b"


def test_repeat_penalty_decays_so_an_old_favourite_wins_again():
    people = [Participant("U1", "nonveg", [])]
    a, b = resto("a", "A"), resto("b", "B")
    recent = [{"restaurant_id": "a", "cuisines": ["north"], "ordered_at": NOW - 13 * DAY}]
    result = solve([a, b], people, NO_POLICY, {"a": 5.0, "b": 2.0}, recent, NOW)
    assert result.candidate.id == "a"


def test_ratings_break_a_tie():
    people = [Participant("U1", "nonveg", [])]
    result = solve([resto("a", "A"), resto("b", "B")], people, NO_POLICY,
                   {"a": 2.0, "b": 5.0}, [], NOW)
    assert result.candidate.id == "b"


def test_empty_candidate_set_returns_a_rejection_with_a_reason_not_an_exception():
    result = solve([], [Participant("U1", "veg", [])], NO_POLICY, {}, [], NOW)
    assert isinstance(result, Rejection)
    assert result.reason


def test_pick_carries_a_runner_up_and_per_person_dish_lists():
    people = [Participant("U1", "veg", []), Participant("U2", "nonveg", [])]
    result = solve([resto("a", "A"), resto("b", "B")], people, NO_POLICY, {"a": 5.0}, [], NOW)
    assert result.runner_up is not None and result.runner_up.id != result.candidate.id
    assert set(result.per_person_dishes) == {"U1", "U2"}
    assert all(len(v) >= 2 for v in result.per_person_dishes.values())


def test_single_candidate_has_no_runner_up():
    result = solve([resto("a", "A")], [Participant("U1", "nonveg", [])],
                   NO_POLICY, {}, [], NOW)
    assert result.runner_up is None


def test_reason_mentions_the_restaurant_and_a_price():
    result = solve([resto("a", "Sattvik")], [Participant("U1", "nonveg", [])],
                   NO_POLICY, {}, [], NOW)
    assert "Sattvik" in result.reason
    assert "150" in result.reason


def test_allergen_caveat_never_claims_safety():
    lowered = ALLERGEN_CAVEAT.lower()
    assert "safe" not in lowered
    assert "allergen" in lowered
