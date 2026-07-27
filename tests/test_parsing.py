from canteen.parsing import close_time, parse_profile, to_candidates


def test_parses_diet_blocklist_and_budget_from_one_line():
    p = parse_profile("veg, no mushroom, no paneer, 250")
    assert p == {"diet": "veg", "blocklist": ["mushroom", "paneer"], "budget": 250}


def test_diet_defaults_to_nonveg_when_unstated():
    assert parse_profile("no coriander")["diet"] == "nonveg"


def test_hyphenated_non_veg_is_understood():
    assert parse_profile("non-veg")["diet"] == "nonveg"


def test_budget_tolerates_a_rupee_symbol_or_prefix():
    assert parse_profile("veg, ₹300")["budget"] == 300
    assert parse_profile("veg, rs 300")["budget"] == 300


def test_unrecognised_words_become_blocklist_entries_rather_than_being_dropped():
    assert parse_profile("veg, brinjal")["blocklist"] == ["brinjal"]


def test_empty_input_is_a_permissive_profile_not_a_crash():
    assert parse_profile("") == {"diet": "nonveg", "blocklist": [], "budget": None}


def test_to_candidates_maps_the_agent_json_onto_the_solver_types():
    out = to_candidates([{
        "id": "r1", "name": "Sattvik", "cuisines": ["south"], "eta_minutes": 20,
        "is_open": True, "deliverable": True,
        "dishes": [{"name": "Dosa", "price": 120, "veg": True}],
    }])
    assert out[0].id == "r1"
    assert out[0].dishes[0].name == "Dosa"
    assert out[0].dishes[0].veg is True
    assert out[0].dishes[0].jain is False


def test_to_candidates_accepts_restaurant_id_as_an_alias_for_id():
    assert to_candidates([{"restaurant_id": "r9", "name": "X"}])[0].id == "r9"


def test_to_candidates_defaults_missing_availability_to_permissive():
    c = to_candidates([{"id": "r1", "name": "X"}])[0]
    assert c.is_open is True and c.deliverable is True and c.eta_minutes == 30


def test_to_candidates_drops_nameless_dishes_without_crashing():
    c = to_candidates([{"id": "r1", "name": "X",
                        "dishes": [{"price": 100}, {"name": "Dal", "price": 90}]}])[0]
    assert [d.name for d in c.dishes] == ["Dal"]


def test_close_time_rolls_over_the_hour():
    assert close_time("11:30", 30) == "12:00"
    assert close_time("11:45", 30) == "12:15"
    assert close_time("23:50", 30) == "00:20"
