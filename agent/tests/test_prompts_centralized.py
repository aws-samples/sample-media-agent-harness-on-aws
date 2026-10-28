"""Byte-identity guard for the centralized static prompts (prompts/ package).

Static agent system prompts were moved out of the entrypoints into `prompts/` for
central management. The move MUST be byte-for-byte — a prompt is agent behavior, so an
accidental edit during the move is a silent regression. These frozen SHA256s were captured
from the pre-refactor code; they must hold before AND after the move. Regenerate ONLY with a
deliberate, reviewed prompt change (and pair it with an eval run).
"""
import hashlib


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# Frozen from the pre-centralization code (do not edit without a reviewed prompt change).
FROZEN = {
    # Rebaselined for FAL MODEL NODES + the `endpoint_ids` schema field: a fal submit is held to
    # the node's planned endpoint (session c3447913: planned H3-Max, submitted Hailuo, 422).
    # Rebaselined for REPEATED NODES (for_each) + TOOL NODES (kind "tool", the Task Agent's
    # no-model fast path) and the `for_each` schema field (staging plan 81d9198e: 8 spelled-out
    # shot prompts, 4,182 design tokens, two model calls per fully specified node).
    "design.DESIGN_SYSTEM_PROMPT": "ae3790ab7058c1fca073deb9a9c8d713273d7860b867cce17bcc5001d6b8a8e9",
    # The one-call plan-and-design prompt: the planning prompt + FUSED_DESIGN_SECTION_TEMPLATE
    # wrapping the design prompt above verbatim.
    "planning.FUSED_SYSTEM_PROMPT": "622404ad31d46182b115829c93b8add88f60c88a61f8c935e4cd78d7f21e1857",
    # Rebaselined for the one-source-N-slices fan-out carve-out (session 27c33c57: "clip shots
    # 3-9" became a 7-node plan, 7m39s, each node re-reading the whole source).
    "planning.PLANNING_SYSTEM_PROMPT": "ed538607bc9f5e660f8312b2f56335f9a5d5729411a52c2af38d6f15e1665122",
    "render.RENDER_SYSTEM_PROMPT": "a160a456b24cad516ed6fbcaa2e499d8b336c081ab1048938063ee089cb7625b",
    "task.DEFAULT_TASK_SYSTEM_PROMPT": "64c3be38cd21a8f2ed675e467349abc61458ba1e5ef01609156f3d51f754622b",
    # Rebaselined: the download link carries the caller's conversation_id, because the download
    # route now serves only the derived session a link names (per-caller scoping).
    "task.ASYNC_JOBS_GUIDANCE_TEMPLATE": "b287c96faa440d478c2886baf454082b3325a8a14a3c764317816f7a2e7cf6a5",
    "main.SKILL_GEN_PROMPT": "b7decff051a4729e72dcc263a2c3e7c885470238392391206ecf2b525aef6f42",
    "chat._SYSTEM_PROMPT_TEMPLATE": "cb9a3f1504ad92f04ca8dfa8ea6559aeceace216e816f616d9ff58eda65f7f2b",
}

# The assembled orchestrator prompt with the dynamic catalog neutralized and no inventory.
# Rebaselined for the REPLIES AND REMEMBERED CONTEXT section (sessions 764a71c0, 2f376186), then
# for the one-source-N-slices fan-out carve-out (session 27c33c57), then for the remembered-file-
# locations rule (find/show via project_status + show_asset, never another session's URL), then to
# do that lookup BEFORE looking in memory, then for removing long-term facts (memory keeps only
# preferences + session summaries; the file-locations rule shortened to project_status/show_asset),
# then for the two-errors bound on a broken tool path (a crop_video failure became 220s and 26
# steps of chasing ffmpeg, a static download and an M2C fallback), then for browse_project in the
# remembered-file-locations rule (browse the project as a Renderer grid tab).
# 2026-10-16: RESPONSE STYLE gained "Search once with a well-formed query..." and "Refer to files by their short
# asset reference, never by long URLs".
ORCHESTRATOR_ASSEMBLED_SHA = "830fb08fc10b568fe35ebef5346f6f53b30c631235964f20f6db2ab256052325"


def _live_named() -> dict:
    import planning_agent, render_agent, task_agent, main
    from prompts.design import design_system_prompt
    from lab import chat_agent
    return {
        # The DAG design rules stay in prompts/design.py (the Design Agent runtime is retired; the
        # fused planning call bundles this verbatim). Byte-identity is unchanged.
        "design.DESIGN_SYSTEM_PROMPT": design_system_prompt(),
        "planning.PLANNING_SYSTEM_PROMPT": planning_agent.PLANNING_SYSTEM_PROMPT,
        "planning.FUSED_SYSTEM_PROMPT": planning_agent._fused_system_prompt(),
        "render.RENDER_SYSTEM_PROMPT": render_agent.RENDER_SYSTEM_PROMPT,
        "task.DEFAULT_TASK_SYSTEM_PROMPT": task_agent.DEFAULT_TASK_SYSTEM_PROMPT,
        "task.ASYNC_JOBS_GUIDANCE_TEMPLATE": task_agent.ASYNC_JOBS_GUIDANCE_TEMPLATE,
        "main.SKILL_GEN_PROMPT": main.SKILL_GEN_PROMPT,
        "chat._SYSTEM_PROMPT_TEMPLATE": chat_agent._SYSTEM_PROMPT_TEMPLATE,
    }


def test_static_prompts_are_byte_identical():
    live = _live_named()
    mismatched = {k: _sha(live[k]) for k, exp in FROZEN.items() if _sha(live[k]) != exp}
    assert not mismatched, (
        "Static prompt(s) changed vs the frozen baseline — the centralization move must be "
        f"byte-for-byte. Mismatched: {list(mismatched)}"
    )


def test_orchestrator_assembled_prompt_is_byte_identical(monkeypatch):
    import main
    monkeypatch.setattr(main, "_format_catalog_text", lambda *a, **k: "<CATALOG>")
    monkeypatch.setattr(main, "_build_catalog", lambda *a, **k: {})
    assembled = main._build_system_prompt(prompt="")
    assert _sha(assembled) == ORCHESTRATOR_ASSEMBLED_SHA, (
        "The assembled orchestrator system prompt changed — _build_system_prompt must produce "
        "the identical text after extracting the static base into prompts/."
    )
