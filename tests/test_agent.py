import pytest

from canteen import agent


def test_every_declared_server_has_exactly_one_matching_toolset():
    servers = agent.mcp_servers("tok", ["food", "im"])
    tools = agent.toolsets(["food", "im"])
    declared = {s["name"] for s in servers}
    referenced = [t["mcp_server_name"] for t in tools]
    assert declared == set(referenced)
    assert len(referenced) == len(set(referenced))


def test_servers_carry_the_token_and_the_real_swiggy_urls():
    servers = agent.mcp_servers("tok-abc", ["food", "im", "dineout"])
    assert {s["url"] for s in servers} == {
        "https://mcp.swiggy.com/food",
        "https://mcp.swiggy.com/im",
        "https://mcp.swiggy.com/dineout",
    }
    assert all(s["authorization_token"] == "tok-abc" for s in servers)
    assert all(s["type"] == "url" for s in servers)


def test_unknown_server_name_is_rejected_loudly():
    with pytest.raises(KeyError):
        agent.mcp_servers("tok", ["desserts"])


def test_spend_tools_are_the_three_that_move_money():
    assert agent.SPEND_TOOLS == {"place_food_order", "checkout", "book_table"}


def test_local_tool_schemas_are_well_formed():
    for tool in agent.LOCAL_TOOLS:
        assert tool["name"]
        assert tool["description"]
        assert tool["input_schema"]["type"] == "object"


def test_dispatch_local_routes_to_the_context_callable():
    calls = []
    ctx = {"record_rating": lambda **kw: calls.append(kw) or "ok"}
    out = agent.dispatch_local("record_rating", {"restaurant_id": "r1", "score": 5}, ctx)
    assert out == "ok"
    assert calls == [{"restaurant_id": "r1", "score": 5}]


def test_dispatch_local_returns_an_error_string_rather_than_raising():
    out = agent.dispatch_local("nope", {}, {})
    assert "unknown tool" in out.lower()


def test_dispatch_local_reports_a_tool_exception_as_text():
    def boom(**kw):
        raise ValueError("kaboom")

    out = agent.dispatch_local("record_rating", {}, {"record_rating": boom})
    assert "kaboom" in out


def test_dispatch_local_json_encodes_non_string_results():
    out = agent.dispatch_local("get_policy", {}, {"get_policy": lambda: {"cap": 250}})
    assert out == '{"cap": 250}'
