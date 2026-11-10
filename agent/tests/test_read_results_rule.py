"""The agent must read result files rather than pointing at them.

Observed: asked about encoding details, the agent replied that they "live inside
probe.json … only referenced by URL" and stopped. It had the URL and the means to
fetch it, so this was a behaviour gap, not a missing capability — these tests pin
both halves: the capability exists, and the instruction to use it is present and
stays generic.
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))


def _prompt() -> str:
    """The prompt's literal text, taken from source.

    Calling `_build_system_prompt()` needs a live gateway registry, which a unit
    test should not require; the instruction wording is a static property of the
    source, so read it from there.
    """
    import ast
    tree = ast.parse((SRC / "prompts" / "orchestrator.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "build_orchestrator_system_prompt":
            return " ".join(
                n.value for n in ast.walk(node)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            )
    raise AssertionError("_build_system_prompt not found")


class TestCapabilityExists:
    def test_agent_can_fetch_remote_content(self):
        src = (SRC / "main.py").read_text()
        # the tool that makes reading a result file possible at all
        assert '"http_request": http_request' in src
        assert '"read_result_files": read_result_files' in src


class TestInstructionPresent:
    def test_prompt_requires_reading_result_files(self):
        p = _prompt().lower()
        assert "retrieve and read" in p
        # the specific anti-pattern is called out, not just the happy path
        assert "is not an answer" in p

    def test_prompt_requires_saying_why_on_failure(self):
        p = _prompt().lower()
        assert "cannot be retrieved" in p

    def test_rule_stays_generic(self):
        """No filenames, tool parameters, query examples or UI nouns.

        A rule that names probe.json fixes one report and rots immediately; the
        instruction has to hold for any tool's output.
        """
        p = _prompt()
        for leak in ["probe.json", "shots.json", "facemetadata", "global_speakers",
                     "face_tracks", "encoding specs", "Diarize", "Recognize Faces",
                     "Segment Shots", "render tab"]:
            assert leak not in p, f"system prompt leaked {leak!r}"

    def test_rule_does_not_name_a_specific_tool_to_use(self):
        """Naming the fetch tool inside the rule would break when tools change."""
        seg = _prompt()
        start = seg.find("When a completed result carries data files")
        assert start >= 0
        rule = seg[start:start + 600]
        for toolish in ["http_request", "file_read", "code_interpreter", "get_job"]:
            assert toolish not in rule, f"rule hardcodes {toolish}"
