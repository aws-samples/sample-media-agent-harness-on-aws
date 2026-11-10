"""Skill scripts reach the code-interpreter sandbox (src/skill_scripts.py + NormalizedCodeInterpreterTool).

Skills told the model to run `python scripts/<x>.py` but a sandbox starts empty and nothing copied the files
(only crop_video embedded its own). The wrapper now stages every referenced skill script, out of band, into the
sandbox the action targets.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import skill_scripts as ss  # noqa: E402
import tool_input_normalize as tin  # noqa: E402


@pytest.fixture
def skills(tmp_path):
    root = tmp_path / "skills"
    for skill, files in {
        "alpha": {"scripts/run.py": "import helper\nprint('alpha run')\n", "scripts/helper.py": "X = 1\n",
                  "assets/page.html": "<html/>", "SKILL.md": "x"},
        "beta": {"scripts/run.py": "print('beta run')\n", "SKILL.md": "x", "secret.txt": "nope"},
        "content-moderation": {"scripts/build_report.py": "print('br')\n", "SKILL.md": "x"},
    }.items():
        for rel, text in files.items():
            p = root / skill / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
    return [root]


def test_detects_bare_named_and_asset_references():
    refs = ss.find_references("python scripts/run.py && python skills/beta/scripts/run.py; "
                              "open('alpha/assets/page.html'); myscripts/x.py")
    assert refs == [(None, "scripts", "run.py"), ("beta", "scripts", "run.py"), ("alpha", "assets", "page.html")]


def test_traversal_and_non_script_paths_are_never_staged(skills):
    assert ss.files_to_stage("python scripts/../secret.txt", roots=skills) == {}
    assert ss.files_to_stage("cat beta/secret.txt", roots=skills) == {}
    assert ss.files_to_stage("python scripts/missing.py", roots=skills) == {}


def test_bare_reference_prefers_the_active_skill_and_brings_sibling_modules(skills):
    files = ss.files_to_stage("python scripts/run.py", active_skills=["alpha"], roots=skills)
    assert files["scripts/run.py"].startswith(b"import helper")
    assert "scripts/helper.py" in files and "skills/alpha/scripts/helper.py" in files
    assert not any(k.startswith("skills/beta/") for k in files)


def test_ambiguous_bare_reference_stages_every_match_under_its_own_skill(skills):
    files = ss.files_to_stage("python scripts/run.py", roots=skills)
    assert "skills/alpha/scripts/run.py" in files and "skills/beta/scripts/run.py" in files
    assert files["scripts/run.py"] in (files["skills/alpha/scripts/run.py"], files["skills/beta/scripts/run.py"])


def test_skill_names_match_across_dash_and_underscore(skills):
    files = ss.files_to_stage("python content-moderation/scripts/build_report.py f.json", roots=skills)
    assert files["skills/content-moderation/scripts/build_report.py"] == b"print('br')\n"


def test_size_cap_drops_siblings_then_everything(skills, monkeypatch):
    monkeypatch.setattr(ss, "MAX_STAGED_BYTES", 33)  # run.py (31 B) fits alone, not with helper.py (6 B)
    files = ss.files_to_stage("python scripts/run.py", active_skills=["alpha"], roots=skills)
    assert "skills/alpha/scripts/run.py" in files and "skills/alpha/scripts/helper.py" not in files
    monkeypatch.setattr(ss, "MAX_STAGED_BYTES", 10)
    assert ss.files_to_stage("python scripts/run.py", active_skills=["alpha"], roots=skills) == {}


def test_writer_code_recreates_the_files(tmp_path, skills):
    files = ss.files_to_stage("python scripts/run.py", active_skills=["alpha"], roots=skills)
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        exec(ss.writer_code(files), {})  # noqa: S102 - our own generated writer
        assert (tmp_path / "scripts" / "run.py").read_bytes() == files["scripts/run.py"]
        assert (tmp_path / "skills" / "alpha" / "scripts" / "helper.py").exists()
        assert (tmp_path / "alpha" / "scripts" / "run.py").exists()
    finally:
        os.chdir(cwd)


def test_the_real_skills_resolve():
    for text, key in [("python scripts/assemble_reel.py m.json o.mp4", "skills/agent-olive/scripts/assemble_reel.py"),
                      ("python media-references/scripts/estimate_cost.py", "skills/media-references/scripts/estimate_cost.py"),
                      ("python scripts/build_report.py findings.json", "skills/content-moderation/scripts/build_report.py")]:
        assert key in ss.files_to_stage(text), text


# ---- the wrapper ---------------------------------------------------------------------------------------------
class _Client:
    def __init__(self):
        self.calls = []

    def invoke(self, op, params):
        self.calls.append((op, params))


class _Info:
    def __init__(self):
        self.client = _Client()
        self.session_id = "sandbox-1"


class _CI:
    def __init__(self):
        self._sessions = {"default": _Info()}

    def _ensure_session(self, name):
        return ("default", None)


class _Inner:
    tool_name = "code_interpreter"
    tool_spec = {"name": "code_interpreter"}
    tool_type = "python"

    def __init__(self):
        self.seen = []
        self._tool_func = None

    async def stream(self, tool_use, invocation_state, **kw):
        self.seen.append(tool_use)
        yield {"done": True}


def _use(action):
    return {"toolUseId": "t", "name": "code_interpreter", "input": {"code_interpreter_input": {"action": action}}}


def _run(tool, tu):
    async def go():
        return [e async for e in tool.stream(tu, {})]
    return asyncio.run(go())


@pytest.fixture
def wrapped(monkeypatch, skills):
    monkeypatch.setattr(ss, "skill_roots", lambda: skills)
    inner = _Inner()
    tool = tin.NormalizedCodeInterpreterTool(inner)
    ci = _CI()
    monkeypatch.setattr(tool, "_interpreter", lambda: ci)
    return tool, inner, ci


def test_wrapper_stages_out_of_band_and_leaves_the_command_untouched(wrapped):
    tool, inner, ci = wrapped
    tu = _use({"type": "executeCommand", "command": "python3 beta/scripts/run.py"})
    _run(tool, tu)
    calls = ci._sessions["default"].client.calls
    assert len(calls) == 1 and calls[0][0] == "executeCode" and "literal_eval" in calls[0][1]["code"]
    assert inner.seen[0]["input"]["code_interpreter_input"]["action"]["command"] == "python3 beta/scripts/run.py"
    _run(tool, tu)  # same sandbox, same files: not staged again
    assert len(calls) == 1


def test_wrapper_without_interpreter_handle_prepends_to_python_code(monkeypatch, skills):
    monkeypatch.setattr(ss, "skill_roots", lambda: skills)
    inner = _Inner()
    tool = tin.NormalizedCodeInterpreterTool(inner)
    monkeypatch.setattr(tool, "_interpreter", lambda: None)
    _run(tool, _use({"type": "executeCode", "code": "import subprocess; subprocess.run(['python','scripts/run.py'])",
                     "language": "python"}))
    code = inner.seen[0]["input"]["code_interpreter_input"]["action"]["code"]
    assert code.startswith("import base64 as _b") and code.endswith("subprocess.run(['python','scripts/run.py'])")


def test_wrapper_passes_through_when_nothing_is_referenced_or_staging_fails(wrapped, monkeypatch):
    tool, inner, ci = wrapped
    tu = _use({"type": "executeCode", "code": "print(1)"})
    _run(tool, tu)
    assert inner.seen[-1]["input"]["code_interpreter_input"]["action"]["code"] == "print(1)"
    monkeypatch.setattr(ss, "files_to_stage", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    _run(tool, _use({"type": "executeCode", "code": "python scripts/run.py"}))
    assert inner.seen[-1]["input"]["code_interpreter_input"]["action"]["code"] == "python scripts/run.py"
    assert ci._sessions["default"].client.calls == []


def test_presign_rewrites_the_model_text_but_never_the_staged_source(wrapped, monkeypatch):
    tool, inner, ci = wrapped
    monkeypatch.setattr(tin, "presign_artifact_urls_in_code",
                        lambda tu: {**tu, "input": {"code_interpreter_input": {"action": {
                            **tu["input"]["code_interpreter_input"]["action"],
                            "command": tu["input"]["code_interpreter_input"]["action"]["command"].replace("CDN", "SIGNED")}}}})
    _run(tool, _use({"type": "executeCommand", "command": "python3 alpha/scripts/run.py CDN"}))
    assert inner.seen[0]["input"]["code_interpreter_input"]["action"]["command"].endswith("SIGNED")
    staged = ci._sessions["default"].client.calls[0][1]["code"]
    assert "SIGNED" not in staged and "CDN" not in staged


# A script named WITHOUT its scripts/ dir, as agent-olive's own text says it ("python assemble_reel.py"): staging
# skipped it, and session 39d5fb04 spent five minutes copying the script into the sandbox by hand.
def test_a_bare_script_name_of_an_active_skill_is_staged_where_it_runs():
    import skill_scripts as ss
    files = ss.files_to_stage("FFMPEG_S3_URI=s3://b/tools/ffmpeg python assemble_reel.py m.json out.mp4 --execute",
                              ["agent-olive"])
    assert "assemble_reel.py" in files and "scripts/assemble_reel.py" in files
    assert files["assemble_reel.py"] == files["skills/agent-olive/scripts/assemble_reel.py"]


def test_a_bare_name_never_stages_from_an_inactive_skill_or_an_unknown_file():
    import skill_scripts as ss
    assert ss.files_to_stage("python assemble_reel.py", ["video-processing"]) == {}
    assert ss.files_to_stage("see notes.py and setup.sh", ["agent-olive"]) == {}


def test_a_runtime_absolute_skill_path_runs_from_the_staged_copy(wrapped):
    # The AgentSkills plugin shows "Location: /var/task/skills/<skill>/SKILL.md"; a model that builds
    # the script path from it must still run the staged copy (the sandbox has no /var/task).
    tool, inner, ci = wrapped
    _run(tool, _use({"type": "executeCommand",
                     "command": "python3 /var/task/skills/beta/scripts/run.py --x /var/task/skills/beta/secret.txt"}))
    staged = ci._sessions["default"].client.calls
    assert len(staged) == 1
    cmd = inner.seen[0]["input"]["code_interpreter_input"]["action"]["command"]
    # the staged script is rewritten; a path that was never staged is left alone
    assert cmd == "python3 skills/beta/scripts/run.py --x /var/task/skills/beta/secret.txt"


def test_relativize_rewrites_only_staged_files(skills):
    files = {"skills/alpha/scripts/run.py": b"x"}
    root = str(skills[0])
    assert ss.relativize_runtime_paths(f"python {root}/alpha/scripts/run.py", files, roots=skills) == \
        "python skills/alpha/scripts/run.py"
    assert ss.relativize_runtime_paths("python /var/task/skills/alpha/scripts/other.py", files, roots=skills) == \
        "python /var/task/skills/alpha/scripts/other.py"
    assert ss.relativize_runtime_paths("python scripts/run.py", {}, roots=skills) == "python scripts/run.py"


def test_an_idled_out_sandbox_is_replaced_and_the_call_runs_once_more(monkeypatch, skills):
    # session be68313c: the cached sandbox had expired, so injection, staging AND the stitch failed
    # with "Code interpreter session ... is not active" until the model happened to retry.
    from strands.types._events import ToolResultEvent
    monkeypatch.setattr(ss, "skill_roots", lambda: skills)

    class _Flaky(_Inner):
        async def stream(self, tool_use, invocation_state, **kw):
            self.seen.append(tool_use)
            if len(self.seen) == 1:
                yield ToolResultEvent({"toolUseId": "t", "status": "error", "content": [{"text": (
                    "Error: ValidationException - An error occurred (ValidationException) when calling the "
                    "InvokeCodeInterpreter operation: Code interpreter session 01M5 is not active")}]})
            else:
                yield ToolResultEvent({"toolUseId": "t", "status": "success", "content": [{"text": "ok"}]})

    inner = _Flaky()
    tool = tin.NormalizedCodeInterpreterTool(inner)
    ci = _CI()
    ci.default_session = "default"
    ci._ensure_session = lambda name: (name or "default", None) if (name or "default") in ci._sessions else (
        ci._sessions.setdefault(name or "default", _Info()) and (name or "default", None))
    monkeypatch.setattr(tool, "_interpreter", lambda: ci)
    events = _run(tool, _use({"type": "executeCommand", "command": "python3 beta/scripts/run.py"}))
    results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(inner.seen) == 2
    assert [r.tool_result["status"] for r in results] == ["success"]    # the stale error never reaches the model
    assert ci._sessions["default"].client.calls  # the fresh sandbox got the script staged again


def test_a_second_inactive_error_is_returned_not_retried_forever(monkeypatch, skills):
    from strands.types._events import ToolResultEvent
    monkeypatch.setattr(ss, "skill_roots", lambda: skills)

    class _Dead(_Inner):
        async def stream(self, tool_use, invocation_state, **kw):
            self.seen.append(tool_use)
            yield ToolResultEvent({"toolUseId": "t", "status": "error",
                                   "content": [{"text": "Code interpreter session X is not active"}]})

    inner = _Dead()
    tool = tin.NormalizedCodeInterpreterTool(inner)
    ci = _CI()
    ci.default_session = "default"
    ci._ensure_session = lambda name: (ci._sessions.setdefault("default", _Info()) and ("default", None))
    monkeypatch.setattr(tool, "_interpreter", lambda: ci)
    events = _run(tool, _use({"type": "executeCode", "code": "print(1)", "language": "python"}))
    assert len(inner.seen) == 2
    assert [e.tool_result["status"] for e in events if isinstance(e, ToolResultEvent)] == ["error"]
