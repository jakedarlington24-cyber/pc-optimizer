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


def test_every_request_sends_the_system_prompt_and_tools(monkeypatch, fake_tools):
    client = FakeClient([response([text("Hi!")], "end_turn")])
    ask_claude.run_conversation(client, [{"role": "user", "content": "hello"}])
    request = client.requests[0]
    assert request["system"] == pc_tools.SYSTEM_PROMPT
    assert request["tools"] == pc_tools.TOOLS
    assert request["model"] == pc_tools.MODEL
