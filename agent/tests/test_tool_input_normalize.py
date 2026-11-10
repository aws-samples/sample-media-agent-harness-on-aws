"""Tests for the code_interpreter input normalizer (tool_input_normalize).

Covers the pure coercion, the tool_use rewrite, the delegating wrapper, and an
offline integration proof against the REAL strands ``code_interpreter`` tool:
a JSON-string ``code_interpreter_input`` is rejected with a ``model_type``
validation error unwrapped, but the wrapper coerces it to a dict so that
specific error no longer fires (the retry-storm trigger is gone).
"""
import asyncio
import json

from tool_input_normalize import (
    coerce_json_string_to_dict,
    normalize_code_interpreter_input,
    wrap_code_interpreter,
    NormalizedCodeInterpreterTool,
)
from strands.types.tools import AgentTool

# The exact shape observed in staging logs (Opus 4.8 stringifying the nested arg).
_STRINGIFIED = '{"action": {"type": "executeCode", "language": "python", "code": "import boto3\\nprint(1)"}}'


# --------------------------- pure coercion ---------------------------------

def test_coerce_json_string_object_to_dict():
    out = coerce_json_string_to_dict(_STRINGIFIED)
    assert isinstance(out, dict)
    assert out["action"]["type"] == "executeCode"


def test_coerce_passthrough_plain_dict():
    d = {"action": {"type": "listLocalSessions"}}
    assert coerce_json_string_to_dict(d) is d  # unchanged, same object


def test_coerce_passthrough_non_json_string():
    s = "just some text, not json"
    assert coerce_json_string_to_dict(s) is s


def test_coerce_passthrough_json_list():
    # A JSON array decodes to a list, not a dict -> leave as-is for the tool to reject.
    s = '["a", "b"]'
    assert coerce_json_string_to_dict(s) == s


def test_coerce_passthrough_non_string():
    assert coerce_json_string_to_dict(123) == 123
    assert coerce_json_string_to_dict(None) is None


def test_coerce_malformed_json_left_untouched():
    s = '{"action": {"type": '  # truncated / invalid JSON
    assert coerce_json_string_to_dict(s) is s


# --------------------- normalize_code_interpreter_input --------------------

def test_normalize_rewrites_string_field():
    tu = {"toolUseId": "t1", "name": "code_interpreter",
          "input": {"code_interpreter_input": _STRINGIFIED}}
    out = normalize_code_interpreter_input(tu)
    assert isinstance(out["input"]["code_interpreter_input"], dict)
    assert out["input"]["code_interpreter_input"]["action"]["type"] == "executeCode"
    # original not mutated
    assert isinstance(tu["input"]["code_interpreter_input"], str)


def test_normalize_noop_when_already_dict():
    tu = {"toolUseId": "t1", "input": {"code_interpreter_input": {"action": {"type": "x"}}}}
    assert normalize_code_interpreter_input(tu) is tu  # same object, no allocation


def test_normalize_noop_when_field_absent():
    tu = {"toolUseId": "t1", "input": {"something_else": 1}}
    assert normalize_code_interpreter_input(tu) is tu


def test_normalize_preserves_other_input_keys():
    tu = {"toolUseId": "t1", "input": {"code_interpreter_input": _STRINGIFIED, "extra": 7}}
    out = normalize_code_interpreter_input(tu)
    assert out["input"]["extra"] == 7
    assert out["toolUseId"] == "t1"


def test_normalize_handles_non_dict_tool_use():
    assert normalize_code_interpreter_input(None) is None


# ----------------------------- the wrapper ---------------------------------

class _RecordingTool(AgentTool):
    """Minimal AgentTool that records the tool_use it received and yields it back."""
    def __init__(self):
        super().__init__()
        self.seen = None

    @property
    def tool_name(self): return "code_interpreter"

    @property
    def tool_spec(self): return {"name": "code_interpreter", "inputSchema": {}}

    @property
    def tool_type(self): return "function"

    def get_display_properties(self): return {"Name": "code_interpreter", "Type": "function"}

    async def stream(self, tool_use, invocation_state, **kwargs):
        self.seen = tool_use
        yield {"ok": True}


def _drain(agen):
    async def run():
        return [ev async for ev in agen]
    return asyncio.run(run())


def test_wrapper_delegates_properties():
    inner = _RecordingTool()
    w = wrap_code_interpreter(inner)
    assert isinstance(w, NormalizedCodeInterpreterTool)
    assert w.tool_name == "code_interpreter"
    assert w.tool_type == "function"
    assert w.tool_spec["name"] == "code_interpreter"
    assert w.get_display_properties()["Type"] == "function"


def test_wrapper_normalizes_string_before_inner():
    inner = _RecordingTool()
    w = wrap_code_interpreter(inner)
    tu = {"toolUseId": "t1", "input": {"code_interpreter_input": _STRINGIFIED}}
    events = _drain(w.stream(tu, {}))
    assert events == [{"ok": True}]
    # The inner tool saw a DICT, not the raw JSON string.
    assert isinstance(inner.seen["input"]["code_interpreter_input"], dict)


def test_wrapper_passes_dict_through_unchanged():
    inner = _RecordingTool()
    w = wrap_code_interpreter(inner)
    tu = {"toolUseId": "t1", "input": {"code_interpreter_input": {"action": {"type": "x"}}}}
    _drain(w.stream(tu, {}))
    assert inner.seen is tu  # untouched happy path


def test_wrap_is_idempotent():
    inner = _RecordingTool()
    w1 = wrap_code_interpreter(inner)
    w2 = wrap_code_interpreter(w1)
    assert w2 is w1


# ---------------- offline integration against the REAL tool ----------------

def _run_real_tool(tool, tool_use):
    async def run():
        async for ev in tool.stream(tool_use, {}):
            return ev
    return asyncio.run(run())


def _result_text(ev):
    if isinstance(ev, str):
        return ev
    try:
        return json.dumps(ev)
    except Exception:
        return str(ev)


_MODEL_TYPE_ERR = "valid dictionary or instance of CodeInterpreterInput"


def test_real_tool_rejects_string_but_wrapper_coerces_past_model_type():
    """End-to-end on the installed strands version, offline (validation happens
    before any sandbox call): the raw JSON-string input trips the model_type
    validation error; wrapping coerces it to a dict so that error is gone."""
    from agentcore_code_interpreter import AgentCoreCodeInterpreter
    import os
    ci = AgentCoreCodeInterpreter(region=os.environ.get("AWS_REGION", "us-west-2"))
    raw = ci.code_interpreter
    wrapped = wrap_code_interpreter(raw)

    tu = {"toolUseId": "t1", "name": "code_interpreter",
          "input": {"code_interpreter_input": _STRINGIFIED}}

    unwrapped_txt = _result_text(_run_real_tool(raw, tu))
    assert _MODEL_TYPE_ERR in unwrapped_txt, (
        "baseline changed: raw tool should reject a stringified input with a model_type error"
    )

    wrapped_txt = _result_text(_run_real_tool(wrapped, dict(tu)))
    assert _MODEL_TYPE_ERR not in wrapped_txt, (
        "wrapper failed to coerce the stringified input past the model_type validation"
    )
