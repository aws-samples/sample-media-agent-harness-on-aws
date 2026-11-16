"""A node must produce what it declared, or it has not completed.

The mux failure that was reported three times was never silent. The node said so:

    "I cannot complete the merge step... Calling it and claiming it merged the music
     track would be fabricating a result the tool cannot produce."

It declared output_type "s3_uri" and output_key "final_video", returned that prose, and
was recorded status="completed" -- because status was "completed" unless the agent
raised. The plan then reported SUCCEEDED with no merged file, and a user had to read the
node's artifacts by hand to discover the merge never happened.

Checked against the node's OWN declaration, not a list of node names.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Trimmed from the real t3_merge result in S3.
REFUSAL = (
    "Confirmed both source outputs:\n"
    "- Video: `https://v3b.fal.media/files/b/0aa596f0/tn3x0S_output.mp4`\n"
    "**I cannot complete the merge step.** The only media-processing tool available "
    "has a fixed schema that accepts exactly one source asset."
)


def _unmet(output_type, structured, text):
    from task_agent import _unmet_output_contract
    return _unmet_output_contract(output_type, structured, text)


def test_a_uri_output_type_with_no_uri_is_unmet():
    reason = _unmet("s3_uri", {"text": "I cannot complete the merge step."},
                    "I cannot complete the merge step.")
    assert reason and "no s3:// or https:// uri" in reason


def test_a_uri_output_type_with_a_real_uri_is_met():
    assert _unmet("s3_uri", {"uri": "s3://bkt/final_video.mp4"}, "done") == ""


def test_text_and_json_output_types_impose_no_uri_requirement():
    for t in ("text", "json", ""):
        assert _unmet(t, {"text": "a summary"}, "a summary") == ""


def test_the_real_refusal_still_passes_because_it_quotes_its_inputs():
    """Honest limitation, asserted so it is not mistaken for coverage.

    The refusal quotes the two INPUT urls, so a uri-presence check cannot catch this
    exact text. What this contract does catch is the larger class -- a node that
    promises an artifact and returns prose with no uri at all. Distinguishing "a uri
    I made" from "a uri I was given" needs the node's inputs, which the executor does
    not receive today; that is the follow-up, and pretending otherwise here would
    hide it.
    """
    assert _unmet("s3_uri", {"text": REFUSAL}, REFUSAL) == ""


def test_media_and_artifact_output_types_are_also_covered():
    for t in ("media", "artifact", "file", "url", "URI"):
        assert _unmet(t, {"text": "nothing here"}, "nothing here") != ""


def test_the_chainer_forwards_the_declaration():
    src = (Path(__file__).resolve().parents[2]
           / "infra/lib/lambdas/plan-chainer/handler.py").read_text()
    assert '"output_type": node_def.get("output_type")' in src


def _declared(structured):
    from task_agent import _declared_error
    return _declared_error(structured)


def test_declared_error_detects_top_level_error_payload():
    # t4_synthesize_fr's actual output shape
    err = _declared({"error": "Cannot proceed: upstream step t3_translate_fr produced no data"})
    assert err.startswith("Cannot proceed")


def test_declared_error_detects_a_status_error_payload():
    # c7d66001's assemble step: no top-level "error", the failure is in "status" + "errors"
    err = _declared({"status": "error", "final_url": None, "per_shot": [],
                     "errors": ["chunk1 FAILED upstream", "AccessDenied reading plan step outputs"]})
    assert err == "chunk1 FAILED upstream; AccessDenied reading plan step outputs"
    assert _declared({"status": "FAILED"}) == "status: FAILED"
    assert _declared({"status": "failure", "errors": "no input"}) == "no input"


def test_declared_error_ignores_non_error_payloads():
    assert _declared({"uri": "s3://bkt/out.wav"}) == ""
    assert _declared({"text": "a normal summary with the word error in prose"}) == ""
    assert _declared({"error": ""}) == ""          # empty error is not a failure
    assert _declared({"error": None}) == ""
    assert _declared({"error": False, "status": "ok"}) == ""
    assert _declared({"status": "completed", "piece_a_status": "FAILED"}) == ""  # only the node's own status
    assert _declared("not a dict") == ""
    assert _declared(None) == ""
