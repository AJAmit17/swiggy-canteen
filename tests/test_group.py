import threading

from canteen import group


def test_classify_recognises_the_three_group_intents():
    assert group.classify("lunch") == group.FOOD
    assert group.classify("order lunch for the team") == group.FOOD
    assert group.classify("book a table for 8 at 8pm") == group.TABLE
    assert group.classify("dinner reservation tonight") == group.TABLE
    assert group.classify("restock the pantry") == group.PANTRY
    assert group.classify("we're out of coffee, groceries please") == group.PANTRY


def test_classify_leaves_plain_questions_to_the_model():
    assert group.classify("what's good around here?") is None
    assert group.classify("track my order") is None


def test_table_beats_food_when_both_words_appear():
    """'book a table for lunch' is a booking, not a group food order."""
    assert group.classify("book a table for lunch tomorrow") == group.TABLE


def test_joining_is_idempotent():
    context = {"joined": ["U1"]}
    once = group.join(context, "U2")
    twice = group.join(once, "U2")
    assert twice["joined"] == ["U1", "U2"]


def test_joining_preserves_the_rest_of_the_context():
    context = {"joined": ["U1"], "restaurantId": "r1"}
    assert group.join(context, "U2")["restaurantId"] == "r1"


def test_join_handles_a_context_that_has_no_joined_list_yet():
    assert group.join({}, "U1")["joined"] == ["U1"]


def test_the_same_channel_gets_the_same_cart_lock():
    """Joiners mutate one server-side cart, so their writes must serialise."""
    assert group.cart_lock("C1") is group.cart_lock("C1")
    assert group.cart_lock("C1") is not group.cart_lock("C2")
    assert isinstance(group.cart_lock("C1"), type(threading.Lock()))
