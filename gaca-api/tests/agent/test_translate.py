"""Unit tests for the Hermes -> GacaEvent translator (offline, deterministic).

The raw dicts below mirror shapes captured live from hermes-agent 0.18.0.
"""
from gaca_api.agent.hermes.translate import translate
from gaca_api.schemas.event import (
    AssistantDelta,
    AssistantReasoning,
    RunCompleted,
    RunFailed,
    ToolCompleted,
    ToolStarted,
)

SID, RID = "sess-1", "our-run-1"


def _t(raw):
    return translate(raw, session_id=SID, run_id=RID)


def test_message_delta_maps_to_assistant_delta():
    ev = _t({"event": "message.delta", "timestamp": 1783194662.07, "delta": "Hey"})
    assert isinstance(ev, AssistantDelta)
    assert ev.text == "Hey"
    assert ev.session_id == SID and ev.run_id == RID
    assert ev.seq is None  # the core stamps seq, not the translator


def test_reasoning_available_is_renamed():
    ev = _t({"event": "reasoning.available", "text": "thinking"})
    assert isinstance(ev, AssistantReasoning)
    assert ev.type == "assistant.reasoning"
    assert ev.text == "thinking"


def test_tool_started_renames_preview_to_args_preview():
    ev = _t({"event": "tool.started", "tool": "memory", "preview": '+user: "Name: David"'})
    assert isinstance(ev, ToolStarted)
    assert ev.tool == "memory"
    assert ev.args_preview == '+user: "Name: David"'


def test_tool_completed_carries_timing_and_error():
    ev = _t({"event": "tool.completed", "tool": "memory", "duration": 0.004, "error": False})
    assert isinstance(ev, ToolCompleted)
    assert ev.duration == 0.004
    assert ev.error is False


def test_run_completed_types_usage():
    ev = _t(
        {
            "event": "run.completed",
            "output": "Hi",
            "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        }
    )
    assert isinstance(ev, RunCompleted)
    assert ev.output == "Hi"
    assert ev.usage.total_tokens == 12  # dict got wrapped in our typed Usage


def test_run_completed_without_usage():
    ev = _t({"event": "run.completed", "output": "Hi"})
    assert isinstance(ev, RunCompleted)
    assert ev.usage is None


def test_run_failed_maps():
    ev = _t({"event": "run.failed", "error": "boom"})
    assert isinstance(ev, RunFailed)
    assert ev.error == "boom"


def test_timestamp_preserved_as_utc():
    ev = _t({"event": "message.delta", "timestamp": 1783194662.07, "delta": "x"})
    assert ev.created_at.tzinfo is not None
    assert ev.created_at.utcoffset().total_seconds() == 0


def test_missing_timestamp_falls_back_to_default():
    ev = _t({"event": "message.delta", "delta": "x"})  # no timestamp
    assert ev.created_at is not None


def test_unknown_event_is_dropped_not_leaked():
    assert _t({"event": "approval.request", "foo": 1}) is None
    assert _t({"event": "totally.unknown"}) is None
