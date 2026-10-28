"""Index_Document builder (Requirement 5, Requirement 8 criteria 9 and 11, Requirement 12 criterion 8).

Pure and deterministic (Req 5.20). Ported from the validated prototype
(``docs/prototypes/media-harness-projects/validation/docbuilder.py``) with the five fixes of design Component 10:

1. Duplicate spans keep the FIRST in build order and count the rest as omitted (Req 5.15); a shot links
   ``scene_parent_id`` only to a scene that survived (Req 5.1). The prototype's dict let a later duplicate
   replace the first, uncounted.
2. Speaker labels are numbered by each label's first non-empty turn, before span checks (Req 5.4).
3. Images compose Scene_Text from the parsed description object and take ``named_entities`` from its
   ``entities`` (Req 8.9, 8.11); the prototype indexed the raw text.
4. Truncated scenes are counted (Req 5.3).
5. (Record format lives in :mod:`ingestion.records`.)

Timestamps arrive already in whole milliseconds (:mod:`ingestion.adapters`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

from projects.document_locator import DOCUMENT_SCOPE, PAGE_STRIDE_MS, page_of_span  # noqa: F401 -- re-exported

from .adapters import EntityMentionIn, SceneIn, ShotIn, TopicIn, TurnIn

SCENE_TEXT_MAX = 8000
SCOPES = ("shot", "scene", "utterance", "topic")
_ORDER = {t: i for i, t in enumerate(SCOPES)}

# ----------------------------------------------------------------------------- document page locator
# A document (PDF) produces only ``scene`` documents (the strongest configured retrieval paths), with the
# PAGE NUMBER encoded in the span so a search hit can open the file at the right page WITHOUT adding a
# ``document`` scope or a ``page`` column -- both are M2C-owned and immutable per collection (Requirement
# 16, design §21). One stride per page, the within-page chunk index as the offset, so every chunk of a
# document keeps a distinct, contract-valid span (0 <= start < end <= duration):
#
#   start_ms = (page - 1) * PAGE_STRIDE_MS + chunk_index   (page 1-based, chunk 0-based within the page)
#   end_ms   = start_ms + 1
#   duration = max_page * PAGE_STRIDE_MS
#   page     = start_ms // PAGE_STRIDE_MS + 1              (decode; also ui/src/shared/pageLocator.ts)
#
# PAGE_STRIDE_MS is cross-pinned with the SPA constant by agent/tests/fixtures/page_locator.json so the two
# never drift (the same discipline as projects/storage_layout).
# PAGE_STRIDE_MS, DOCUMENT_SCOPE and page_of_span live in projects/document_locator.py (shared with search).



@dataclass(frozen=True)
class BuildResult:
    documents: tuple[dict, ...]
    omitted: int
    truncated_scenes: int

    def counts(self) -> dict[str, int]:
        return {t: sum(d["doc_type"] == t for d in self.documents) for t in SCOPES}


# ------------------------------------------------------------------------------------------- text
def clean_entities(entities: Iterable[Any]) -> str:
    """Req 5.7: trim, comma → space, collapse whitespace, drop empty, case-insensitive dedupe (first casing)."""
    out: list[str] = []
    seen: set[str] = set()
    for e in entities or ():
        if not isinstance(e, str):
            continue
        norm = " ".join(e.replace(",", " ").split())
        if norm and norm.casefold() not in seen:
            seen.add(norm.casefold())
            out.append(norm)
    return ", ".join(out)


def _join(values: Any) -> str:
    if isinstance(values, str):
        return values.strip()
    return ", ".join(v.strip() for v in values or () if isinstance(v, str) and v.strip())


def scene_text(description: str = "", objects: Sequence[str] = (), setting: str = "",
               on_screen_text: Any = "", brands: Sequence[str] = (), sentiment: str = "") -> tuple[str, bool]:
    """Req 5.2 / 5.3 → ``(text, truncated)``. Lines in fixed order, blank ones omitted, joined by ``\\n``."""
    lines: list[str] = []
    for label, value in ((None, description), ("Objects", objects), ("Setting", setting),
                         ("On-screen text", on_screen_text), ("Brands", brands), ("Sentiment", sentiment)):
        v = _join(value)
        if v:
            lines.append(v if label is None else f"{label}: {v}")
    text = "\n".join(lines)
    if len(text) <= SCENE_TEXT_MAX:
        return text, False
    acc: list[str] = []
    for ln in lines:
        if len("\n".join(acc + [ln])) > SCENE_TEXT_MAX:
            break
        acc.append(ln)
    return ("\n".join(acc) if acc else text[:SCENE_TEXT_MAX]), True


def parent_ref(doc_type: str, start_ms: int, end_ms: int) -> str:
    return f"{doc_type}:{start_ms}:{end_ms}"


def _text_doc(asset_id: str, doc_type: str, s: int, e: int, text: str, entities: str = "") -> dict:
    content = [{"kind": "text", "field": "text", "value": text},
               {"kind": "text", "field": "vector", "value": text}]
    if entities:
        content.append({"kind": "text", "field": "named_entities", "value": entities})
    return {"asset_id": asset_id, "doc_type": doc_type, "start_ms": s, "end_ms": e, "content": content}


def _shot_doc(asset_id: str, s: int, e: int, frame_uri: str, scene_ref: Optional[str]) -> dict:
    fields = {"frame_s3_key": frame_uri.split("/", 3)[3]}
    if scene_ref:
        fields["scene_parent_id"] = scene_ref
    return {"asset_id": asset_id, "doc_type": "shot", "start_ms": s, "end_ms": e,
            "content": [{"kind": "image", "field": "vector", "value": frame_uri}], "fields": fields}


def _valid(s: Optional[int], e: Optional[int], d: int) -> bool:
    return isinstance(s, int) and isinstance(e, int) and 0 <= s < e <= d


def _dedupe(docs: list[dict]) -> tuple[list[dict], int]:
    """Stable sort into build order, then keep the first of each (doc_type, start_ms, end_ms) (Req 5.15)."""
    docs = sorted(docs, key=lambda d: (_ORDER[d["doc_type"]], d["start_ms"], d["end_ms"]))
    out, seen, dropped = [], set(), 0
    for d in docs:
        k = (d["doc_type"], d["start_ms"], d["end_ms"])
        if k in seen:
            dropped += 1
            continue
        seen.add(k)
        out.append(d)
    return out, dropped


def _best_overlap(s: int, e: int, cands: list[dict]) -> Optional[dict]:
    """Greatest overlap ≥ 1 ms; ties → earlier start_ms (then earlier end_ms: build order)."""
    best, key = None, None
    for c in cands:
        ov = min(e, c["end_ms"]) - max(s, c["start_ms"])
        if ov < 1:
            continue
        k = (ov, -c["start_ms"], -c["end_ms"])
        if key is None or k > key:
            best, key = c, k
    return best


def _overlapping_names(s: int, e: int, mentions: Sequence[EntityMentionIn]) -> list[str]:
    """Every mention name whose span overlaps ``[s, e)`` by ≥ 1 ms, in mention order (the same ≥ 1 ms rule
    :func:`_best_overlap` uses). Used to fold face/cast/program entities into a scene's ``named_entities``."""
    return [m.name for m in mentions if min(e, m.end_ms) - max(s, m.start_ms) >= 1]


# ------------------------------------------------------------------------------------- speech docs
def _speech_docs(asset_id: str, turns: Sequence[TurnIn], topics: Sequence[TopicIn], d: int,
                 scenes: list[dict]) -> tuple[list[dict], list[dict], int]:
    omitted = 0
    tdocs: list[dict] = []
    for tp in topics:
        text = (tp.summary or "").strip()
        if not text or not _valid(tp.start_ms, tp.end_ms, d):
            omitted += 1
            continue
        tdocs.append(_text_doc(asset_id, "topic", tp.start_ms, tp.end_ms, text, clean_entities(tp.entities)))
    tdocs, dup = _dedupe(tdocs)
    omitted += dup

    # Fix 2: number labels by first non-empty turn over the WHOLE transcript, before span checks.
    labels: dict[str, int] = {}
    for t in turns:
        if (t.text or "").strip() and t.speaker is not None and t.speaker not in labels:
            labels[t.speaker] = len(labels) + 1
    udocs: list[dict] = []
    for t in turns:
        text = (t.text or "").strip()
        if not text or not _valid(t.start_ms, t.end_ms, d):
            omitted += 1
            continue
        if t.speaker is not None:
            text = f"Speaker {labels[t.speaker]}: {text}"
        doc = _text_doc(asset_id, "utterance", t.start_ms, t.end_ms, text)
        fields = {}
        sc = _best_overlap(t.start_ms, t.end_ms, scenes)
        tc = _best_overlap(t.start_ms, t.end_ms, tdocs)
        if sc:
            fields["scene_parent_id"] = parent_ref("scene", sc["start_ms"], sc["end_ms"])
        if tc:
            fields["topic_parent_id"] = parent_ref("topic", tc["start_ms"], tc["end_ms"])
        if fields:
            doc["fields"] = fields
        udocs.append(doc)
    udocs, dup = _dedupe(udocs)
    return udocs, tdocs, omitted + dup


# ----------------------------------------------------------------------------------------- builders
def build_video(asset_id: str, shots: Sequence[ShotIn], scenes: Sequence[SceneIn], turns: Sequence[TurnIn],
                topics: Sequence[TopicIn], duration_ms: int, *,
                people: Sequence[EntityMentionIn] = (), cast: Sequence[EntityMentionIn] = (),
                segments: Sequence[EntityMentionIn] = ()) -> BuildResult:
    """Req 5.1–5.22, 5.24. Pass empty ``turns``/``topics`` for a video with no detectable speech (Req 5.18).

    ``people`` (face-recognition identities), ``cast`` (cast-resolution character/actor names) and
    ``segments`` (program-structure Segment / Program-element labels) are timed entity mentions
    (:class:`EntityMentionIn`). Each scene's ``named_entities`` is its enrichment entities PLUS every
    mention whose span overlaps the scene (de-duplicated and cleaned by :func:`clean_entities`), so the
    entities are searchable through ``scene_named_entities_fts`` instead of a side-channel asset blob.
    All three default empty, so a caller that passes none (the flag-off path) yields byte-identical scene
    documents. Structural ``segment_parent_id`` grouping of a Segment's member scenes is a future
    enhancement (needs a hybrid-search backend change; ``contract`` rejects the field today)."""
    d = int(duration_ms)
    omitted = truncated = 0
    sdocs: list[dict] = []
    trunc_flags: dict[int, bool] = {}
    for sc in scenes:
        text, cut = scene_text(sc.description, sc.objects, sc.setting, sc.on_screen_text, sc.brands, sc.sentiment)
        if not text or not _valid(sc.start_ms, sc.end_ms, d):
            omitted += 1
            continue
        folded = (_overlapping_names(sc.start_ms, sc.end_ms, people)
                  + _overlapping_names(sc.start_ms, sc.end_ms, cast)
                  + _overlapping_names(sc.start_ms, sc.end_ms, segments))
        entities = clean_entities(list(sc.entities) + folded)
        doc = _text_doc(asset_id, "scene", sc.start_ms, sc.end_ms, text, entities)
        trunc_flags[id(doc)] = cut
        sdocs.append(doc)
    sdocs, dup = _dedupe(sdocs)
    omitted += dup
    truncated = sum(trunc_flags[id(doc)] for doc in sdocs)
    scene_spans = {(x["start_ms"], x["end_ms"]) for x in sdocs}

    shdocs: list[dict] = []
    for sh in shots:
        parts = (sh.frame_uri or "").split("/", 3)
        if not sh.frame_uri.startswith("s3://") or len(parts) < 4 or not parts[2] or not parts[3] \
                or not _valid(sh.start_ms, sh.end_ms, d):
            omitted += 1
            continue
        ref = parent_ref("scene", sh.start_ms, sh.end_ms) if (sh.start_ms, sh.end_ms) in scene_spans else None
        shdocs.append(_shot_doc(asset_id, sh.start_ms, sh.end_ms, sh.frame_uri, ref))
    shdocs, dup = _dedupe(shdocs)
    omitted += dup

    udocs, tdocs, om = _speech_docs(asset_id, turns, topics, d, sdocs)
    omitted += om
    return BuildResult(tuple(shdocs + sdocs + udocs + tdocs), omitted, truncated)


def build_audio(asset_id: str, turns: Sequence[TurnIn], topics: Sequence[TopicIn], duration_ms: int) -> BuildResult:
    """Req 5.23 / 12.8: only utterance and topic documents; no utterance carries ``scene_parent_id``."""
    udocs, tdocs, omitted = _speech_docs(asset_id, turns, topics, int(duration_ms), [])
    return BuildResult(tuple(udocs + tdocs), omitted, 0)


def _image_list(v: Any) -> tuple[str, ...]:
    if isinstance(v, str):
        return (v,)
    if isinstance(v, list):
        return tuple(x for x in v if isinstance(x, str))
    return ()


def _image_str(v: Any) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        return ", ".join(x.strip() for x in v if x.strip())
    return ""


def build_image(asset_id: str, rendition_uri: str, description: dict) -> BuildResult:
    """Req 8.11: a ``shot`` (0–1 ms, the Search_Rendition) and a ``scene`` (Scene_Text from the description).

    ``description`` is the parsed object whose ``description`` is a non-empty string (checked by the caller,
    Req 8.10). Members absent or of the wrong type are ignored (Req 8.9).
    """
    text, cut = scene_text(_image_str(description.get("description")), _image_list(description.get("objects")),
                           _image_str(description.get("setting")), _image_str(description.get("on_screen_text")),
                           _image_list(description.get("brands")), _image_str(description.get("sentiment")))
    shot = _shot_doc(asset_id, 0, 1, rendition_uri, parent_ref("scene", 0, 1))
    if not text:
        return BuildResult((), 2, 0)
    scene = _text_doc(asset_id, "scene", 0, 1, text, clean_entities(_image_list(description.get("entities"))))
    return BuildResult((shot, scene), 0, int(cut))


def build_document(asset_id: str, chunks: Sequence[Mapping[str, Any]]) -> BuildResult:
    """A PDF's chunks → ``scene`` documents with the page locator encoded in the span (Requirement 16).

    Each chunk is ``{page, chunk_index, heading, text}`` (:mod:`ingestion.pdf`). The heading is prefixed to
    the text so FTS and the model see the page's context, except when the text already begins with it (the
    first chunk of a page starts at the heading). ``named_entities`` is empty for v1 (no entity extraction).
    A chunk whose text is blank is dropped and counted as omitted; duplicate spans cannot occur because
    every ``(page, chunk_index)`` is unique, but ``_dedupe`` keeps the invariant explicit.
    """
    docs: list[dict] = []
    omitted = 0
    for ch in chunks:
        try:
            page = int(ch["page"])
            ci = int(ch["chunk_index"])
        except (KeyError, TypeError, ValueError):
            omitted += 1
            continue
        text = str(ch.get("text") or "").strip()
        heading = str(ch.get("heading") or "").strip()
        if not text or page < 1 or ci < 0 or ci >= PAGE_STRIDE_MS:
            omitted += 1
            continue
        body = text if (heading and text.startswith(heading)) else (f"{heading}\n{text}" if heading else text)
        start = (page - 1) * PAGE_STRIDE_MS + ci
        docs.append(_text_doc(asset_id, DOCUMENT_SCOPE, start, start + 1, body))
    docs, dropped = _dedupe(docs)
    return BuildResult(tuple(docs), omitted + dropped, 0)
