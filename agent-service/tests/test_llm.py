from types import SimpleNamespace as NS

import pytest

from app.agents import llm


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


def install(monkeypatch, responses):
    msgs = FakeMessages(responses)
    monkeypatch.setattr(llm, "_client", NS(beta=NS(messages=msgs)))
    return msgs


def text(s):
    return NS(type="text", text=s)


async def test_structured_output(monkeypatch):
    msgs = install(monkeypatch, [NS(stop_reason="end_turn", content=[text('{"a": 1}')])])
    assert await llm.structured_call(system="s", prompt="p", schema={}) == {"a": 1}
    req = msgs.requests[0]
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["fallbacks"] == "default"
    assert "tool_choice" not in req


async def test_tool_loop(monkeypatch):
    tool_use = NS(type="tool_use", id="t1", name="read_file", input={"path": "app/app.py"})
    msgs = install(monkeypatch, [
        NS(stop_reason="tool_use", content=[tool_use]),
        NS(stop_reason="end_turn", content=[text('{"done": true}')]),
    ])
    seen = []

    async def handler(name, args):
        seen.append((name, args))
        return "file contents"

    out = await llm.structured_call(system="s", prompt="p", schema={}, tools=[{}], tool_handler=handler)
    assert out == {"done": True} and seen == [("read_file", {"path": "app/app.py"})]
    last = msgs.requests[1]["messages"][2]
    assert last["content"][0]["tool_use_id"] == "t1" and last["content"][0]["content"] == "file contents"


async def test_tool_error_returned_to_model(monkeypatch):
    tool_use = NS(type="tool_use", id="t1", name="read_file", input={"path": "nope"})
    msgs = install(monkeypatch, [
        NS(stop_reason="tool_use", content=[tool_use]),
        NS(stop_reason="end_turn", content=[text("{}")]),
    ])

    async def handler(name, args):
        raise FileNotFoundError("nope")

    await llm.structured_call(system="s", prompt="p", schema={}, tools=[{}], tool_handler=handler)
    result = msgs.requests[1]["messages"][2]["content"][0]
    assert result["is_error"] is True


async def test_refusal_raises(monkeypatch):
    install(monkeypatch, [NS(stop_reason="refusal", content=[], stop_details=None)])
    with pytest.raises(llm.LLMRefusal):
        await llm.structured_call(system="s", prompt="p", schema={})
