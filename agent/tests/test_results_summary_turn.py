"""A finished job's notification asks the agent to SUMMARISE results the panel already shows. Asking in the prompt
was not enough (IND403 dry run: the agent opened a second shot-segmentation gallery tab), so a
`turn_purpose: "results_summary"` turn does not get the tools that make media or open a tab."""
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import main  # noqa: E402


def test_every_withheld_tool_is_a_real_builtin():
    # A renamed tool would silently escape the filter. The registry is filled at startup.
    main._register_builtin_tools()
    missing = sorted(set(main.RESULTS_SUMMARY_WITHHELD_TOOLS) - set(main._all_builtin_tools))
    assert not missing, f"not builtins: {missing}"


def test_the_gallery_and_media_actions_are_withheld():
    assert {"show_media_gallery", "clip_video", "extract_frames", "crop_video"} <= main.RESULTS_SUMMARY_WITHHELD_TOOLS


def test_the_turn_filter_reads_turn_purpose_and_only_narrows():
    src = inspect.getsource(main.invoke)
    assert 'payload.get("turn_purpose")' in src
    assert '"results_summary"' in src
    assert "not in RESULTS_SUMMARY_WITHHELD_TOOLS" in src  # it removes tools; nothing adds one
