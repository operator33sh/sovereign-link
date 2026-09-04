"""Tests voor llm._run_tool_loop — tool-call loop gedrag."""

import json
import pytest
from unittest.mock import patch, MagicMock

import llm
from llm import _run_tool_loop, _LOOP_OVERFLOW


def _make_response(content=None, tool_calls=None, finish_reason=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
        finish_reason = finish_reason or "tool_calls"
    else:
        finish_reason = finish_reason or "stop"
    return {
        "choices": [{"message": message, "finish_reason": finish_reason}]
    }


def test_run_tool_loop_no_tools():
    """Loop zonder tool-calls geeft direct de assistant-tekst terug."""
    response = _make_response(content="Hallo wereld")
    with patch.object(llm, "_chat", return_value=response):
        with patch.object(llm, "context", MagicMock()):
            messages = [{"role": "user", "content": "test"}]
            result = _run_tool_loop(messages, max_iter=5)
    assert result == "Hallo wereld"


def test_run_tool_loop_overflow():
    """Loop die tool-calls blijft doen geeft _LOOP_OVERFLOW terug."""
    tool_call = {
        "id": "tc1",
        "type": "function",
        "function": {"name": "unknown_tool", "arguments": "{}"},
    }
    response = _make_response(tool_calls=[tool_call])

    with patch.object(llm, "_chat", return_value=response):
        with patch.object(llm, "context", MagicMock()):
            with patch.dict(llm.TOOL_HANDLERS, {}, clear=True):
                messages = [{"role": "user", "content": "test"}]
                result = _run_tool_loop(messages, max_iter=3)

    assert result is _LOOP_OVERFLOW


def test_run_tool_loop_calls_handler():
    """Bekende tool-handler wordt aangeroepen; resultaat in messages gezet."""
    tool_call = {
        "id": "tc1",
        "type": "function",
        "function": {"name": "test_tool", "arguments": '{"x": 1}'},
    }
    first  = _make_response(tool_calls=[tool_call])
    second = _make_response(content="Klaar")

    mock_handler = MagicMock(return_value="handler-resultaat")

    call_count = 0
    def fake_chat(msgs):
        nonlocal call_count
        call_count += 1
        return first if call_count == 1 else second

    with patch.object(llm, "_chat", side_effect=fake_chat):
        with patch.object(llm, "context", MagicMock()):
            with patch.dict(llm.TOOL_HANDLERS, {"test_tool": mock_handler}):
                messages = [{"role": "user", "content": "test"}]
                result = _run_tool_loop(messages, max_iter=5)

    assert result == "Klaar"
    mock_handler.assert_called_once_with({"x": 1})
