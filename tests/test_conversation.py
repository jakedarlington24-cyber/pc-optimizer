"""Tests for the Claude tool-use loop, using a fake client so no API key or network is needed."""

from types import SimpleNamespace

import pytest

import ask_claude
import pc_tools


def text(t):
    return SimpleNamespace(type="text", text=t)


def tool_use(tool_id, name, tool_input):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=tool_input)


def response(blocks, stop_reason):
    return SimpleNamespace(content=blocks, stop_reason=stop_reason)


class FakeClient:
    """Stands in for anthropic.Anthropic(): returns scripted responses in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


@pytest.fixture
def fake_tools(monkeypatch):
    calls = []
    monkeypatch.setitem(pc_tools.TOOL_FUNCTIONS, "get_basic_stats", lambda: "CPU usage: 5%")
    monkeypatch.setitem(pc_tools.TOOL_FUNCTIONS, "kill_process", lambda process_name: calls.append(process_name) or f"Closed {process_name}")
    monkeypatch.setitem(pc_tools.TOOL_FUNCTIONS, "disable_startup_program", lambda program_name: calls.append(program_name) or "Disabled")
    monkeypatch.setattr(ask_claude, "create_restore_point", lambda: calls.append("restore point") or (True, "ok"))
    return calls


def tool_results(conversation):
    return {r["tool_use_id"]: r["content"] for r in conversation[-2]["content"]}


def test_read_only_tools_run_and_declined_actions_never_run(monkeypatch, fake_tools):
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    client = FakeClient([
        response([text("Let me check."), tool_use("t1", "get_basic_stats", {}),
                  tool_use("t2", "kill_process", {"process_name": "Discord.exe"})], "tool_use"),
        response([text("Okay, I left it running.")], "end_turn"),
    ])
    conversation = [{"role": "user", "content": "close discord"}]

    ask_claude.run_conversation(client, conversation)

    results = tool_results(conversation)
    assert results["t1"] == "CPU usage: 5%"
    assert results["t2"] == "User declined this action."
    assert fake_tools == [], "a declined action must not run"
    assert conversation[-1]["role"] == "assistant"
    assert len(client.requests) == 2


def test_approved_action_runs_with_restore_point_when_needed(monkeypatch, fake_tools):
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    client = FakeClient([
        response([tool_use("t1", "disable_startup_program", {"program_name": "Discord"})], "tool_use"),
        response([text("Done.")], "end_turn"),
    ])
    conversation = [{"role": "user", "content": "stop discord at startup"}]

    ask_claude.run_conversation(client, conversation)

    assert fake_tools == ["restore point", "Discord"]
    assert tool_results(conversation)["t1"].startswith("Disabled")


def test_every_request_sends_the_system_prompt_and_tools_with_caching(monkeypatch, fake_tools):
    client = FakeClient([response([text("Hi!")], "end_turn")])
    ask_claude.run_conversation(client, [{"role": "user", "content": "hello"}])
    request = client.requests[0]
    assert request["system"][0]["text"] == pc_tools.SYSTEM_PROMPT
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}, "tools + system should be cached"
    assert request["cache_control"] == {"type": "ephemeral"}, "the conversation should be cached automatically"
    assert request["tools"] == pc_tools.TOOLS
    assert request["model"] == pc_tools.MODEL


def test_cached_prefix_is_identical_between_requests():
    # Caching only works if everything before the conversation is byte-for-byte the same every time
    first = pc_tools.api_request([{"role": "user", "content": "a"}])
    second = pc_tools.api_request([{"role": "user", "content": "b"}])
    for key in ("model", "tools", "system"):
        assert repr(first[key]) == repr(second[key])


def test_cost_estimate_applies_cache_prices():
    usage = SimpleNamespace(input_tokens=1_000_000, output_tokens=100_000,
                            cache_creation_input_tokens=1_000_000, cache_read_input_tokens=10_000_000)
    # $2 uncached + $2.50 cache write + $2 cache read (10M at 0.1x) + $1 output
    assert ask_claude.estimate_cost(usage) == pytest.approx(7.50)
    assert ask_claude.estimate_cost(SimpleNamespace(input_tokens=500, output_tokens=0,
                                                    cache_creation_input_tokens=None,
                                                    cache_read_input_tokens=None)) == pytest.approx(0.001)
    assert ask_claude.estimate_cost(None) == 0.0


def test_run_conversation_returns_total_cost():
    usage = SimpleNamespace(input_tokens=1000, output_tokens=0)
    client = FakeClient([SimpleNamespace(content=[text("Hi")], stop_reason="end_turn", usage=usage)])
    assert ask_claude.run_conversation(client, [{"role": "user", "content": "hi"}]) == pytest.approx(0.002)
