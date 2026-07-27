import json

import pytest

from canteen import agent


def test_every_server_is_declared_as_one_mcp_tool_with_the_token():
    tools = agent.mcp_tools("tok-abc", ["food", "im", "dineout"])
    assert {t["server_url"] for t in tools} == {
        "https://mcp.swiggy.com/food",
        "https://mcp.swiggy.com/im",
        "https://mcp.swiggy.com/dineout",
    }
    assert all(t["type"] == "mcp" for t in tools)
    assert all(t["authorization"] == "tok-abc" for t in tools)
    # Approval is never delegated to the model — the Slack button is the gate.
    assert all(t["require_approval"] == "never" for t in tools)


def test_unknown_server_name_is_rejected_loudly():
    with pytest.raises(KeyError):
        agent.mcp_tools("tok", ["desserts"])


def test_spend_tools_are_the_three_that_move_money():
    assert agent.SPEND_TOOLS == {"place_food_order", "checkout", "book_table"}


def test_spending_tools_are_invisible_by_default():
    """The payment gate. Not a prompt instruction — the API is never told these
    tools exist unless a human clicked a button."""
    allowed = {t for tool in agent.mcp_tools("tok", ["food", "im", "dineout"])
               for t in tool["allowed_tools"]}
    assert not (allowed & agent.SPEND_TOOLS)
    assert "search_restaurants" in allowed  # everything else still there


def test_spending_tools_appear_only_when_the_caller_authorises():
    allowed = {t for tool in agent.mcp_tools("tok", ["food", "im", "dineout"],
                                             allow_spend=True)
               for t in tool["allowed_tools"]}
    assert agent.SPEND_TOOLS <= allowed


def test_run_only_exposes_spend_tools_for_the_authorised_preamble():
    """Regression guard: the gate keys off AUTHORISED, so a handler that forgets
    it gets a read-only agent rather than a silent purchase."""
    seen = []

    class FakeResponse:
        output = []
        output_text = "done"

    class FakeClient:
        class responses:
            @staticmethod
            def create(**kw):
                seen.append(kw)
                return FakeResponse()

    agent.run(FakeClient(), prompt="p", token="t", servers=["food"], ctx={})
    agent.run(FakeClient(), prompt="p", token="t", servers=["food"], ctx={},
              extra_system=agent.AUTHORISED)

    unauthorised, authorised = (kw["tools"][0]["allowed_tools"] for kw in seen)
    assert "place_food_order" not in unauthorised
    assert "place_food_order" in authorised


def test_every_listed_server_tool_belongs_to_exactly_one_server():
    """allowed_tools is hand-maintained, so a copy-paste slip must be caught."""
    for name, (label, url) in agent.SERVERS.items():
        assert agent.SERVER_TOOLS[name], name
    assert set(agent.SERVER_TOOLS) == set(agent.SERVERS)
    counts = {"food": 14, "im": 13, "dineout": 8}
    assert {k: len(v) for k, v in agent.SERVER_TOOLS.items()} == counts


def test_local_tool_schemas_are_well_formed():
    for tool in agent.LOCAL_TOOLS:
        assert tool["type"] == "function"
        assert tool["name"]
        assert tool["description"]
        assert tool["parameters"]["type"] == "object"


def test_run_feeds_local_tool_results_back_as_function_call_output():
    class Call:
        type = "function_call"
        call_id = "fc_1"
        name = "get_policy"
        arguments = '{}'

    turns = []

    class FakeClient:
        class responses:
            @staticmethod
            def create(**kw):
                turns.append(kw["input"])
                if len(turns) == 1:
                    return type("R", (), {"output": [Call()], "output_text": ""})()
                return type("R", (), {"output": [], "output_text": "cap is 250"})()

    out = agent.run(FakeClient(), prompt="what is the cap?", token="t",
                    servers=["food"], ctx={"get_policy": lambda: {"cap": 250}})

    assert out == "cap is 250"
    result = turns[1][-1]
    assert result["type"] == "function_call_output"
    assert result["call_id"] == "fc_1"
    assert json.loads(result["output"]) == {"cap": 250}


def test_run_gives_up_rather_than_looping_forever():
    class Call:
        type = "function_call"
        call_id = "fc_1"
        name = "get_policy"
        arguments = '{}'

    calls = []

    class FakeClient:
        class responses:
            @staticmethod
            def create(**kw):
                calls.append(1)
                return type("R", (), {"output": [Call()], "output_text": ""})()

    out = agent.run(FakeClient(), prompt="p", token="t", servers=["food"],
                    ctx={"get_policy": lambda: "x"})
    assert len(calls) == agent.MAX_TURNS
    assert "stuck" in out


def test_run_survives_malformed_tool_arguments():
    class Call:
        type = "function_call"
        call_id = "fc_1"
        name = "get_policy"
        arguments = '{not json'

    turns = []

    class FakeClient:
        class responses:
            @staticmethod
            def create(**kw):
                turns.append(kw["input"])
                if len(turns) == 1:
                    return type("R", (), {"output": [Call()], "output_text": ""})()
                return type("R", (), {"output": [], "output_text": "recovered"})()

    assert agent.run(FakeClient(), prompt="p", token="t", servers=["food"],
                     ctx={"get_policy": lambda: "x"}) == "recovered"
    assert "not valid JSON" in turns[1][-1]["output"]

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
