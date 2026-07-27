import threading

from canteen import db


def fresh(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    return conn


def test_token_round_trip(tmp_path):
    conn = fresh(tmp_path)
    assert db.get_token(conn) is None
    db.save_token(conn, "acc", "ref", 1800000000.0)
    got = db.get_token(conn)
    assert got["access_token"] == "acc"
    assert got["refresh_token"] == "ref"
    assert got["expires_at"] == 1800000000.0


def test_token_save_replaces_rather_than_appends(tmp_path):
    conn = fresh(tmp_path)
    db.save_token(conn, "a", "r1", 1.0)
    db.save_token(conn, "b", "r2", 2.0)
    assert db.get_token(conn)["access_token"] == "b"
    assert conn.execute("select count(*) from swiggy_token").fetchone()[0] == 1


def test_profile_blocklist_survives_as_a_list(tmp_path):
    conn = fresh(tmp_path)
    db.upsert_profile(conn, "U1", "veg", ["paneer", "mushroom"], 250)
    p = db.get_profile(conn, "U1")
    assert p["diet"] == "veg"
    assert p["blocklist"] == ["paneer", "mushroom"]
    assert p["budget"] == 250


def test_get_profiles_returns_defaults_for_unknown_users(tmp_path):
    conn = fresh(tmp_path)
    db.upsert_profile(conn, "U1", "jain", [], 200)
    got = {p["user_id"]: p for p in db.get_profiles(conn, ["U1", "U2"])}
    assert got["U1"]["diet"] == "jain"
    assert got["U2"]["diet"] == "nonveg"
    assert got["U2"]["blocklist"] == []


def test_policy_has_defaults_when_unset(tmp_path):
    conn = fresh(tmp_path)
    pol = db.get_policy(conn, "C1")
    assert pol["per_head_cap"] is None
    assert pol["vendor_allowlist"] == []
    db.upsert_policy(conn, "C1", 250, ["r1", "r2"])
    pol = db.get_policy(conn, "C1")
    assert pol["per_head_cap"] == 250
    assert pol["vendor_allowlist"] == ["r1", "r2"]


def test_recent_orders_filters_by_timestamp(tmp_path):
    conn = fresh(tmp_path)
    db.record_order(conn, "C1", "r1", "Biryani Blues", ["north"], ["U1"], 800, 1000.0)
    db.record_order(conn, "C1", "r2", "Sattvik", ["south"], ["U1"], 600, 2000.0)
    assert [o["restaurant_id"] for o in db.recent_orders(conn, "C1", 1500.0)] == ["r2"]


def test_restaurant_ratings_averages_scores(tmp_path):
    conn = fresh(tmp_path)
    db.record_rating(conn, "U1", "r1", 5)
    db.record_rating(conn, "U2", "r1", 3)
    db.record_rating(conn, "U1", "r2", 4)
    assert db.restaurant_ratings(conn) == {"r1": 4.0, "r2": 4.0}


def test_connect_hands_each_thread_its_own_connection(tmp_path):
    """Slack Bolt runs every listener on a pool thread, and sqlite3 objects
    cannot cross threads. connect() must not share one handle."""
    path = str(tmp_path / "t.db")
    main_conn = db.connect(path)
    db.init_schema(main_conn)

    seen = {}

    def worker():
        seen["conn"] = db.connect(path)

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert seen["conn"] is not main_conn


def test_connect_reuses_the_same_connection_within_one_thread(tmp_path):
    path = str(tmp_path / "t.db")
    assert db.connect(path) is db.connect(path)


def test_a_write_on_one_thread_is_visible_from_another(tmp_path):
    path = str(tmp_path / "t.db")
    db.init_schema(db.connect(path))
    db.upsert_profile(db.connect(path), "U1", "veg", ["okra"], 200)

    seen = {}

    def worker():
        seen["profile"] = db.get_profile(db.connect(path), "U1")

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert seen["profile"]["diet"] == "veg"
    assert seen["profile"]["blocklist"] == ["okra"]


def test_par_levels_round_trip(tmp_path):
    conn = fresh(tmp_path)
    db.set_par_level(conn, "p1", "Milk 1L", 6)
    assert db.par_levels(conn) == {"p1": {"product_id": "p1", "name": "Milk 1L", "qty": 6}}
