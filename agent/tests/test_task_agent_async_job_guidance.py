"""Unit tests for the external async-job guidance append (`_resolve_system_prompt`).

Regression cover for a live sandbox defect: external Custom Agent invocations
exhausted the ``EXT_MAX_TOKENS`` ceiling (418,062 > 400,000) by calling a
gateway ``*_get_job_status`` tool in a tight loop — 17 cycles of byte-identical
``RUNNING`` results, each re-sending the whole conversation.

``main.py`` has always carried an ``ASYNC JOBS`` block telling the orchestrator
not to poll in a loop. ``task_agent.py`` — the runtime that actually hosts every
Custom Agent — had none. Worse, the external path maps ``persona.md`` onto
``system_prompt``, and a caller-supplied ``system_prompt`` *replaces* the
built-in default wholesale, so a persona guaranteed the guidance was absent
exactly where the bounded EXT ceilings can be hit.

The fix appends the guidance for principal-bearing (external) invocations
instead of letting the persona replace it. Scoping mirrors the existing
``_select_ceilings`` / ``_usage_attribution`` convention: external is
``isinstance(principal, dict)``, and internal invocations are left byte-identical
so nothing regresses.
"""

from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import task_agent  # noqa: E402
from task_agent import (  # noqa: E402
    ASYNC_JOBS_GUIDANCE,
    DEFAULT_TASK_SYSTEM_PROMPT,
    _resolve_system_prompt,
    _s3_context_guidance,
    _artifact_output_prefix,
)

PERSONA = "You are Meridian Post's media assistant."


def _external(**extra) -> dict:
    """A principal-bearing payload, as stamped by the external invoke proxy."""
    return {
        "principal": {"issuer": "https://issuer.example.com", "sub": "caller-1"},
        **extra,
    }


# ---------------------------------------------------------------------------
# The regression: a persona must not be able to drop the async-job contract
# ---------------------------------------------------------------------------


def test_external_persona_still_receives_async_job_guidance():
    """A persona-bearing external call keeps BOTH the persona and the guidance.

    This is the exact shape that blew the token ceiling in the sandbox: the
    resolver wrote ``persona.md`` into ``system_prompt``, which replaced the
    default, leaving no polling instruction at all.
    """
    resolved = _resolve_system_prompt(_external(system_prompt=PERSONA))

    assert PERSONA in resolved
    assert "ASYNC JOBS:" in resolved
    assert "AT MOST ONCE" in resolved


def test_persona_precedes_guidance_and_is_verbatim():
    """The persona is preserved exactly and stays first; guidance is appended."""
    resolved = _resolve_system_prompt(_external(system_prompt=PERSONA))

    assert resolved.startswith(PERSONA)
    assert resolved.index(PERSONA) < resolved.index("ASYNC JOBS:")
    # Appended, not interpolated: persona, then session-scoped S3 guidance, then the async
    # contract (external). S3 guidance is now appended to EVERY invocation (see the source fix:
    # code_interpreter must write to artifacts/<session>/output/, not a flat key).
    assert resolved == f"{PERSONA}\n\n{_s3_context_guidance('')}\n\n{ASYNC_JOBS_GUIDANCE}"


def test_external_without_system_prompt_gets_default_plus_guidance():
    """With no caller prompt the default still gains the S3 + async guidance."""
    resolved = _resolve_system_prompt(_external())

    assert resolved == f"{DEFAULT_TASK_SYSTEM_PROMPT}\n\n{_s3_context_guidance('')}\n\n{ASYNC_JOBS_GUIDANCE}"


# ---------------------------------------------------------------------------
# Internal path: async guidance stays external-only, but the SESSION-SCOPED S3
# guidance is now appended for every invocation (the flat-key fix), so internal
# prompts are default/persona + S3 guidance (no async block).
# ---------------------------------------------------------------------------


def test_internal_custom_prompt_gets_s3_guidance_no_async():
    """Internal dispatch_tool persona: S3 guidance appended, async guidance NOT."""
    resolved = _resolve_system_prompt({"system_prompt": PERSONA})

    assert resolved == f"{PERSONA}\n\n{_s3_context_guidance('')}"
    assert "ASYNC JOBS:" not in resolved


def test_internal_default_prompt_gets_s3_guidance_no_async():
    """No principal, no caller prompt ⇒ default + session-scoped S3 guidance."""
    resolved = _resolve_system_prompt({})

    assert resolved == f"{DEFAULT_TASK_SYSTEM_PROMPT}\n\n{_s3_context_guidance('')}"
    assert "ASYNC JOBS:" not in resolved


def test_non_dict_principal_is_treated_as_internal():
    """Only a dict principal marks an invocation external (no async block).

    Mirrors ``_select_ceilings`` / ``_usage_attribution``. The S3 guidance is still
    appended (it is unconditional), but the async block is not.
    """
    for bogus in ("", None, 0, [], "caller-1"):
        resolved = _resolve_system_prompt(
            {"principal": bogus, "system_prompt": PERSONA}
        )
        assert resolved == f"{PERSONA}\n\n{_s3_context_guidance('')}", f"principal={bogus!r} should be internal"
        assert "ASYNC JOBS:" not in resolved


# ---------------------------------------------------------------------------
# Guidance content — scoped to what the Task Agent can actually do
# ---------------------------------------------------------------------------


def test_guidance_forbids_poll_loops():
    assert "Do NOT poll" in ASYNC_JOBS_GUIDANCE
    assert "get_job_status" in ASYNC_JOBS_GUIDANCE


def test_guidance_omits_tools_the_task_agent_does_not_have():
    """Don't advertise orchestrator-only affordances to the Task Agent.

    ``list_my_jobs`` / ``get_job`` are bound on the Main Agent, and
    ``dispatch_tool`` is how the Task Agent gets *called* — naming them here
    would invite calls to tools that aren't in this runtime's tool list, which
    the prompt's own TOOL USAGE RULES forbid.
    """
    for absent in ("list_my_jobs", "dispatch_tool", "get_job(", "Rendered artifacts"):
        assert absent not in ASYNC_JOBS_GUIDANCE, f"{absent!r} should not appear"


def test_default_prompt_constant_matches_module_default():
    """The extracted constant is the prompt the module actually uses."""
    assert DEFAULT_TASK_SYSTEM_PROMPT.startswith("You are a task execution agent.")
    assert "TOOL USAGE RULES (CRITICAL):" in task_agent.DEFAULT_TASK_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Download links — the output-truncation half of the problem
# ---------------------------------------------------------------------------
#
# A presigned S3 URL is ~1700 characters. An agent that called
# ``get_download_url`` per artifact and pasted the results into one reply blew
# the per-response output budget and truncated mid-URL, so a job that had
# actually SUCCEEDED came back with no usable links at all. The guidance steers
# it to the short ``/invoke/<id>/download?ref=`` form instead.


def test_guidance_directs_agent_to_short_download_links():
    assert "DOWNLOAD LINKS:" in ASYNC_JOBS_GUIDANCE
    assert "/invoke/<agent_id>/download?ref=" in ASYNC_JOBS_GUIDANCE
    # It must point at the data it ALREADY has rather than another tool call.
    assert "`outputs`" in ASYNC_JOBS_GUIDANCE


def test_guidance_forbids_pasting_presigned_urls():
    """Both the tool-call habit and the paste itself are called out."""
    assert "media-outputs___get_download_url" in ASYNC_JOBS_GUIDANCE
    assert "X-Amz-Signature" in ASYNC_JOBS_GUIDANCE
    assert "AWSAccessKeyId" in ASYNC_JOBS_GUIDANCE
    # And it says WHY, so the model has a reason to comply rather than a bare
    # prohibition it can rationalize around.
    assert "truncated" in ASYNC_JOBS_GUIDANCE


def test_guidance_retains_the_poll_loop_rules():
    """The download section must not have displaced the original contract."""
    assert "Do NOT poll" in ASYNC_JOBS_GUIDANCE
    assert "AT MOST ONCE" in ASYNC_JOBS_GUIDANCE


def test_download_path_carries_the_real_agent_id():
    """The rendered path must be concrete, not a placeholder to fill in.

    Regression: the guidance used to say "use the `agent_id` from this
    invocation", but the agent never sees that value anywhere in its prompt. It
    substituted the persona's DISPLAY NAME instead
    (`/invoke/ExtToolsBot/download?...`), which does not match any published
    agent id — CloudFront fell through to the SPA and the link returned HTML
    instead of the file.
    """
    rendered = task_agent._async_jobs_guidance("a7b90e6b-cd12-429c-a8a5-0c47f92f399b")
    assert "/invoke/a7b90e6b-cd12-429c-a8a5-0c47f92f399b/download?ref=" in rendered
    # No unfilled template hole survives into the prompt.
    assert "{download_base}" not in rendered
    assert "<agent_id>" not in rendered


def test_resolved_prompt_interpolates_agent_id_for_external_calls():
    """The external path renders the concrete path; internal is untouched."""
    external = _resolve_system_prompt({
        "principal": {"issuer": "https://i", "sub": "s"},
        "agent_id": "agent-xyz",
        "system_prompt": "You are Bot.",
    })
    assert "/invoke/agent-xyz/download?ref=" in external
    assert external.startswith("You are Bot.")

    # No agent_id (shouldn't happen externally) falls back to the placeholder
    # rather than emitting a malformed path.
    no_id = _resolve_system_prompt({"principal": {"issuer": "https://i", "sub": "s"}})
    assert "/invoke/<agent_id>/download?ref=" in no_id


def test_download_link_carries_the_callers_conversation():
    """The download route serves a caller only the derived session a link names, and that
    session folds in the conversation nonce. A link written inside a named conversation that
    dropped it would point at the caller's DEFAULT conversation and refuse their own output."""
    external = _resolve_system_prompt({
        "principal": {"issuer": "https://i", "sub": "s", "conversation_id": "conv-7"},
        "agent_id": "agent-xyz",
        "system_prompt": "You are Bot.",
    })
    assert "/invoke/agent-xyz/download?conversation_id=conv-7&ref=" in external
    # Sanitized exactly as the proxy sanitizes it, so the re-derived session matches.
    hostile = task_agent._async_jobs_guidance("agent-xyz", "conv 7&ref=evil<x>")
    assert "/invoke/agent-xyz/download?conversation_id=conv7refevilx&ref=" in hostile
    # No conversation → the plain form, unchanged for every existing caller.
    assert "/invoke/agent-xyz/download?ref=" in task_agent._async_jobs_guidance("agent-xyz", "")
    assert "{download_link}" not in external


def test_main_model_output_is_capped():
    """The main task-agent model must carry an explicit output cap.

    Strands omits ``inferenceConfig.maxTokens`` entirely when ``max_tokens`` is
    unset, so Bedrock applies its own smaller default — the ceiling that
    truncated the presigned-URL reply, losing the whole answer on a job that had
    actually succeeded.

    The cap originally lived inline here as ``max_tokens=TASK_MAX_OUTPUT_TOKENS``.
    Staging's ``model_factory.build_model`` now owns it for all ten construction
    sites (``max_tokens_for``: explicit arg → ``BEDROCK_MAX_TOKENS`` → default),
    which is the better seam — ten sites configured individually is exactly how
    they drift apart. So this asserts the *invariant* (the model is built through
    the capping factory) rather than one spelling of it.
    """
    from model.factory import DEFAULT_MAX_TOKENS, max_tokens_for

    # The factory caps unconditionally: no argument, no env var, still bounded.
    assert DEFAULT_MAX_TOKENS > 0
    assert max_tokens_for() > 0
    assert max_tokens_for(4096) == 4096

    src = pathlib.Path(task_agent.__file__).read_text(encoding="utf-8")
    # Routed through the factory, and NOT constructing BedrockModel directly —
    # a direct call would silently lose both the cap and prompt caching.
    assert "build_model(model_id=model_id" in src
    assert "BedrockModel(" not in src


# ---------------------------------------------------------------------------
# The source fix: code_interpreter writes DIRECTLY to artifacts/<session>/output/
# ---------------------------------------------------------------------------
# The flat-key defect (session a2460595): a script wrote to a session-less
# artifacts/<file> key and the render/download produced a wrong (presigned/SPA)
# URL. The prompt now injects the session-scoped output prefix per invocation so
# the write lands in the managed, CloudFront-served location from the start.


def test_output_prefix_is_session_scoped_with_explicit_id():
    assert _artifact_output_prefix("sess-123") == f"{task_agent.S3_PREFIX}sess-123/output/"


def test_output_prefix_sanitizes_slashes_in_session_id():
    assert _artifact_output_prefix("a/b").endswith("a_b/output/")


def test_output_prefix_falls_back_to_flat_without_a_session():
    # Backward-compatible: no session bound and none passed => the flat prefix. Bind an EMPTY
    # session so this is deterministic even if another test left a session on the contextvar.
    from jobs import context as _jc
    tok = _jc.set_request_context(actor_id="t", session_id="")
    try:
        assert _artifact_output_prefix("") == task_agent.S3_PREFIX
    finally:
        _jc.reset_request_context(tok)


def test_s3_guidance_directs_a_direct_session_scoped_write():
    g = _s3_context_guidance("sess-123")
    assert "artifacts/sess-123/output/<filename>" in g
    assert "DIRECTLY" in g
    assert "ContentType" in g
    # It must steer AWAY from the flat key that caused the bug.
    assert "do NOT write to a flat" in g


def test_resolved_prompt_threads_the_session_output_prefix():
    resolved = _resolve_system_prompt({"session_id": "sess-xyz"})
    assert "artifacts/sess-xyz/output/<filename>" in resolved
    # And it is not the old flat-key instruction.
    assert "/artifacts/<filename>" not in resolved


def test_default_constant_no_longer_carries_the_flat_write_instruction():
    # The flat "write ... to s3://.../artifacts/<filename>" recipe is gone from the constant;
    # file-output guidance is now session-scoped and appended per invocation.
    assert "artifact(operation='adopt'" not in DEFAULT_TASK_SYSTEM_PROMPT
    assert "S3 CONTEXT" not in DEFAULT_TASK_SYSTEM_PROMPT


def test_external_call_is_session_scoped_too():
    # External (persona) invocations with a session also get the scoped path + async block.
    resolved = _resolve_system_prompt(_external(system_prompt=PERSONA, session_id="ext-1"))
    assert "artifacts/ext-1/output/<filename>" in resolved
    assert "ASYNC JOBS:" in resolved


def test_s3_context_says_our_links_are_read_by_key(monkeypatch):
    """A link a cell assembles itself (CDN + prefix + name) is never presigned, and the CDN is private, so every
    node is told to read our links by key (staging c7d66001: the assemble step got 403 on all of its inputs)."""
    import task_agent

    monkeypatch.setattr(task_agent, "ARTIFACTS_DOMAIN", "cdn.example")
    monkeypatch.setattr(task_agent, "S3_BUCKET", "bkt")
    g = task_agent._s3_context_guidance("sid-1")
    assert "`https://cdn.example/<key>` are NOT public" in g and "read `<key>` from `bkt` with boto3" in g
    monkeypatch.setattr(task_agent, "ARTIFACTS_DOMAIN", "")
    assert "NOT public" not in task_agent._s3_context_guidance("sid-1")


def test_s3_context_says_where_skill_scripts_are_and_never_to_search_for_them():
    """A plan step has no skills, so twice in c7d66001 it went looking for `scripts/trim_plan.py` with
    `glob('/**/…', recursive=True)`, which walks /proc and never returns; the step ran out its 600 s."""
    import task_agent

    g = task_agent._s3_context_guidance("sid-1")
    assert "`ffmpeg` and `ffprobe` are on PATH" in g
    assert "`import trim_plan`" in g and "never search the filesystem" in g
