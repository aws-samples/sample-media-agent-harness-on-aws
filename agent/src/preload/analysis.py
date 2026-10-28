"""Build a preanalyzed asset's reusable ``steps[]`` (design Part 2, ``preanalyzed``).

The ingestion tick never cares *who* produced a step's outputs — it reuses a step record while its status is
``SUCCEEDED`` and its ``inputs`` match what a fresh submission would pass (``reducer._stage`` / ``_current``).
So a preanalyzed asset is registered as a normal ``queued`` asset whose ``steps`` are pre-seeded as
``SUCCEEDED`` records pointing at the uploaded analysis files, with ``facts`` and ``inputs`` set so
``reducer.plan_video`` returns ``build=True`` with nothing to submit, and the deployed tick builds + indexes
with **no analysis job submitted**.

Facts are computed with the SAME adapters the live ``_observe`` uses (``adapters.duration_ms`` /
``has_speech`` / ``technical_from_probe``), which doubles as shape validation: a file whose shape the adapter
cannot read fails the asset cleanly here rather than indexing garbage.

Scope: ``video`` (the design's worked example and the ontology context). ``preanalyzed`` for other kinds
raises :class:`PreanalyzedError` so the caller fails that one asset cleanly instead of guessing.
"""
from __future__ import annotations

import hashlib
import posixpath
from typing import Any, Mapping, Optional

from ingestion import adapters as A
from ingestion import reducer
from ingestion import workflows as W

# manifest ``analysis`` key → (pipeline step, output label the tick reads by). The manifest key ``enrichment``
# maps to the ENRICHMENT step's ``shots`` label (``WORKFLOWS[ENRICHMENT].labels == ("shots",)``), and
# ``frames`` to the frame-capture ``frames`` group. ``probe`` is NOT a step output — it feeds
# ``facts.technical`` on frame capture, exactly as ``_observe`` does.
VIDEO_LABELS: dict[str, tuple[str, str]] = {
    "shot-list": (W.SHOT_SEGMENTATION, "shot-list"),
    "transcript": (W.SPEECH_DIARIZATION, "transcript"),
    "frames": (W.FRAME_CAPTURE, W.FRAME_GROUP),
    "enrichment": (W.ENRICHMENT, "shots"),
    "analysis": (W.CONVERSATION_ANALYSIS, "analysis"),
}
PROBE_KEY = "probe"


class PreanalyzedError(ValueError):
    """A preanalyzed bundle whose shape the pipeline could not reuse (fail this one asset, not the deploy)."""


def _job_id(asset_id: str, step: str) -> str:
    """A synthetic but STABLE job id for a seeded step (re-deriving it on a re-deploy is the same id)."""
    h = hashlib.sha256(f"preload|{asset_id}|{step}".encode()).hexdigest()[:16]
    return f"preload-{h}"


def _prefix_of(uri: str) -> str:
    """The ``s3://bucket/dir`` prefix of an object uri (``outputPrefix`` the live steps carry)."""
    return posixpath.dirname(uri)


def _rec(step: str, *, job_id: str, inputs: tuple[str, ...], label: str, uri: str,
         facts: Optional[dict] = None) -> dict:
    return {
        "status": reducer.SUCCEEDED,
        "jobId": job_id,
        "runSeq": 0,            # produced before this asset's first run; never a failure of run 1
        "inputs": list(inputs),
        "outputs": {label: uri},
        "outputPrefix": _prefix_of(uri),
        "facts": facts or {},
        "callFailures": 0,
        "notBefore": 0,
    }


def build_video_steps(asset_id: str, uris: Mapping[str, str], docs: Mapping[str, Any]) -> dict[str, dict]:
    """The ``steps`` map for a preanalyzed VIDEO asset, or raise :class:`PreanalyzedError`.

    ``uris``  — manifest ``analysis`` key → the uploaded object's ``s3://`` uri (already placed in the bucket).
    ``docs``  — the PARSED JSON for the keys needed to compute facts: ``shot-list`` and (if present)
                ``transcript`` and ``probe``. Only these three are read here; the tick reads the rest at build.

    Builds the same five-or-four records a live run would leave: wave 1 (shot-segmentation,
    speech-diarization, frame-capture) with ``inputs == ()``; then shot-metadata-enrichment with
    ``inputs == (shotJob, diarJob)`` when the source has speech else ``(shotJob,)``; and
    conversation-analysis with ``inputs == (diarJob,)`` only when there is speech.
    """
    for required in ("shot-list", "frames", "enrichment"):
        if required not in uris:
            raise PreanalyzedError(f"preanalyzed video is missing the {required!r} analysis output")

    shot_list = docs.get("shot-list")
    duration_ms = A.duration_ms((shot_list or {}).get("actual_duration_sec") if isinstance(shot_list, Mapping) else None)
    if duration_ms is None:
        raise PreanalyzedError("shot-list is missing a readable 'actual_duration_sec' (video duration)")

    shot_job = _job_id(asset_id, W.SHOT_SEGMENTATION)
    diar_job = _job_id(asset_id, W.SPEECH_DIARIZATION)

    # Diarization governs whether conversation-analysis is part of the plan (reducer.has_speech).
    has_transcript = "transcript" in uris
    transcript = docs.get("transcript") if has_transcript else None
    speech = bool(has_transcript and A.has_speech(transcript if isinstance(transcript, Mapping) else None))
    if speech and "analysis" not in uris:
        raise PreanalyzedError("transcript shows speech but no 'analysis' (conversation-analysis) output is provided")

    steps: dict[str, dict] = {}

    step, label = VIDEO_LABELS["shot-list"]
    steps[step] = _rec(step, job_id=shot_job, inputs=(), label=label,
                       uri=uris["shot-list"], facts={"durationMs": duration_ms})

    # speech-diarization: SUCCEEDED when a transcript was provided, else the silent-video NO_AUDIO twin.
    step, label = VIDEO_LABELS["transcript"]
    if has_transcript:
        diar_facts = {"hasSpeech": speech,
                      "durationMs": A.duration_ms((transcript or {}).get("audio_duration_sec")
                                                  if isinstance(transcript, Mapping) else None)}
        steps[step] = _rec(step, job_id=diar_job, inputs=(), label=label, uri=uris["transcript"], facts=diar_facts)
    else:
        steps[step] = {"status": reducer.NO_AUDIO, "jobId": diar_job, "runSeq": 0, "inputs": [],
                       "outputs": {}, "facts": {"hasSpeech": False}, "callFailures": 0, "notBefore": 0}

    step, label = VIDEO_LABELS["frames"]
    fc_facts: dict = {}
    probe = docs.get(PROBE_KEY)
    if probe is not None:
        tech = A.technical_from_probe(probe)
        if tech:
            fc_facts["technical"] = tech
    steps[step] = _rec(step, job_id=_job_id(asset_id, W.FRAME_CAPTURE), inputs=(), label=label,
                       uri=uris["frames"], facts=fc_facts)

    step, label = VIDEO_LABELS["enrichment"]
    enrich_inputs = (shot_job, diar_job) if speech else (shot_job,)
    steps[step] = _rec(step, job_id=_job_id(asset_id, W.ENRICHMENT), inputs=enrich_inputs, label=label,
                       uri=uris["enrichment"])

    if speech:
        step, label = VIDEO_LABELS["analysis"]
        steps[step] = _rec(step, job_id=_job_id(asset_id, W.CONVERSATION_ANALYSIS), inputs=(diar_job,),
                           label=label, uri=uris["analysis"])

    return steps


def verify_builds(steps: Mapping[str, dict], cfg: reducer.Config = reducer.Config()) -> None:
    """Assert the seeded ``steps`` make ``plan_video`` build with nothing to submit, or raise.

    This is the load-bearing invariant (design Part 2): if a future reducer change breaks reuse, a preload
    would silently resubmit analysis jobs. Callers run this right after :func:`build_video_steps`.
    """
    plan = reducer.plan_video(steps, now=0.0, run_seq=1, cfg=cfg)
    if plan.submit or plan.fail or not plan.build:
        raise PreanalyzedError(
            f"seeded steps do not build without submission: submit={plan.submit} fail={plan.fail} build={plan.build}")
