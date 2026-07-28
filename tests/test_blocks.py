import json

from canteen import blocks


def action_ids(payload):
    return [e["action_id"]
            for b in payload for e in b.get("elements", [])
            if "action_id" in e]


def rendered(payload):
    # ensure_ascii would escape ₹ to ₹ and every rupee assertion would lie.
    return json.dumps(payload, ensure_ascii=False)


def test_connect_prompt_links_out_and_explains_the_broken_page():
    """The redirect deliberately fails to load. If we don't warn, people think
    the bot is broken and stop."""
    payload = blocks.connect_prompt("https://mcp.swiggy.com/auth/authorize?x=1")
    text = rendered(payload)
    assert "https://mcp.swiggy.com/auth/authorize?x=1" in text
    assert "won't load" in text or "will not load" in text
    assert "paste" in text.lower()


def test_confirm_purchase_puts_the_real_total_on_the_button():
    payload = blocks.confirm_purchase("food", 480, "Dosa and filter coffee")
    assert "confirm_purchase" in action_ids(payload)
    assert "cancel_purchase" in action_ids(payload)
    assert "₹480" in rendered(payload)
    assert "COD" in rendered(payload)


def test_confirm_purchase_carries_the_service_in_the_button_value():
    payload = blocks.confirm_purchase("instamart", 250, "Milk, bread")
    values = [e["value"] for b in payload for e in b.get("elements", [])
              if e.get("action_id") == "confirm_purchase"]
    assert values == ["instamart"]


def test_confirm_booking_states_date_time_and_party_size():
    payload = blocks.confirm_booking({
        "restaurant_id": "r1", "restaurant_name": "Toit", "slot_id": "s1",
        "date": "2026-08-01", "time": "8:00 PM", "guest_count": 6})
    text = rendered(payload)
    assert "Toit" in text and "2026-08-01" in text and "8:00 PM" in text
    assert "6" in text
    assert "confirm_booking" in action_ids(payload)


def test_group_food_names_the_host_and_who_has_joined():
    payload = blocks.group_food("U1", "Sattvik", ["Dosa ₹120"], 120, ["U1", "U2"])
    text = rendered(payload)
    assert "<@U1>" in text          # host is credited, and pays
    assert "<@U2>" in text
    assert "Sattvik" in text
    assert "₹120" in text
    assert "add_my_dish" in action_ids(payload)
    assert "place_group_order" in action_ids(payload)


def test_group_food_before_a_restaurant_is_chosen_offers_no_order_button():
    """Nothing may be ordered until there is a restaurant and a cart."""
    payload = blocks.group_food("U1", None, [], 0, ["U1"])
    assert "place_group_order" not in action_ids(payload)


def test_pantry_list_shows_every_item_and_the_total():
    payload = blocks.pantry_list(
        [{"spinId": "p1", "name": "Milk 1L", "quantity": 2, "price": 60},
         {"spinId": "p2", "name": "Coffee", "quantity": 1, "price": 240}], 360)
    text = rendered(payload)
    assert "Milk 1L" in text and "Coffee" in text
    assert "₹360" in text
    assert "confirm_purchase" in action_ids(payload)


def test_table_options_render_one_button_per_slot():
    payload = blocks.table_options([
        {"restaurant_id": "r1", "restaurant_name": "Toit", "slot_id": "s1",
         "date": "2026-08-01", "time": "7:00 PM", "guest_count": 6},
        {"restaurant_id": "r1", "restaurant_name": "Toit", "slot_id": "s2",
         "date": "2026-08-01", "time": "8:00 PM", "guest_count": 6},
    ])
    values = [e["value"] for b in payload for e in b.get("elements", [])
              if e.get("action_id") == "pick_slot"]
    assert len(values) == 2
    assert json.loads(values[0])["slot_id"] == "s1"


def test_every_button_value_stays_within_slack_limits():
    """Slack rejects an action value over 2000 characters, and the failure is a
    silent 400 at post time."""
    payload = blocks.table_options([
        {"restaurant_id": "r" * 50, "restaurant_name": "n" * 200,
         "slot_id": "s" * 50, "date": "2026-08-01", "time": "8:00 PM",
         "guest_count": 6}])
    for b in payload:
        for e in b.get("elements", []):
            assert len(e.get("value", "")) <= 2000
