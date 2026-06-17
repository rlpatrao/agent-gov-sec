"""
tests/test_bedrock_gateway.py — the client-side apigw-bedrock model (WS5).

Exercises the LangChain ⇄ Bedrock Converse mapping and the model's request/response
handling with the HTTP POST mocked out — no AWS, no network. importorskip's
LangChain so it skips cleanly without the .[langgraph] extra.
"""

from __future__ import annotations

import pytest

pytest.importorskip("langchain_core")

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from payload_agents._runtime.bedrock_gateway import BedrockGatewayChatModel, _from_converse, _to_converse


def test_to_converse_extracts_system_and_maps_roles():
    system, msgs = _to_converse([
        SystemMessage(content="You are FinOps."),
        HumanMessage(content="show billing"),
    ])
    assert system == [{"text": "You are FinOps."}]
    assert msgs == [{"role": "user", "content": [{"text": "show billing"}]}]


def test_to_converse_maps_tool_call_and_result():
    ai = AIMessage(content="", tool_calls=[{"name": "query_billing", "args": {"cols": ["cost"]}, "id": "c1", "type": "tool_call"}])
    tool = ToolMessage(content='{"rows": 2}', tool_call_id="c1")
    _, msgs = _to_converse([HumanMessage(content="hi"), ai, tool])
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"][0]["toolUse"] == {"toolUseId": "c1", "name": "query_billing", "input": {"cols": ["cost"]}}
    assert msgs[2]["role"] == "user"
    assert msgs[2]["content"][0]["toolResult"]["toolUseId"] == "c1"


def test_to_converse_merges_consecutive_same_role():
    # Two tool results in a row must merge into one user message (Bedrock requires
    # strictly alternating roles).
    _, msgs = _to_converse([
        HumanMessage(content="hi"),
        ToolMessage(content="a", tool_call_id="t1"),
        ToolMessage(content="b", tool_call_id="t2"),
    ])
    user_turns = [m for m in msgs if m["role"] == "user"]
    assert len(user_turns) == 1
    assert len(user_turns[0]["content"]) == 3  # text + 2 toolResults


def test_from_converse_parses_text_and_tool_use():
    resp = {
        "output": {"message": {"content": [
            {"text": "Here are the costs."},
            {"toolUse": {"toolUseId": "u1", "name": "summarize_costs", "input": {"text": "x"}}},
        ]}},
        "stopReason": "tool_use",
        "usage": {"inputTokens": 10, "outputTokens": 5},
    }
    msg = _from_converse(resp).generations[0].message
    assert msg.content == "Here are the costs."
    assert msg.tool_calls == [{"name": "summarize_costs", "args": {"text": "x"}, "id": "u1", "type": "tool_call"}]
    assert msg.response_metadata["stop_reason"] == "tool_use"


def test_bind_tools_builds_converse_tool_config():
    m = BedrockGatewayChatModel(endpoint="https://gw/invoke", api_key="k")
    bound = m.bind_tools([{
        "type": "function",
        "function": {"name": "query_billing", "description": "read billing",
                     "parameters": {"type": "object", "properties": {"cols": {"type": "array"}}}},
    }])
    spec = bound.tool_config["tools"][0]["toolSpec"]
    assert spec["name"] == "query_billing"
    assert spec["inputSchema"]["json"]["properties"] == {"cols": {"type": "array"}}
    # bind_tools must not mutate the original
    assert m.tool_config is None


def test_generate_posts_converse_and_returns_aimessage(monkeypatch):
    m = BedrockGatewayChatModel(endpoint="https://gw/invoke", api_key="secret-key", max_tokens=128)
    captured = {}

    def fake_post(body):
        captured["body"] = body
        return {"output": {"message": {"content": [{"text": "Total: $4600"}]}}, "stopReason": "end_turn", "usage": {}}

    monkeypatch.setattr(m, "_post", fake_post)
    result = m.invoke([SystemMessage(content="sys"), HumanMessage(content="total cost?")])

    assert result.content == "Total: $4600"
    assert captured["body"]["system"] == [{"text": "sys"}]
    assert captured["body"]["messages"][0]["role"] == "user"
    assert captured["body"]["inferenceConfig"]["maxTokens"] == 128
