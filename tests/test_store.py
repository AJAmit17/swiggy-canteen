import threading

from canteen import store


def fresh(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_schema(conn)
    return conn


def test_tokens_are_per_user(tmp_path):
    conn = fresh(tmp_path)
    store.save_token(conn, "U1", "acc-1", "ref-1", 1800000000.0)
    store.save_token(conn, "U2", "acc-2", "ref-2", 1800000001.0)
    assert store.get_token(conn, "U1")["access_token"] == "acc-1"
    assert store.get_token(conn, "U2")["access_token"] == "acc-2"
    assert store.get_token(conn, "U3") is None


def test_saving_a_token_twice_replaces_it(tmp_path):
    conn = fresh(tmp_path)
    store.save_token(conn, "U1", "old", "r", 1.0)
    store.save_token(conn, "U1", "new", "r", 2.0)
    assert store.get_token(conn, "U1")["access_token"] == "new"
    assert conn.execute("select count(*) from swiggy_token").fetchone()[0] == 1


def test_deleting_a_token_forces_a_reconnect(tmp_path):
    conn = fresh(tmp_path)
    store.save_token(conn, "U1", "acc", "ref", 1.0)
    store.delete_token(conn, "U1")
    assert store.get_token(conn, "U1") is None


def test_pending_auth_can_only_be_taken_once(tmp_path):
    """The auth code is single-use, so the record that authorises it must be
    too — otherwise a replayed paste re-enters the exchange."""
    conn = fresh(tmp_path)
    store.save_pending(conn, "U1", "verifier", "state-abc", 1000.0)
    first = store.take_pending(conn, "U1")
    assert first["verifier"] == "verifier"
    assert first["state"] == "state-abc"
    assert store.take_pending(conn, "U1") is None


def test_starting_a_second_link_replaces_the_first(tmp_path):
    conn = fresh(tmp_path)
    store.save_pending(conn, "U1", "v1", "s1", 1000.0)
    store.save_pending(conn, "U1", "v2", "s2", 2000.0)
    assert store.take_pending(conn, "U1")["state"] == "s2"


def test_preferences_round_trip(tmp_path):
    conn = fresh(tmp_path)
    assert store.get_preference(conn, "U1") is None
    store.set_preference(conn, "U1", "vegetarian, no mushroom")
    store.set_preference(conn, "U1", "vegetarian, no mushroom, ~300")
    assert store.get_preference(conn, "U1") == "vegetarian, no mushroom, ~300"


def test_history_round_trips_and_clears(tmp_path):
    conn = fresh(tmp_path)
    assert store.get_history(conn, "D1") is None
    messages = [{"role": "user", "content": "hi"},
                {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}]
    store.set_history(conn, "D1", messages, 1000.0)
    assert store.get_history(conn, "D1") == messages
    store.clear_history(conn, "D1")
    assert store.get_history(conn, "D1") is None


def test_group_context_survives_as_a_dict(tmp_path):
    conn = fresh(tmp_path)
    store.save_group(conn, "C1", "food", "U1", "1.1",
                     {"restaurantId": "r1", "joined": ["U1"]}, 1000.0)
    got = store.get_group(conn, "C1")
    assert got["kind"] == "food"
    assert got["host_user_id"] == "U1"
    assert got["message_ts"] == "1.1"
    assert got["context"] == {"restaurantId": "r1", "joined": ["U1"]}


def test_group_context_can_be_updated_without_losing_the_row(tmp_path):
    conn = fresh(tmp_path)
    store.save_group(conn, "C1", "food", "U1", "1.1", {"joined": []}, 1000.0)
    store.set_group_context(conn, "C1", {"joined": ["U1", "U2"]})
    got = store.get_group(conn, "C1")
    assert got["context"]["joined"] == ["U1", "U2"]
    assert got["host_user_id"] == "U1"


def test_only_one_group_flow_per_channel(tmp_path):
    conn = fresh(tmp_path)
    store.save_group(conn, "C1", "food", "U1", "1.1", {}, 1000.0)
    store.save_group(conn, "C1", "table", "U2", "2.2", {}, 2000.0)
    assert store.get_group(conn, "C1")["kind"] == "table"
    assert conn.execute("select count(*) from group_order").fetchone()[0] == 1


def test_deleting_a_group_ends_the_flow(tmp_path):
    conn = fresh(tmp_path)
    store.save_group(conn, "C1", "food", "U1", "1.1", {}, 1000.0)
    store.delete_group(conn, "C1")
    assert store.get_group(conn, "C1") is None


def test_bot_thread_round_trips(tmp_path):
    conn = fresh(tmp_path)
    assert store.is_bot_thread(conn, "C1", "111.1") is False
    store.mark_bot_thread(conn, "C1", "111.1", 1000.0)
    assert store.is_bot_thread(conn, "C1", "111.1") is True


def test_bot_thread_marking_twice_does_not_duplicate(tmp_path):
    conn = fresh(tmp_path)
    store.mark_bot_thread(conn, "C1", "111.1", 1000.0)
    store.mark_bot_thread(conn, "C1", "111.1", 2000.0)
    assert conn.execute("select count(*) from bot_thread").fetchone()[0] == 1


def test_bot_thread_does_not_leak_across_channels(tmp_path):
    conn = fresh(tmp_path)
    store.mark_bot_thread(conn, "C1", "111.1", 1000.0)
    assert store.is_bot_thread(conn, "C2", "111.1") is False


def test_connect_hands_each_thread_its_own_connection(tmp_path):
    """Bolt runs every listener on a pool thread and sqlite3 objects cannot
    cross threads."""
    path = str(tmp_path / "t.db")
    main_conn = store.connect(path)
    store.init_schema(main_conn)
    seen = {}

    def worker():
        seen["conn"] = store.connect(path)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert seen["conn"] is not main_conn


def test_a_write_on_one_thread_is_visible_from_another(tmp_path):
    path = str(tmp_path / "t.db")
    store.init_schema(store.connect(path))
    store.save_token(store.connect(path), "U1", "acc", "ref", 1.0)
    seen = {}

    def worker():
        seen["token"] = store.get_token(store.connect(path), "U1")

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert seen["token"]["access_token"] == "acc"
