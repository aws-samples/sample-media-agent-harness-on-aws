"""The planner guidance must teach the collector-node fan-out contract.

Fan-out failed in production because the map pointed straight at an async producer whose output
is an S3 pointer, forcing the runtime to guess (the deleted `_distinct_speakers` heuristics). The
durable fix moves list-derivation into a planner-emitted collector node and makes the map runtime
dumb — so the planner guidance (the DAG design rules now live in `prompts/design.py`, bundled into
the fused planning call) MUST steer toward that shape. These are source-content assertions (the
prompts are built at import behind a feature flag), matching the `test_mirror_env` pattern.
"""
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"


def test_design_rules_teach_collector_fanout():
    src = (_SRC / "prompts" / "design.py").read_text()
    low = src.lower()
    assert "collector" in low                          # the concept is named
    assert "over.from" in src                          # map references a node, resolved dumbly
    assert '{"items"' in src                           # canonical collector output shape
    assert "collect_speakers" in src                   # Pattern A (derived from a producer)
    assert "list_mp4s" in src                          # Pattern B (list a source directly)
    # the runtime must be told NOT to crawl/parse — that's the collector's job now
    assert "does NOT crawl" in src or "domain-agnostic" in src


def test_planning_agent_covers_runtime_and_listed_fanout():
    src = ((_SRC / "planning_agent.py").read_text() + (_SRC / "prompts" / "planning.py").read_text())
    assert "RUNTIME FAN-OUT" in src
    # both the after-a-producer case and the list-a-source case are covered
    assert "listing a source" in src or "in <source to list>" in src


def test_planning_agent_diarize_reads_original_not_sibling_extract_output():
    # Regression: transcript nodes were wired to consume a sibling process_media "extract audio"
    # node's PREFIX output, forcing a guessed audio.wav key -> NoSuchKey (2/3 transcripts failed).
    src = ((_SRC / "planning_agent.py").read_text() + (_SRC / "prompts" / "planning.py").read_text())
    assert "INPUT SOURCE" in src
    assert "extract audio" in src and "NoSuchKey" in src
    # extract-audio + transcript of the same file are INDEPENDENT, not a sequence
    assert "TWO INDEPENDENT sub-goals" in src


def test_design_rules_teach_author_then_format_for_large_pdf():
    src = (_SRC / "prompts" / "design.py").read_text()
    assert "AUTHOR node" in src and "FORMAT node" in src   # the two-node split
    assert "BY REFERENCE" in src                           # renderer loads text by ref, not inline
    assert "never receive the document inline" in src.lower()


# ---- Test 1: skill guidance so nodes stop guessing keys / rejecting on low-confidence refs ----

_AGENT = Path(__file__).resolve().parents[1]


def test_diarization_skill_warns_against_guessing_output_keys():
    src = (_AGENT / "skills/video-processing/references/speech_diarization_agent.md").read_text()
    assert "Never guess output keys" in src
    assert "speakers/SPEAKER_00.wav" in src            # the real convention, stated
    assert "speaker_channels/speaker_A.wav" in src      # named as the mistake NOT to make
    assert "final_transcript.json" in src               # translation reads the transcript, not audio


def test_tts_skill_warns_low_confidence_diarized_ref():
    src = (_AGENT / "skills/video-processing/references/tts_agent.md").read_text()
    assert "diarized speaker" in src.lower()
    assert "0.70" in src                                # the ASR-validation threshold that rejects


# ---- Test 3: planner decomposes a broad research/report instead of one monolithic node ----

def test_planning_agent_steers_research_decomposition():
    src = ((_AGENT / "src/planning_agent.py").read_text() + (_AGENT / "src/prompts/planning.py").read_text())
    assert "PER TOPIC/SECTION" in src
    assert "monolithic" in src.lower()                  # names the observed stall
    assert "synthesis sub-goal" in src


# ---- generate_pdf wrote to a bucket named 'artifacts': inject the concrete location ----

def test_task_agent_injects_concrete_artifact_location():
    src = (_AGENT / "src/task_agent.py").read_text()
    # nodes must be told the concrete, SESSION-SCOPED output location (interpolated), not left to
    # guess — the default-task guidance uses `{out}` and the plan-node path uses
    # `{_artifact_output_prefix()}`; either is the concrete managed location.
    assert (
        "s3://{S3_BUCKET}/{out}<filename>" in src
        or "s3://{S3_BUCKET}/{_artifact_output_prefix()}<filename>" in src
    )
    # and explicitly forbidden from writing to a bucket named after the prefix (derived, not the
    # hardcoded literal 'artifacts')
    assert "never write to a bucket literally named" in src
    assert "operation='adopt'" in src
