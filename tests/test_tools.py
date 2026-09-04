"""Tests voor tools package — definities en handlers."""

import pytest
from tools import TOOL_DEFINITIONS, TOOL_HANDLERS, AGENT_TOOL_DEFINITIONS


def test_tool_counts_match():
    assert len(TOOL_DEFINITIONS) == len(TOOL_HANDLERS)


def test_every_definition_has_handler():
    missing = [t["function"]["name"] for t in TOOL_DEFINITIONS
               if t["function"]["name"] not in TOOL_HANDLERS]
    assert missing == [], f"Geen handler voor: {missing}"


def test_agent_tools_subset_of_tools():
    tool_names = {t["function"]["name"] for t in TOOL_DEFINITIONS}
    agent_names = {t["function"]["name"] for t in AGENT_TOOL_DEFINITIONS}
    extra = agent_names - tool_names
    assert extra == set(), f"Agent-tools niet in TOOL_DEFINITIONS: {extra}"


def test_tool_schema_structure():
    for t in TOOL_DEFINITIONS:
        assert t.get("type") == "function"
        fn = t["function"]
        assert "name" in fn
        assert "description" in fn
        assert "parameters" in fn
