from canteen import blocks
from canteen.brain import ALLERGEN_CAVEAT, Candidate, Dish, Pick


def d(name, price=150):
    return Dish(name=name, price=price, veg=True)


def cand(cid="r1", name="Sattvik"):
    return Candidate(id=cid, name=name, cuisines=["south"], eta_minutes=25,
                     is_open=True, deliverable=True, dishes=[d("Dosa"), d("Idli")])


def a_pick(runner_up=None):
    c = cand()
    return Pick(candidate=c, score=4.2, reason="*Sattvik* — ~₹150/head",
                runner_up=runner_up, per_person_dishes={"U1": c.dishes})


def action_ids(bs):
    ids = []
    for b in bs:
        for e in b.get("elements", []):
            if isinstance(e, dict) and "action_id" in e:
                ids.append(e["action_id"])
        acc = b.get("accessory")
        if isinstance(acc, dict) and "action_id" in acc:
            ids.append(acc["action_id"])
    return ids


def all_blocks_have_a_type(bs):
    return all("type" in b for b in bs)


def test_roll_call_has_a_join_button_and_states_the_deadline():
    bs = blocks.roll_call("12:00")
    assert all_blocks_have_a_type(bs)
    assert "join_lunch" in action_ids(bs)
    assert "12:00" in str(bs)


def test_pick_message_offers_veto_and_shows_the_reason():
    bs = blocks.pick_message(a_pick(runner_up=cand("r2", "Toit")), ["U1"], 300)
    assert "veto_pick" in action_ids(bs)
    assert "Sattvik" in str(bs)
    assert "Toit" in str(bs)


def test_pick_message_omits_veto_when_there_is_no_runner_up():
    assert "veto_pick" not in action_ids(blocks.pick_message(a_pick(), ["U1"], 300))


def test_pick_message_omits_veto_once_the_window_has_closed():
    bs = blocks.pick_message(a_pick(runner_up=cand("r2", "Toit")), ["U1"], 0)
    assert "veto_pick" not in action_ids(bs)


def test_dish_picker_is_a_select_carrying_every_dish():
    bs = blocks.dish_picker([d("Dosa", 120), d("Idli", 80)])
    text = str(bs)
    assert "Dosa" in text and "Idli" in text
    assert "120" in text and "80" in text


def test_dish_picker_carries_the_allergen_caveat_verbatim():
    assert ALLERGEN_CAVEAT in str(blocks.dish_picker([d("Dosa")]))


def test_dish_picker_truncates_to_the_slack_option_limit():
    bs = blocks.dish_picker([d(f"Dish {i}") for i in range(120)])
    opts = [
        o
        for b in bs
        for e in b.get("elements", [])
        if isinstance(e, dict)
        for o in e.get("options", [])
    ]
    assert len(opts) <= 100


def test_dish_picker_says_so_when_nothing_is_eatable():
    bs = blocks.dish_picker([])
    assert all_blocks_have_a_type(bs)
    assert "choose_dish" not in action_ids(bs)


def test_confirm_shows_the_total_and_a_place_order_button():
    bs = blocks.confirm("Sattvik", ["<@U1> Dosa ₹120"], 120)
    assert "place_order" in action_ids(bs)
    assert "120" in str(bs)


def test_tracking_and_rate_prompt_render():
    assert all_blocks_have_a_type(blocks.tracking("Sattvik", "On the way", "12 min"))
    assert "rate_5" in action_ids(blocks.rate_prompt("r1", "Sattvik"))


def test_pantry_approval_lists_items_and_gates_on_a_button():
    bs = blocks.pantry_approval([{"name": "Milk 1L", "qty": 4, "price": 240}], 240)
    assert "approve_pantry" in action_ids(bs)
    assert "Milk 1L" in str(bs)


def test_dineout_options_render_one_button_per_option():
    bs = blocks.dineout_options([
        {"restaurant_id": "r1", "restaurant_name": "Toit", "slot_id": "s1", "time": "8:00 PM"},
        {"restaurant_id": "r2", "restaurant_name": "Fatty Bao", "slot_id": "s2",
         "time": "8:30 PM"},
    ])
    assert action_ids(bs).count("book_slot") == 2


def test_rejection_explains_rather_than_apologises_blankly():
    bs = blocks.rejection("Nothing worked: Sattvik is closed.")
    assert "Sattvik is closed" in str(bs)
