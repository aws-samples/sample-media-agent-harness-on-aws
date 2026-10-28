"""M2C catalog check (Req 8.15, 11.2, 11.3, 11.5; design Component 9 "Catalog").

Pure over a set of listed tool names. Registration, Retry, Re-add, and rebuild retry call it before writing
anything, and refuse with the missing tool names. A ``tools/list`` that did not complete is "unreadable", which
the caller reports with that wording rather than as a missing-tool list. The image model-capability check
(``get_model``) is separate because it needs the configured ``model_chain``.
"""
from __future__ import annotations

from typing import Iterable

_COMMON = ("hybrid-search___create_collection", "hybrid-search___index_content", "hybrid-search___get_job_status",
           "hybrid-search-admin___delete_collection", "hybrid-search-admin___configure_search_backend")
_WF = {
    "shot-segmentation": ("shot-segmentation___segment_shots", "shot-segmentation___get_job_status"),
    "speech-diarization": ("speech-diarization___diarize_speech", "speech-diarization___get_job_status"),
    "media-processing": ("media-processing___process_media", "media-processing___get_job_status"),
    "shot-metadata-enrichment": ("shot-metadata-enrichment___enrich_shot_metadata",
                                 "shot-metadata-enrichment___get_job_status"),
    "conversation-analysis": ("conversation-analysis___analyze_conversation", "conversation-analysis___get_job_status"),
    "bedrock-inference": ("bedrock-inference___run_inference", "bedrock-inference___get_job_status",
                          "bedrock-inference___get_model"),
}
_BY_KIND = {
    "video": ("shot-segmentation", "speech-diarization", "media-processing", "shot-metadata-enrichment",
              "conversation-analysis"),
    "image": ("bedrock-inference",),
    "audio": ("speech-diarization", "conversation-analysis"),
}

# Entity-metadata workflows (Phase 1), required ONLY when the ``entity_metadata`` flag is on — they are
# absent from some envs' toolkits, so making them unconditionally required would fail registration there
# (design: default OFF).
_ENTITY_WF = {
    "face-recognition": ("face-recognition___recognize_faces", "face-recognition___get_job_status"),
    "program-structure-analysis": ("program-structure-analysis___analyze_program_structure",
                                   "program-structure-analysis___get_job_status"),
    "cast-resolution": ("cast-resolution___resolve_cast", "cast-resolution___get_job_status"),
}
_ENTITY_BY_KIND = {
    "video": ("face-recognition", "program-structure-analysis", "cast-resolution"),
}

UNREADABLE = "the M2C tool catalog is unreadable"


def required_tools(kind: str, entity_metadata: bool = False) -> tuple[str, ...]:
    if kind not in _BY_KIND:
        raise ValueError(f"no ingestion pipeline for kind {kind!r}")
    tools = _COMMON + tuple(t for wf in _BY_KIND[kind] for t in _WF[wf])
    if entity_metadata:
        tools = tools + tuple(t for wf in _ENTITY_BY_KIND.get(kind, ()) for t in _ENTITY_WF[wf])
    return tools


def missing_tools(kinds: Iterable[str], listed: Iterable[str], entity_metadata: bool = False) -> tuple[str, ...]:
    """Required tools absent from the catalog, in a stable order (no duplicates).

    ``entity_metadata`` adds the Phase 1 entity-metadata workflows to the video requirement; off by
    default so an env without those toolkit tools still registers (the pipeline simply does not run them).
    """
    have = set(listed)
    out: list[str] = []
    for k in kinds:
        for t in required_tools(k, entity_metadata):
            if t not in have and t not in out:
                out.append(t)
    return tuple(out)


def refusal(missing: Iterable[str]) -> str:
    names = ", ".join(missing)
    return f"the M2C catalog is missing {names}" if names else ""
