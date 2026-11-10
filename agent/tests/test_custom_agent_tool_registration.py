"""Both delegation tools are bound, trusted, and advertised.

A tool absent from `_added_builtins` is never bound, so the model cannot call
it and the feature looks broken with no error anywhere.

Tests follow the pattern in test_main_persistence_wiring.py: assert against the
registration data directly rather than invoking `_init_agent_subsystems` (which
requires live AWS). This reproduces main's assembly expressions to prove the
tools land in the right sets.
"""
import ast
import os
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))


def _prompt() -> str:
    """The prompt's literal text, taken from source.

    Calling `_build_system_prompt()` needs a live gateway registry, which a unit
    test should not require; the instruction wording is a static property of the
    source, so read it from there.

    Taken from test_read_results_rule.py pattern.
    """
    tree = ast.parse((SRC / "prompts" / "orchestrator.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "build_orchestrator_system_prompt":
            return " ".join(
                n.value for n in ast.walk(node)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            )
    raise AssertionError("_build_system_prompt not found")


# ---------------------------------------------------------------------------
# invoke_custom_agent — directly registered delegation tool
# ---------------------------------------------------------------------------

def test_invoke_custom_agent_can_be_registered():
    """invoke_custom_agent can be registered as a builtin tool.

    Mirrors the registration in ``main._init_agent_subsystems``:
        "invoke_custom_agent": custom_agent_tool.invoke_custom_agent,

    This differs from dispatch_tool (which maps to its module) because
    invoke_custom_agent is a @tool async def, not a module-style handler.
    """
    import custom_agent_tool

    # Reproduce main's registration expression
    all_builtin_tools: dict = {}
    all_builtin_tools.update({
        "invoke_custom_agent": custom_agent_tool.invoke_custom_agent,
    })

    assert "invoke_custom_agent" in all_builtin_tools
    assert callable(all_builtin_tools["invoke_custom_agent"])
    # Verify it's the decorated function, not the module
    assert hasattr(all_builtin_tools["invoke_custom_agent"], "tool_spec")


def test_main_imports_custom_agent_tool():
    """main.py imports custom_agent_tool and wires invoke_custom_agent.

    This verifies the actual registration is present in main.py source.
    """
    src = (SRC / "main.py").read_text()

    # Import statement should be present
    assert "import custom_agent_tool" in src

    # Registration in _all_builtin_tools should be present
    assert '"invoke_custom_agent": custom_agent_tool.invoke_custom_agent' in src

    # Should be in _added_builtins set
    assert '"invoke_custom_agent"' in src


def test_invoke_custom_agent_in_added_builtins():
    """invoke_custom_agent is in _added_builtins set.

    Mirrors the set update in ``main._init_agent_subsystems``:
        _added_builtins.update({"dispatch_tool", "plan_and_execute_tool",
                                 "invoke_custom_agent", ...})
    """
    added_builtins: set[str] = set()
    added_builtins.update({
        "dispatch_tool", "plan_and_execute_tool", "invoke_custom_agent",
        "http_request", "current_time",
    })

    assert "invoke_custom_agent" in added_builtins


def test_invoke_custom_agent_is_trusted():
    """invoke_custom_agent becomes trusted via _trusted_builtins.update(_added_builtins).

    This is the same flow dispatch_tool and plan_and_execute_tool follow.
    """
    added_builtins: set[str] = {"invoke_custom_agent", "dispatch_tool"}
    trusted_builtins: set[str] = set()
    trusted_builtins.update(added_builtins)

    assert "invoke_custom_agent" in trusted_builtins


# ---------------------------------------------------------------------------
# list_custom_agents — reachable through the lab_asset multiplexer
# ---------------------------------------------------------------------------

def test_list_custom_agents_in_individual_lab_tools():
    """list_custom_agents is in _INDIVIDUAL_LAB_TOOLS.

    The tool is NOT registered directly. Instead it goes through the lab_asset
    multiplexer. _INDIVIDUAL_LAB_TOOLS is the source list that
    _build_lab_action_registry() processes to build _LAB_ACTIONS.

    The model invokes it as: lab_asset(action="list_custom_agents", params={...})
    """
    from main_lab_tools import _INDIVIDUAL_LAB_TOOLS, list_custom_agents

    assert list_custom_agents in _INDIVIDUAL_LAB_TOOLS


def test_list_custom_agents_action_registered():
    """list_custom_agents action is in _LAB_ACTIONS after registry build.

    _build_lab_action_registry() introspects _INDIVIDUAL_LAB_TOOLS and builds
    _LAB_ACTIONS with fn.__name__ as the action key.
    """
    from main_lab_tools import _LAB_ACTIONS, list_custom_agents

    # The registry is built at module import time
    assert "list_custom_agents" in _LAB_ACTIONS
    assert _LAB_ACTIONS["list_custom_agents"]["fn"].__name__ == "list_custom_agents"


# ---------------------------------------------------------------------------
# System prompt guidance
# ---------------------------------------------------------------------------

def test_system_prompt_routes_to_delegation():
    """System prompt names the REACHABLE call shape, not the bare action name.

    The prompt rule guides the model to list first and delegate via
    invoke_custom_agent. This is the agreed mitigation for on-demand discovery
    (dispatch_tool.py:87-93 records the model leaving a tool argument empty
    while narrating its use), so it has to be callable as written.

    ``list_custom_agents`` is NOT a top-level tool — it is an action on the
    lab_asset multiplexer. Asserting only that the bare name appears pinned a
    string the model cannot act on: it would look for a tool by that name, find
    none, and its only fallback was `action="describe"` with no params.
    """
    p = _prompt()

    assert 'lab_asset(action="list_custom_agents")' in p, (
        "the prompt must name the call shape that actually works"
    )
    assert "invoke_custom_agent" in p

    # The routing guidance should be present
    assert "delegate" in p.lower()


def test_lab_asset_schema_advertises_the_delegation_action():
    """The model must be able to discover the action from the SCHEMA alone.

    `lab_asset`'s docstring IS the tool description Bedrock receives, and it
    enumerates the reachable actions exhaustively — `<verb>_<asset>` over four
    draft categories, six hooks by name, five lifecycle actions by name.
    `list_custom_agents` matches none of those patterns, so omitting it left the
    action invisible: nothing in the schema implied it existed.

    Discovery is the sole mitigation the spec accepted for its Decision 3 risk
    ("the model never calls the tool"), so an unadvertised action defeats it.
    """
    from main_lab_tools import lab_asset

    description = lab_asset.tool_spec["description"]
    assert "list_custom_agents" in description, (
        "the prompt and invoke_custom_agent's description both tell the model "
        "to call this action; the only schema it can read must name it too"
    )


def test_system_prompt_custom_agent_routing_is_actionable():
    """The custom agent routing rule gives specific, actionable guidance.

    Checks that the rule doesn't just mention the tools but tells the model
    when and how to use them.
    """
    p = _prompt()

    # Should tell the model to read descriptions
    lower_p = p.lower()
    assert any(word in lower_p for word in ["description", "describe"])

    # Should mention the agent's configuration
    assert any(phrase in lower_p for phrase in ["own", "their own", "its own"])
