"""M2C analysis workflows: what each step submits and polls (design Component 8, "M2C submission arguments").

Pure. The only per-asset inputs are the source URI and upstream outputs (Req 4.10, 8.8, 12.7); no
``history_scope`` (asset-specific, Req 4.10 forbids it) and an explicit ``use_batch: false`` (M2C's default is
``true``). A rerun adds an ``idempotency_token`` so M2C, which absorbs identical parameters into an existing run
(including a failed one), starts a new run; a first run passes none (Req 7.15, design D6).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

SHOT_SEGMENTATION = "shot-segmentation"
SPEECH_DIARIZATION = "speech-diarization"
FRAME_CAPTURE = "frame capture"
ENRICHMENT = "shot-metadata-enrichment"
CONVERSATION_ANALYSIS = "conversation-analysis"
IMAGE_DESCRIPTION = "image description"
# Entity-metadata workflows (Phase 1, flag-gated): their outputs are folded into the scene documents'
# searchable ``named_entities``, not stored as a side-channel asset blob.
FACE_RECOGNITION = "face-recognition"            # Person identities (incl. celebrity recognition)
PROGRAM_STRUCTURE = "program-structure-analysis"  # Program elements / segments
CAST_RESOLUTION = "cast-resolution"              # Characters (actor -> role)

TERMINAL = frozenset({"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"})
UNSUCCESSFUL = frozenset({"FAILED", "TIMED_OUT", "ABORTED"})

FRAME_GROUP = "frames"


@dataclass(frozen=True)
class WorkflowSpec:
    step: str
    submit_tool: str
    status_tool: str
    # The documented output label(s) the pipeline reads (Req 4.11). Selected by label, never by position.
    labels: tuple[str, ...]


WORKFLOWS: dict[str, WorkflowSpec] = {
    SHOT_SEGMENTATION: WorkflowSpec(SHOT_SEGMENTATION, "shot-segmentation___segment_shots",
                                    "shot-segmentation___get_job_status", ("shot-list",)),
    SPEECH_DIARIZATION: WorkflowSpec(SPEECH_DIARIZATION, "speech-diarization___diarize_speech",
                                     "speech-diarization___get_job_status", ("transcript",)),
    FRAME_CAPTURE: WorkflowSpec(FRAME_CAPTURE, "media-processing___process_media",
                                "media-processing___get_job_status", (FRAME_GROUP,)),
    ENRICHMENT: WorkflowSpec(ENRICHMENT, "shot-metadata-enrichment___enrich_shot_metadata",
                             "shot-metadata-enrichment___get_job_status", ("shots",)),
    CONVERSATION_ANALYSIS: WorkflowSpec(CONVERSATION_ANALYSIS, "conversation-analysis___analyze_conversation",
                                        "conversation-analysis___get_job_status", ("analysis",)),
    IMAGE_DESCRIPTION: WorkflowSpec(IMAGE_DESCRIPTION, "bedrock-inference___run_inference",
                                    "bedrock-inference___get_job_status", ("response",)),
    # Entity-metadata workflows. Tool names, args and output labels verified live against the staging M2C
    # gateway target config + describe_tool contracts (docs/entity-metadata-workflows.md). Only the
    # one labelled output each stage READS is declared here (mirrors the other specs).
    FACE_RECOGNITION: WorkflowSpec(FACE_RECOGNITION, "face-recognition___recognize_faces",
                                   "face-recognition___get_job_status", ("face-metadata",)),
    PROGRAM_STRUCTURE: WorkflowSpec(PROGRAM_STRUCTURE, "program-structure-analysis___analyze_program_structure",
                                    "program-structure-analysis___get_job_status", ("structure",)),
    CAST_RESOLUTION: WorkflowSpec(CAST_RESOLUTION, "cast-resolution___resolve_cast",
                                  "cast-resolution___get_job_status", ("persons",)),
}
IMAGE_RENDITION = "image rendition"  # done in IngestTick itself (Pillow), not an M2C workflow
DOCUMENT_EXTRACT = "document extract"  # done in IngestTick itself (pypdf), not an M2C workflow


@dataclass(frozen=True)
class FrameConfig:
    interval_sec: float = 1.0
    width: int = 960
    height: int = 540


def idempotency_token(run_seq: int, uuid_hex: str) -> str:
    """Stable within one run, different from every earlier run of the step (Req 8.8)."""
    return f"r{int(run_seq)}-{uuid_hex}"


def _with_token(args: dict, token: Optional[str]) -> dict:
    if token:
        args["idempotency_token"] = token
    return args


def shot_segmentation_args(source_uri: str, token: Optional[str] = None) -> dict:
    return _with_token({"asset_s3uri": source_uri}, token)


def speech_diarization_args(source_uri: str, token: Optional[str] = None) -> dict:
    return _with_token({"asset_s3uri": source_uri}, token)


def frame_capture_args(source_uri: str, frames: FrameConfig = FrameConfig(), token: Optional[str] = None) -> dict:
    return _with_token({
        "asset_s3uri": source_uri, "width": frames.width, "height": frames.height,
        "output_groups": [{"group_name": FRAME_GROUP, "format": "jpeg",
                           "frame_interval_sec": frames.interval_sec}],
    }, token)


def enrichment_args(shots_run_prefix: str, transcript_uri: Optional[str] = None,
                    token: Optional[str] = None) -> dict:
    """``shots_run_prefix`` is the shot-segmentation run's ``output_s3_prefix`` (no trailing slash)."""
    args: dict = {"shot_segmentation_s3uri": shots_run_prefix.rstrip("/"), "use_batch": False}
    if transcript_uri:
        args["transcript_s3uri"] = transcript_uri
    return _with_token(args, token)


def conversation_analysis_args(transcript_uri: str, token: Optional[str] = None) -> dict:
    return _with_token({"transcript_s3uri": transcript_uri}, token)


# --------------------------------------------------------------------------- entity metadata (Phase 1)
def face_recognition_args(source_uri: str, token: Optional[str] = None) -> dict:
    """face-recognition needs only the source URI (``asset_s3uri``); it runs its own frame extraction.

    ``celebrity_recognition`` defaults true on the tool, so celebrity naming happens without passing it.
    No ``use_batch`` (this tool does not take one).
    """
    return _with_token({"asset_s3uri": source_uri}, token)


def program_structure_args(shots_run_prefix: str, shot_metadata_run_prefix: str,
                           conversation_run_prefix: Optional[str] = None,
                           transcript_uri: Optional[str] = None, token: Optional[str] = None) -> dict:
    """``shots_run_prefix`` and ``shot_metadata_run_prefix`` are REQUIRED run prefixes (no trailing slash).

    ``mode`` is left to the tool's ``auto`` default. Conversation-analysis + transcript widen the
    products (news detection, gap map) and are passed only when the source had speech. No ``use_batch``.
    """
    args: dict = {"shot_segmentation_s3uri": shots_run_prefix.rstrip("/"),
                  "shot_metadata_s3uri": shot_metadata_run_prefix.rstrip("/")}
    if conversation_run_prefix:
        args["conversation_analysis_s3uri"] = conversation_run_prefix.rstrip("/")
    if transcript_uri:
        args["transcript_s3uri"] = transcript_uri
    return _with_token(args, token)


def cast_resolution_args(face_run_prefix: str, shots_run_prefix: str, shot_metadata_run_prefix: str,
                         diarization_run_prefix: Optional[str] = None, token: Optional[str] = None) -> dict:
    """Bind identities into person/character entities. All inputs are sibling RUN PREFIXES.

    face-recognition is the always-present identity source for video; shot-segmentation is REQUIRED for
    the on-screen text reads that yield character/cast pairs, and shot-metadata supplies overlay-text
    names. speech-diarization is added only when the source had speech. ``use_batch``/``read_onscreen_text``
    are left to the tool's defaults (true), so character pairs and batch pricing apply.
    """
    args: dict = {"face_recognition_s3uri": face_run_prefix.rstrip("/"),
                  "shot_segmentation_s3uri": shots_run_prefix.rstrip("/"),
                  "shot_metadata_s3uri": shot_metadata_run_prefix.rstrip("/")}
    if diarization_run_prefix:
        args["speech_diarization_s3uri"] = diarization_run_prefix.rstrip("/")
    return _with_token(args, token)


# speech-diarization's failure text for a source with no decodable audio stream. Matched so a silent video is
# indexed visually instead of failing (Req 7.16). Kept narrow: an unmatched diarization failure still fails the
# asset, which is the safe direction. The live silent-video test records the real text verbatim.
_NO_AUDIO_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"no (decodable )?audio stream",
    r"does not (contain|have) (an |any )?audio( stream)?",
    r"has no audio",
    r"Stream map '0:a[^']*' matches no streams",
    r"Output file #?\d+ does not contain any stream",
    r"has_audio['\"]?\s*[:=]\s*false",
    # Audio present but no speech in it (music-only reel, session dcd11946's meridian_3d_reel.mp4): the VAD
    # finds no turns and the chunker raises. Same outcome as no audio: index the video visually.
    r"No VAD turns found",
    # M2C's probe_media_file/parse_output_groups when the source has no audio track (2026-10, project
    # p925d3f6's shot-segmentation proxy/segment_0000.mp4): "No output_groups are compatible with source (no audio)".
    r"compatible with source \(no audio\)",
))


def is_no_audio_stream(error_text: Optional[str]) -> bool:
    text = error_text or ""
    return any(p.search(text) for p in _NO_AUDIO_PATTERNS)


# speech-diarization's failure text for an AUDIO asset that holds sound but no usable speech (music and sound-effect
# stems; staging project p925d3f6's `audio_sfx.wav` -> VAD, `audio_music.wav` -> embeddings, 2026-10-09). There is
# nothing to transcribe, so the asset is kept but not indexed (``stored`` + ``indexSkippedReason: no_speech``) until
# sound tags can be indexed -- never ``failed``.
#
# Deliberately NOT the no-audio-stream patterns above: for a VIDEO those mean "silent video, index it visually", but
# for an AUDIO asset they mean the file has no decodable audio -- a broken upload, which must stay ``failed`` so the
# user sees it and re-uploads. "No high-quality embeddings found" fires after VAD found turns, so it can also be very
# short or noisy speech; the UI therefore says "no usable speech", not "music".
_NO_SPEECH_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"No VAD turns found",                 # VAD kept nothing (the sfx stem)
    r"No high-quality embeddings found",   # speaker clustering had nothing usable (the music stem)
    # The speech-embedding step found no sentences to embed: audio, but no speech (alpha.20261014a wording; prod
    # 111-adc-1808.mp4, AAC stereo, 2026-10-15).
    r"No sentences in \S*transcript",
    # alpha.20261017a names it: "NoSpeechDetected: no usable speech reached clustering — 0 high-quality embeddings"
    # (prod `Separate Audio Stems (audio_sfx).wav`, 2026-10-17 and 2026-10-19, both marked failed).
    r"\bNoSpeechDetected\b",
    r"no usable speech",
))


def is_no_speech(error_text: Optional[str]) -> bool:
    """A diarization failure meaning "this audio holds no usable speech". Never true for a missing audio stream."""
    text = error_text or ""
    return any(p.search(text) for p in _NO_SPEECH_PATTERNS)
