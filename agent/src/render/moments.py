"""Moment selection and the Moments_Gallery manifest builder (design Component 6, Req 3.1-3.6, 3.9,
3.18, 3.19, 3.26).

Pure module, no I/O. The Moments_Rule (`hooks.actions.moments_render`, Task 7.6/7.7) is the only
caller: at Main_Agent `session_end` it hands this module the turn's :class:`TurnOutcome`
(Component 4), takes a :class:`MomentSelection`, and turns it into one ``clip-gallery`` manifest that
the render-batch flush writes to S3. Everything here is deterministic and side-effect free, so the
action can run it on the event loop and the property tests can drive it with hand-built outcomes.

Three invariants shape the code:

- **Only ledger Hits, only full Hits** (Req 3.1, 3.19). Both selection paths read
  ``SessionHit.hit``, the full Hit exactly as delivered. A Hit is recognized by shape
  (``verify.is_hit``), never by the tool that produced it, so a Compact_Hit, a tool result, or the
  model's text is never a source. A Hit removed by the Asset_Kind_Filter or cut by ``k`` never
  entered the ledger, so it cannot become a Moment.
- **Eligibility is the Project, the shape, and the status** (Req 3.2). A Hit becomes a Moment only
  under the turn's Active_Project, when it is a well-formed Hit, and when its resolved
  Project_Asset is not ``removed``. Per the user decision, ``removed`` is the *only* excluded
  status: ``failed``, ``queued``, ``analyzing`` and ``indexing`` assets stay eligible.
- **No URL, by construction** (Req 3.26). A :class:`Moment` and the manifest carry only typed
  fields — ids, ints, the registry Display_Name, ``source_index``, the score, derived text, and an
  :class:`AssetReference` (``project_id``, ``asset_id`` and, for a shot Hit only, a
  ``frame_s3_key`` bucket key). No ``src``/``url`` key is ever written; the SPA mints URLs itself.
"""
from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, Optional

from project_search.citations import TRIM_CHARS
from project_search.compact import serialize_entities
from project_search.verify import AssetReference, is_hit

if TYPE_CHECKING:  # typing only; `turn_end` (Component 4, Task 6.3) may not exist yet at import time
    from project_search.assets import ResolvedAsset
    from project_search.session_hits import SessionHit
    from project_search.turn_end import TurnOutcome

# A turn renders at most this many Moment cards; a distinct Moment beyond it sets `truncated`. The SPA pages
# them (24 at a time), so this bounds the manifest, not the screen.
MOMENTS_MAX = 200
# Matched text longer than this many code points is cut and flagged (keeps the manifest small).
MOMENT_TEXT_MAX = 500
# The tab's pre-filled search query is cut to this many code points (the tab's own box accepts 200).
QUERY_MAX = 200
# At most this many distinct highlight terms per card.
HIGHLIGHT_TERMS_MAX = 20
# A highlight term longer than this many code points is dropped (defends against runaway markup).
HIGHLIGHT_TERM_MAX_CHARS = 100

_REMOVED_STATUS = "removed"
_SHOT_SOURCE = "shot"
_ELLIPSIS = "\u2026"
_HIGHLIGHT_MAX_DEPTH = 3  # `highlights` nesting (str / list / dict) is walked no deeper than this

# Subtitle shown under the "Moments" tab title (see `subtitle_for`).
_SUBTITLE_CITED = "Cited in this answer"
_SUBTITLE_SEARCHED = "Matched by this turn's searches"

_EM_RE = re.compile(r"<em>(.*?)</em>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]*>")
_TRIM_SET = frozenset(TRIM_CHARS)


@dataclass(frozen=True)
class Moment:
    """One Moments_Gallery card. Carries no URL — ``ref`` names the Project_Asset instead."""

    key: tuple[str, int, int]        # (asset_id, start_ms, end_ms): card identity (Req 3.5)
    ref: AssetReference              # verify.AssetReference; never a URL
    kind: str                        # the Project_Asset's kind from the registry
    display_name: str                # the registry Display_Name (not the Hit's title)
    start_ms: int
    end_ms: int
    source_index: Optional[str]
    final_score: float
    text: str                        # matched_text(hit)[0]
    text_truncated: bool
    highlight_terms: list[str]
    citation: Optional[str]          # basis "cited" only: the text of the Moment's first Verified_Citation
    found_by: tuple = ()             # which searches matched it: "meaning" (vector) / "keyword" (full text)


@dataclass(frozen=True)
class MomentSelection:
    """The turn's chosen Moments and the basis they were chosen on."""

    basis: str                       # "cited" (≥1 cited card first) | "searched" (none cited)
    project_id: str
    items: list[Moment]              # 1..MOMENTS_MAX
    truncated: bool                  # more distinct Moments existed than MOMENTS_MAX


def eligible(
    sh: "SessionHit", project_id: str, assets: Mapping[str, "ResolvedAsset"]
) -> Optional["ResolvedAsset"]:
    """The resolved asset when ``sh`` may become a Moment, else ``None`` (Req 3.1, 3.2).

    Shared by both selection paths. A Hit is eligible when it was returned under ``project_id``, it
    is a well-formed Hit (:func:`verify.is_hit`), its ``asset_id`` resolves in ``assets``, and that
    Project_Asset's status is not ``removed`` (the only excluded status, per the user decision and
    open question 2).
    """
    if sh.project_id != project_id:
        return None
    if not is_hit(sh.hit):
        return None
    asset = assets.get(sh.hit["asset_id"])
    if asset is None or asset.status == _REMOVED_STATUS:
        return None
    return asset


def to_moment(sh: "SessionHit", a: "ResolvedAsset", citation: Optional[str] = None) -> Moment:
    """Build the :class:`Moment` for an eligible Hit and its resolved asset.

    Typed fields only: the asset's ``kind`` and Display_Name come from the registry (never the
    Hit's ``title``); ``text`` is the derived matched text; ``highlight_terms`` are the ``<em>``
    spans; ``ref`` carries a ``frame_s3_key`` only for a shot Hit. ``citation`` is set on the cited
    path only.
    """
    hit = sh.hit
    text, text_truncated = matched_text(hit)
    return Moment(
        key=(hit["asset_id"], hit["start_ms"], hit["end_ms"]),
        ref=AssetReference(
            project_id=sh.project_id,
            asset_id=hit["asset_id"],
            frame_s3_key=_frame_key(hit),
        ),
        kind=a.kind,
        display_name=a.display_name,
        start_ms=hit["start_ms"],
        end_ms=hit["end_ms"],
        source_index=_source_index(hit),
        final_score=float(hit["final_score"]),
        text=text,
        text_truncated=text_truncated,
        highlight_terms=highlight_terms(hit.get("highlights")),
        citation=citation,
        found_by=found_by(hit),
    )


def cited_moments(outcome: "TurnOutcome") -> tuple[list[Moment], bool]:
    """Req 3.4. For each verified :class:`CitationRecord` of ``outcome.citations``, in order, find
    the :class:`SessionHit` of ``outcome.session_hits`` whose ``recency`` equals the record's
    ``hit_recency`` and whose ``asset_id`` equals ``record.ref.asset_id`` — the Hit
    ``classify_citations`` already chose (the most recent Hit verifying the Citation).

    Skip a record whose Hit cannot be found or is not :func:`eligible`, and skip one whose card key
    is already taken (two Citations of one Hit, or of Hits with the same key, give one card). The
    card's position is its first Verified_Citation. ``truncated`` is ``True`` when more distinct
    eligible Moments existed than :data:`MOMENTS_MAX`.
    """
    assets = outcome.assets
    moments: list[Moment] = []
    seen_keys: set[tuple[str, int, int]] = set()
    truncated = False
    if assets is None:
        return moments, truncated

    for record in outcome.citations:
        if not getattr(record, "verified", False):
            continue
        ref = getattr(record, "ref", None)
        recency = getattr(record, "hit_recency", None)
        if ref is None or recency is None:
            continue
        sh = _hit_for_record(outcome.session_hits, recency, ref.asset_id)
        if sh is None:
            continue
        asset = eligible(sh, outcome.project_id, assets)
        if asset is None:
            continue
        key = (sh.hit["asset_id"], sh.hit["start_ms"], sh.hit["end_ms"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        if len(moments) >= MOMENTS_MAX:
            truncated = True
            continue
        moments.append(to_moment(sh, asset, citation=getattr(record, "text", None)))

    return moments, truncated


def tab_hits(outcome: "TurnOutcome") -> list:
    """The tab's candidate Hits: ``outcome.tab_hits`` (delivered, then overflow), else ``turn_hits``."""
    hits = getattr(outcome, "tab_hits", None)
    return list(hits) if hits is not None else list(outcome.turn_hits)


def searched_moments(
    outcome: "TurnOutcome", *, taken: frozenset = frozenset(), limit: int = MOMENTS_MAX
) -> tuple[list[Moment], bool]:
    """Req 3.5. Walk :func:`tab_hits` in order — this turn's delivered Hits in call and result order,
    then each call's overflow — keep the first eligible Moment per card key not already in ``taken``
    (the cited cards), and stop adding at ``limit``. ``truncated`` is ``True`` when more distinct
    eligible Moments existed than the limit.
    """
    assets = outcome.assets
    moments: list[Moment] = []
    seen_keys: set[tuple[str, int, int]] = set(taken)
    truncated = False
    if assets is None:
        return moments, truncated

    for sh in tab_hits(outcome):
        asset = eligible(sh, outcome.project_id, assets)
        if asset is None:
            continue
        key = (sh.hit["asset_id"], sh.hit["start_ms"], sh.hit["end_ms"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        if len(moments) >= limit:
            truncated = True
            continue
        moments.append(to_moment(sh, asset))

    return moments, truncated


def select_moments(outcome: "TurnOutcome") -> Optional[MomentSelection]:
    """Choose the turn's Moments, or ``None`` when there are none to show (Req 3.4, 3.5, 3.6).

    As amended 2026-10-10: the tab shows every eligible Hit the turn's searches returned, not only the
    cited ones. :func:`cited_moments` come first (in first-citation order, each carrying its citation),
    then :func:`searched_moments` fills the rest from :func:`tab_hits`, skipping keys already cited, up
    to :data:`MOMENTS_MAX` in total. ``basis`` is ``"cited"`` when at least one cited card exists. ``None``
    when ``outcome.assets is None`` (the registry was unreadable) or when no card qualifies (Req 3.6).
    """
    if outcome.assets is None:
        return None

    cited: list[Moment] = []
    cited_truncated = False
    if any(getattr(r, "verified", False) for r in outcome.citations):
        cited, cited_truncated = cited_moments(outcome)
    rest, rest_truncated = searched_moments(
        outcome, taken=frozenset(m.key for m in cited), limit=MOMENTS_MAX - len(cited)
    )
    items = cited + rest
    if not items:
        return None
    return MomentSelection(
        basis="cited" if cited else "searched",
        project_id=outcome.project_id,
        items=items,
        truncated=cited_truncated or rest_truncated,
    )


def subtitle_for(items: list[Moment]) -> str:
    """"N cited · M more from this turn's searches", or the single-basis line when one side is empty.

    Derived from the cards themselves (a cited card carries its citation), so it stays right after the
    Moments_Rule drops already-shown images from the selection.
    """
    cited = sum(1 for m in items if m.citation is not None)
    rest = len(items) - cited
    if cited and rest:
        return f"{cited} cited \u00b7 {rest} more from this turn's searches"
    return _SUBTITLE_CITED if cited else _SUBTITLE_SEARCHED


def found_by(hit: dict) -> tuple:
    """Which of hybrid search's two searches matched a Hit, from its ``match_sources`` (``ann:<field>`` is the
    vector search, by MEANING; ``fts:<field>`` the full-text one, by KEYWORD). Lab 2 teaches that the two
    combine; the card shows which found each moment. Unknown sources are ignored."""
    srcs = [str(s).lower() for s in (hit.get("match_sources") or []) if isinstance(s, str)]
    out = []
    if any(s.startswith(("ann", "knn", "vector")) for s in srcs):
        out.append("meaning")
    if any(s.startswith(("fts", "bm25", "keyword", "text")) for s in srcs):
        out.append("keyword")
    return tuple(out)


def matched_text(hit: dict) -> tuple[str, bool]:
    """Req 3.9. The matched text of a Hit and whether it was truncated.

    ``""`` for a shot Hit; else ``hit["text"]`` when it is a non-empty str; else
    ``compact.serialize_entities(hit["named_entities"])`` when ``named_entities`` is present; else
    ``""``. The result is the first :data:`MOMENT_TEXT_MAX` code points plus ``"\u2026"`` when it is
    longer, with the truncated flag.
    """
    if hit.get("source_index") == _SHOT_SOURCE:
        raw = ""
    else:
        text = hit.get("text")
        if isinstance(text, str) and text:
            raw = text
        elif hit.get("named_entities") is not None:
            raw = serialize_entities(hit["named_entities"])
        else:
            raw = ""
    if len(raw) > MOMENT_TEXT_MAX:
        return raw[:MOMENT_TEXT_MAX] + _ELLIPSIS, True
    return raw, False


def highlight_terms(highlights: Any) -> list[str]:
    """Req 3.10. The inner text of every ``<em>…</em>`` in the str leaves of ``highlights``.

    ``highlights`` may be a str, or a list / dict nesting up to depth 3. For each ``<em>`` span the
    inner tags are removed, HTML entities are unescaped, and the :data:`TRIM_CHARS` set is trimmed.
    Terms are distinct by ``casefold()`` (first occurrence kept), at most
    :data:`HIGHLIGHT_TERMS_MAX`, each at most :data:`HIGHLIGHT_TERM_MAX_CHARS` code points (longer
    terms dropped). ``[]`` when ``highlights`` is absent or yields nothing.
    """
    terms: list[str] = []
    seen: set[str] = set()
    for leaf in _str_leaves(highlights):
        for match in _EM_RE.finditer(leaf):
            inner = _TAG_RE.sub("", match.group(1))
            inner = html.unescape(inner)
            inner = _trim(inner)
            if not inner or len(inner) > HIGHLIGHT_TERM_MAX_CHARS:
                continue
            fold = inner.casefold()
            if fold in seen:
                continue
            seen.add(fold)
            terms.append(inner)
            if len(terms) >= HIGHLIGHT_TERMS_MAX:
                return terms
    return terms


def build_moments_manifest(
    sel: MomentSelection, *, session_id: str, turn_id: str, created_at: str, query: Optional[str] = None,
    query_ref: Optional[dict] = None,
) -> dict:
    """The ``clip-gallery`` manifest for a :class:`MomentSelection` (Req 3.26).

    The id is deterministic (``"moments-" + rid[:24]`` where
    ``rid = sha256("moments|" + session_id + "|" + turn_id)``), so a repeated flush overwrites one
    object and the SPA's ``render_request_id`` dedup keeps one tab. Every field is typed; no URL is
    written. ``galleryMoments`` is a new top-level key beside ``galleryClips``, so old SPA builds
    parse the manifest unchanged. ``query`` (the turn's first search term) pre-fills the tab's search box;
    absent when the turn recorded none.
    """
    rid = hashlib.sha256(
        ("moments|" + session_id + "|" + turn_id).encode("utf-8")
    ).hexdigest()[:32]
    manifest_id = "moments-" + rid[:24]

    items: list[dict] = []
    for position, moment in enumerate(sel.items, start=1):
        items.append(
            {
                "id": f"m{position}",
                "ref": {
                    "project_id": moment.ref.project_id,
                    "asset_id": moment.ref.asset_id,
                    "frame_s3_key": moment.ref.frame_s3_key,
                },
                "kind": moment.kind,
                "display_name": moment.display_name,
                "start_ms": moment.start_ms,
                "end_ms": moment.end_ms,
                "source_index": moment.source_index,
                "final_score": moment.final_score,
                "text": moment.text,
                "text_truncated": moment.text_truncated,
                "highlight_terms": list(moment.highlight_terms),
                "citation": moment.citation,
                **({"found_by": list(moment.found_by)} if moment.found_by else {}),
            }
        )

    block: dict = {
        "v": 1,
        "project_id": sel.project_id,
        "turn_id": turn_id,
        "basis": sel.basis,
        "cited_count": sum(1 for m in sel.items if m.citation is not None),
        "truncated": sel.truncated,
        "items": items,
    }
    if isinstance(query, str) and query.strip():
        block["query"] = query.strip()[:QUERY_MAX]
    ref = _query_ref_wire(query_ref)
    if ref is not None:
        # The typed query the tab's search bar starts from: an image search shows its image chip(s).
        # `query` stays for SPA builds that predate it (they read a text query only).
        block["queryRef"] = ref
    return {
        "id": manifest_id,
        "createdAt": created_at,
        "template": "clip-gallery",
        "title": "Moments",
        "subtitle": subtitle_for(sel.items),
        "galleryMoments": block,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
_QUERY_IMAGE_FIELDS = ("assetId", "frameKey", "sourceAssetId", "label")


def _query_ref_wire(ref: Optional[dict]) -> Optional[dict]:
    """``query_ref`` reduced to typed fields only (never a URL): text ``{type, q}`` or image ``{type, images,
    match}`` with at most 3 images, each with an ``assetId`` or a ``frameKey``. Anything else → ``None``."""
    if not isinstance(ref, dict):
        return None
    if ref.get("type") == "text":
        q = ref.get("q")
        return {"type": "text", "q": q.strip()[:QUERY_MAX]} if isinstance(q, str) and q.strip() else None
    if ref.get("type") != "image" or not isinstance(ref.get("images"), list):
        return None
    images = []
    for image in ref["images"][:3]:
        if not isinstance(image, dict) or not (image.get("assetId") or image.get("frameKey")):
            continue
        out = {k: image[k] for k in _QUERY_IMAGE_FIELDS if isinstance(image.get(k), str) and image[k]}
        if isinstance(image.get("atMs"), int) and not isinstance(image.get("atMs"), bool):
            out["atMs"] = image["atMs"]
        images.append(out)
    return {"type": "image", "images": images, "match": "any"} if images else None



def _hit_for_record(
    session_hits: Any, recency: tuple[int, int, int], asset_id: str
) -> Optional["SessionHit"]:
    """The one :class:`SessionHit` whose ``recency`` and ``asset_id`` match the chosen Hit."""
    for sh in session_hits:
        if (
            sh.recency == recency
            and isinstance(sh.hit, dict)
            and sh.hit.get("asset_id") == asset_id
        ):
            return sh
    return None


def _source_index(hit: dict) -> Optional[str]:
    """``hit["source_index"]`` when it is a str, else ``None``."""
    value = hit.get("source_index")
    return value if isinstance(value, str) else None


def _frame_key(hit: dict) -> Optional[str]:
    """``hit["frame_s3_key"]`` only for a shot Hit carrying a non-empty str, else ``None``.

    Matches :func:`verify._frame_key` so a Moment's ``ref`` carries the same bucket key a
    :class:`verify.CitationRecord` would.
    """
    if hit.get("source_index") != _SHOT_SOURCE:
        return None
    key = hit.get("frame_s3_key")
    if isinstance(key, str) and key:
        return key
    return None


def _str_leaves(node: Any, depth: int = 1) -> list[str]:
    """Every str leaf of ``node``, walking lists and dicts no deeper than :data:`_HIGHLIGHT_MAX_DEPTH`."""
    if isinstance(node, str):
        return [node]
    if depth > _HIGHLIGHT_MAX_DEPTH:
        return []
    out: list[str] = []
    if isinstance(node, list):
        for item in node:
            out.extend(_str_leaves(item, depth + 1))
    elif isinstance(node, dict):
        for value in node.values():
            out.extend(_str_leaves(value, depth + 1))
    return out


def _trim(text: str) -> str:
    """Strip the :data:`TRIM_CHARS` set from both ends (shared with the citation parser)."""
    start = 0
    end = len(text)
    while start < end and text[start] in _TRIM_SET:
        start += 1
    while end > start and text[end - 1] in _TRIM_SET:
        end -= 1
    return text[start:end]
