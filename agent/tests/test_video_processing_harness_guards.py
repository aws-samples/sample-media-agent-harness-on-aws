"""The harness side of the M2C kit references: our SKILL.md and the headers above the verbatim kit bodies.

The kit is written for a local agent (polls, presigns to read, writes to the proxy bucket). Each guard below
pins one place where that does not fit this harness, so a kit refresh or a SKILL.md edit cannot quietly
reintroduce it. Bodies stay verbatim: corrections live in the harness header (the text before the
"# ... System Prompt" heading), which test_video_processing_skill_package already separates.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[1] / "skills"
VP = SKILLS / "video-processing"
SKILL_MD = (VP / "SKILL.md").read_text(encoding="utf-8")
REFS = sorted((VP / "references").glob("*.md"))
TOOLS = set(json.loads((Path(__file__).parent / "fixtures" / "m2c_gateway_tools.json").read_text())["tools"])
TARGETS = {t.split("___", 1)[0] for t in TOOLS}
BODY = re.compile(r"(?m)^# .+ System Prompt$")


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _split(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    m = BODY.search(text)
    return (text[:m.start()], text[m.start():]) if m else (text, "")


def test_every_m2c_tool_name_exists_on_the_gateway_or_is_corrected():
    """A named tool that does not exist is a call that fails. audio_description / program_structure name
    `audiostem-separation___separate_audio_stems` (the target is `audio-stem`): the header must correct it."""
    bad = []
    for path in [VP / "SKILL.md", *REFS]:
        header, body = _split(path)
        for name in set(re.findall(r"\b([a-z][a-z-]+)___([a-z_]+)\b", body or header)):
            full = "___".join(name)
            known_target = name[0] in TARGETS or name[0] == "audiostem-separation"
            if known_target and full not in TOOLS and f"`{full}`" not in header:
                bad.append(f"{path.name}: {full}")
    assert not bad, "tool names not on the M2C gateway (and not corrected in the header):\n" + "\n".join(bad)


def test_every_wrong_tool_name_is_corrected_to_the_real_one():
    for path in REFS:
        header, body = _split(path)
        if "audiostem-separation___separate_audio_stems" in body:
            assert "`audio-stem___separate_audio_stems`" in header, path.name


def test_skill_md_never_asks_for_the_retired_uuid_parameter():
    # No tool on the alpha.20261014a gateway declares `uuid`; the kit says a stray one is rejected.
    assert "Pass `uuid`" not in SKILL_MD
    assert "idempotency_token" in SKILL_MD


def test_every_reference_header_carries_the_results_reading_rule():
    # The kit bodies presign outputs to READ them; here results come from get_job_status / get_plan_results
    # and read_result_files, and a presigned link is only for handing to the user.
    for path in REFS:
        header, _ = _split(path)
        assert "**Reading results:**" in header and "read_result_files" in header, path.name


def test_no_body_has_the_sandbox_write_the_proxy_bucket_without_a_header_override():
    # The sandbox can write only the session OUTPUT prefix (test_skill_sandbox_write_prefixes).
    wr = re.compile(r"(?:write|upload|stage)[^.\n]{0,80}(?:proxy bucket|micro-workflow/)", re.I)
    for path in REFS:
        header, body = _split(path)
        if wr.search(_flat(body)):
            assert "**Writes:**" in header, f"{path.name}: tells the model to write M2C's proxy bucket"


def test_bedrock_inference_header_keeps_records_inline_and_names_the_thinking_default():
    header, _ = _split(VP / "references" / "bedrock_inference_agent.md")
    flat = _flat(header)
    assert "records inline" in flat and '`thinking: "adaptive"`' in flat


def test_face_recognition_header_restates_the_no_persistence_defaults():
    header, _ = _split(VP / "references" / "face_recognition_agent.md")
    flat = _flat(header)
    assert '`collection_id: ""`' in flat and "explicit user intent" in flat


def test_skill_md_provenance_names_the_kit_it_was_copied_from():
    assert "client-kit-alpha.20261013a" in SKILL_MD
    assert "7cdd98e51c101a806506a9d0e6c463c5deb0fd0e" not in SKILL_MD


def test_skill_md_routing_row_does_not_send_frames_and_crops_to_process_media():
    row = next(l for l in SKILL_MD.splitlines() if l.startswith("| Trim, clip, transcode"))
    assert "capture frames" not in row and "crop (top-left" not in row


def test_skill_md_does_not_claim_no_managed_endpoint_can_crossfade():
    # quick_edit does cuts, fades and crossfades; the harness routes combining to ffmpeg for its own reasons.
    assert "NO managed endpoint supports these" not in _flat(SKILL_MD)


def test_a_single_trim_keeps_the_source_resolution():
    # process_media writes a 960x540 proxy unless width/height are given (staging: a 1280x720 trim came out 960x540).
    flat = _flat(SKILL_MD)
    assert "960×540" in flat and "pass the source's `width` and `height`" in flat


def test_content_moderation_knows_conversation_analysis_has_spoken_evidence():
    text = _flat((SKILLS / "content-moderation" / "SKILL.md").read_text(encoding="utf-8"))
    assert "`conversation_analysis.json` has NO moderation vocabulary" not in text
    assert "discourse_facts" in text
