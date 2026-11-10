"""Main_Agent per-turn search wiring (tasks 11.1 and 11.11; design Component 7 "Binding rules").

`prepare_turn(agentcore_context, payload)` reads the proxy-authored `__turn_context` and the
ProjectToken header, sets the contextvars, and returns the tools to bind for this turn plus the
turn-context prompt block:

- a turn context with project access: `ProjectSearchTool` (normal, or `unavailable` mode);
- no turn context, or an agent-scoped invoke: nothing.

Search is arm-A only: the Main_Agent always runs the Search_Tool itself. Any failure binds no search
tool and never breaks the turn. The prompt block never includes the token.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from . import call_checks, compact, ledger, skill_text
from .binding import AGENT_SCOPE_MARKERS, SearchDescriptor, resolve_search_binding
from .client import SearchGatewayClient
from .context import TurnContext, current_project_token, current_turn_context, header_value
from .refresh import default_refresher
from .token_ref import ProjectTokenRef
from .tool import ProjectSearchTool, fetch_spec

logger = logging.getLogger(__name__)

_FALLBACK_SCHEMA = {"type": "object", "properties": {
    "collection_id": {"type": "string"}, "search_term": {"type": "string"},
    "search_image_s3uri": {"type": "string"}, "k": {"type": "integer"}}}

# Prompt lines shared across bindings (Req 7.5, 7.13, 7.14).
_CUSTOM_AGENT_LINE = ("- Custom agents cannot search Projects in this version. If the user asks for a search to run "
                      "in a custom agent, say so and run the search in this turn instead.")
_ACCESS_ENDED_LINE = ("- If a search returns category `project_access_ended`, tell the user they no longer have access "
                      "to the active project and make no further search in this turn.")
_UNAVAILABLE_LINE = ("- If a search returns category `project_access_unavailable`, do not search again this turn; "
                     "tell the user project access could not be established and to send the message again.")
_NO_EXPIRY_LINE = ("- Never explain a search error as an expired token, and never mention tokens or credentials "
                   "when explaining one.")


def _search_image_lines(tc: TurnContext) -> list[str]:
    """The Search_Image_Buckets and the Find_Similar line (MSQ-I3, Req 1.24).

    Bucket names come from the hydrated runtime config, matching `_build_scope`.
    """
    ingest = os.environ.get("SKILLS_S3_BUCKET", "")
    proxy = os.environ.get("MEDIA_PROXY_BUCKET", "")
    lines = [f'- Hit frames are in two buckets: the Ingest bucket "{ingest}" and the M2C proxy bucket "{proxy}".',
             "- To search by an attached or project image, pass its asset_id as search_image_asset_id, never its "
             "S3 location. Only images can be a query: searching by video or audio is not supported at the moment."]
    if tc.find_similar is not None:
        lines.append(f'- Find similar: search with search_image_s3uri="{tc.find_similar.image_uri}" and no search_term.')
    return lines


_INDEXING_OFF_LINE = (
    "- Indexing is OFF for this project: its files are stored but not analyzed, so you cannot search their "
    "content. Do not run media searches. Find files by name with `project_status` (pass `name`) and use them "
    "directly. If the user asks about what is in the media, say the project isn't searchable yet and that the "
    "project owner can turn on indexing in the project's settings (it analyzes every file and cannot be turned off)."
)

# How a project file reaches the Renderer: a link or a remembered URL in the reply opens nothing. The lookup comes
# FIRST: with only "use show_asset" the agent decided from memory that "sam's picture" was not a project file and
# showed an old upload, though `sam.webp` was in the project (staging, 2026-10-07).
_SHOW_ASSET_LINE = ("- When the user explicitly asks to open, play or show a FILE (e.g. \"show me sam's picture\", "
                    "\"play nba.mp4\"), FIRST call `project_status` with a name keyword from the request, even if "
                    "memory mentions such a file, then call `show_asset` with the match's assetId. Never say a file "
                    "is not in the project without that lookup. A link in your reply does not open it. Do NOT call "
                    "`show_asset` for a question about what is in the media (\"find the moment where…\", \"what is "
                    "X about\"): the search's Moments tab already shows and plays those.")
_FIND_AND_SHOW_LINE = ("- Find files by name with `project_status` (pass `name`); open one with `show_asset` only when "
                       "the user asks to open, play or show it. A link in your reply does not open it.")


# Session 34ca292e: asked to restyle a project image, the agent found it and opened it, then guessed S3 keys in the
# sandbox (AccessDenied) and told the user to paste a link. `use_asset` is how a project file reaches another tool.
_USE_ASSET_LINE = ("- To work ON a project file with another tool (edit, restyle, upscale, transcode, analyze in code), "
                   "find it with `project_status`, call `use_asset` with its assetId, and pass the returned "
                   "`source_url` (or `source_uri` for M2C tools) as the file. Never ask the user for a link and never "
                   "guess storage keys.")


_BROWSE_LINE = ("- When the user asks to browse, list or look through the project's files, call `browse_project` "
                "(optionally with `query`, `folder` or `kind`) to open them as a grid in the Renderer instead of "
                "listing them in your reply.")


_ORGANIZE_LINE = ("- When the user asks to organize the project (create folders or a folder tree, put files in a folder, "
                  "group shots by board, save the moments a search found), call `organize_project`: `folders` creates "
                  "empty folders (a whole tree in one call), `asset_ids` moves whole files, `moments` saves ranges as "
                  "bookmarks, `rename_from` renames a folder. Nothing is copied or re-indexed.\n"
                  "- Projects: `list_projects` lists the user's projects. To go to another project, call `switch_project` "
                  "(a NEW project: `create_project`) with the user's request as `prompt`; a new chat opens there when your "
                  "reply ends and carries on with it, so say so in one sentence and stop. To delete one, call "
                  "`delete_project` and put the card it returns in your reply: only the user's confirmation there deletes.")


# Search cost (2026-10-16): every search is a ~6-8 s M2C workflow run. What costs time is searching, READING and
# searching again; parallel calls in one step cost about one call. Two parallel phrasings are also what found the
# SOURCE video for the golden question ("find moment when Meridian title page show up"); one call alone returned
# only derived clips (golden test, 2026-10-17), so the rule is "one step", not "one call".
_ONE_SEARCH_LINE = ("- Search in ONE step: issue every search the question needs together, in parallel (e.g. the "
                    "question as asked plus one rephrasing or a #scene variant), rather than searching, reading the "
                    "results and searching again. Search again only when those results do not answer it, or when a "
                    "rule above asks for a follow-up (MSQ-C7a).\n"
                    "- When the same moment appears in an original file and in clips derived from it (trims, crops, "
                    "side-by-sides, restyles), cite the ORIGINAL first, then mention the derived copies.")
# Answers that spelled out long CloudFront/presigned URLs cost 3-5 s of output per URL line (one answer: 2,942
# output tokens, 26.5 s). The citation form is what the chat links and the Moments tab plays.
_NO_URL_LINE = ("- In the reply, refer to a project file by its citation (`[name @ mm:ss–mm:ss]`, `[name p.N]`) or "
                "its name, never by a URL: do not copy CloudFront, presigned or s3:// links into the answer (the "
                "citations and the Moments tab already open and play them). Give a file URL only when the user asks for "
                "a link, and then once per file. (The `delete_project` card link is not a file URL: include it as told.)")


# A web-research answer re-cited a moment found turns earlier ("that also explains the moments you found earlier
# … [meridian.mp4 @ 03:58–04:01]"), so a web question surfaced a Moments link (staging a4a7e245, 2026-10-21).
_ON_TOPIC_CITE_LINE = ("- Cite a project moment only when it answers THIS turn's question. For a question about the web or "
                       "a file's outside source, cite web sources only -- do not add earlier moments as colour -- unless "
                       "the user asks to connect the answer to the project's media.")


def _search_tool_name() -> str:
    return os.environ.get("SEARCH_TOOL_NAME", "") or "search_content"


def _prompt_block(tc: TurnContext, binding: Optional[str], project_tools: bool = True) -> str:
    """The turn-context block for the system prompt. `binding` is what was actually bound (None = nothing);
    `project_tools` says whether the Project_Tools (`project_status`, `show_asset`, ...) are bound."""
    ap = tc.active_project
    name = ap.name or "Personal"
    lines = ["", "## Active project for this turn"]
    if binding in ("search_tool", "search_tool_unavailable"):
        lines.append(f'- Active project: "{name}" — search with collection_id="{ap.collection_id}"')
    else:
        lines.append(f'- Active project: "{name}" (collection_id "{ap.collection_id}")')
    if ap.description:
        # Quoted, one line, control characters stripped (context._clean_description): it is user-authored.
        lines.append(f'- Project description (written by the project\'s members): "{ap.description}"')
    if not ap.indexing:
        lines.append(_INDEXING_OFF_LINE)
    lines.append(f"- Web search: {'on' if tc.web_search else 'off'}")
    if binding in ("search_tool", "search_tool_unavailable"):
        lines += [f"- Search this project's indexed media with the `{_search_tool_name()}` tool, always passing "
                  f'collection_id="{ap.collection_id}". Answer only from returned hits and cite them by asset and '
                  "timestamp range.",
                  _ONE_SEARCH_LINE, _NO_URL_LINE, _ON_TOPIC_CITE_LINE,
                  "- If a search returns `policy_denied`, explain that only the active project can be searched."]
        if project_tools:
            lines += [_SHOW_ASSET_LINE, _USE_ASSET_LINE, _BROWSE_LINE, _ORGANIZE_LINE]
    else:
        lines += [f"- Media search is unavailable this turn. If the user asks to search, reply that media search is "
                  f"unavailable (the `{_search_tool_name()}` tool could not be reached), make no search, and say "
                  "nothing about the project's content."]
        if project_tools:
            lines += [_FIND_AND_SHOW_LINE, _USE_ASSET_LINE, _BROWSE_LINE, _ORGANIZE_LINE]
    if binding is not None:
        lines += _search_image_lines(tc)
        lines += [_ACCESS_ENDED_LINE, _UNAVAILABLE_LINE]
    lines += [_CUSTOM_AGENT_LINE, _NO_EXPIRY_LINE]
    block = "\n".join(lines) + "\n"
    # The Main_Agent searches, so it receives the skill text. The static section precedes the
    # per-turn lines above so the prompt prefix stays cacheable across turns (design Component 1 "Arm A").
    if binding in ("search_tool", "search_tool_unavailable"):
        section = skill_text.skill_prompt_section(skill_text.load_media_search_qa())
        if section:
            block = section + "\n" + block
    return block


def _arm_a_tool(binding: str, ref: Optional[ProjectTokenRef], tc: TurnContext, client: SearchGatewayClient,
                spec: Optional[dict], scope: call_checks.CallScope) -> ProjectSearchTool:
    if binding == "search_tool":
        from .assets import AssetServiceResolver

        return ProjectSearchTool(ref, spec or {}, project_id=tc.active_project.id, turn_id=tc.turn_id,
                                 client=client, scope=scope, indexing_off=not tc.active_project.indexing,
                                 images=AssetServiceResolver())
    fallback = compact.with_harness_inputs({"name": client.tool_name,
                                         "description": "Search the active project's media.",
                                         "inputSchema": {"json": _FALLBACK_SCHEMA}})
    return ProjectSearchTool(None, fallback, project_id=tc.active_project.id, turn_id=tc.turn_id,
                             client=client, unavailable=True, scope=scope)


def _build_scope(tc: TurnContext) -> call_checks.CallScope:
    """The Component 2 CallScope for an arm-A turn (Req 1.20).

    `session_id` is best-effort: a turn running without a bound session (tests, management calls) gets
    "", which disables source (a). The two Search_Image_Buckets come from the hydrated runtime config.
    `frame_keys` reads this turn's ledger (the Session_Hit_Store adds the cross-turn keys when wired).
    """
    try:
        from jobs.context import current_session_id
        session_id = current_session_id() or ""
    except Exception:  # noqa: BLE001 - no bound session is a normal condition here
        session_id = ""
    pid = tc.active_project.id
    turn_id = tc.turn_id
    return call_checks.CallScope(
        session_id=session_id,
        project_id=pid,
        ingest_bucket=os.environ.get("SKILLS_S3_BUCKET", ""),
        proxy_bucket=os.environ.get("MEDIA_PROXY_BUCKET", ""),
        frame_keys=lambda: ledger.frame_keys_for(turn_id, pid),
    )


def _project_tools(tc: Any) -> list:
    try:
        from .project_tools import make_project_tools

        return make_project_tools(tc.active_project.id, project_name=tc.active_project.name or "Personal")
    except Exception:  # noqa: BLE001 - the tools are optional; search must still bind
        logger.warning("project tools unavailable this turn", exc_info=True)
        return []


_GRAPH_LINE = ("- Knowledge graph: this project has one. Use the `graph_*` tools (start with `graph_schema` / "
               "`graph_find`) for questions about who, where, what and how things connect, and `graph_context` to "
               "enrich search hits. Rows marked sample are illustrative, never facts about the project.")


async def _graph_tools(tc: TurnContext, ref: Optional[ProjectTokenRef], scoped: bool) -> list:
    """The knowledge-graph tools for this turn (the graph extension of search): only with a graph-enabled active
    Project, a valid Project_Token, project access, and never on an agent-scoped invoke. Never raises."""
    if scoped or ref is None or not tc.active_project.graph or tc.project_access != "ok":
        return []
    try:
        from .graph_tools import make_graph_tools

        return await make_graph_tools(ref, tc.active_project.id, caller="main", turn_id=tc.turn_id)
    except Exception:  # noqa: BLE001 — the graph is optional; search and the rest still bind
        logger.warning("graph tools unavailable this turn", exc_info=True)
        return []


async def prepare_turn(agentcore_context: Any, payload: dict) -> tuple[list, str]:
    # Only a payload that CARRIES a turn context may bind graph tools (the contextvars could hold a previous
    # turn's values otherwise). `_prepare_search_turn` pops `__turn_context`, so decide first.
    has_tc = isinstance(payload.get("__turn_context"), dict)
    scoped = any(payload.get(m) for m in AGENT_SCOPE_MARKERS)
    tools, block = await _prepare_search_turn(agentcore_context, payload)
    tc = current_turn_context.get() if has_tc else None
    if tc is None:
        return tools, block
    gtools = await _graph_tools(tc, current_project_token.get(), scoped)
    if not gtools:
        return tools, block
    section = skill_text.graph_prompt_section(skill_text.load_project_knowledge_graph())
    return [*tools, *gtools], (block or "") + _GRAPH_LINE + "\n" + section


async def _prepare_search_turn(agentcore_context: Any, payload: dict) -> tuple[list, str]:
    try:
        tc = TurnContext.parse(payload.get("__turn_context"))
        payload.pop("__turn_context", None)
        if tc is None:
            return [], ""
        current_turn_context.set(tc)
        raw = header_value(getattr(agentcore_context, "request_headers", None))
        ref = None
        if raw:
            try:
                ref = ProjectTokenRef(raw, refresher=default_refresher())
            except ValueError:
                logger.warning("project token header present but malformed; search unavailable this turn")
        del raw
        if ref is not None and ref.project_id != tc.active_project.id:
            logger.warning("project token project does not match the turn's active project; search unavailable")
            ref = None
        current_project_token.set(ref)
        scoped = any(payload.get(m) for m in AGENT_SCOPE_MARKERS)
        # Project_Tools (ingestion Component 17): Main_Agent only, never on an agent-scoped invoke.
        ptools = [] if scoped else _project_tools(tc)
        binding = resolve_search_binding(SearchDescriptor(
            runtime="main", has_turn_context=True, project_access=tc.project_access,
            has_token=ref is not None, agent_scoped=scoped))
        if binding is None:
            return ptools, ("" if scoped else _prompt_block(tc, None, bool(ptools)))
        client = SearchGatewayClient()
        if not client.url or not client.tool_name:
            # Projects search not configured on this runtime (e.g. local dev): bind nothing.
            return ptools, _prompt_block(tc, None, bool(ptools))

        spec = None
        if binding == "search_tool":
            try:
                spec = await fetch_spec(client, ref)  # type: ignore[arg-type]
            except Exception as exc:  # noqa: BLE001 - bind nothing for search, retry next turn (Req 5.12)
                logger.warning("search tool spec fetch failed (%s); no search tool this turn", type(exc).__name__)
                return ptools, _prompt_block(tc, None, bool(ptools))
        return ([_arm_a_tool(binding, ref, tc, client, spec, _build_scope(tc)), *ptools],
                _prompt_block(tc, binding, bool(ptools)))
    except Exception:  # noqa: BLE001
        logger.warning("project search turn wiring failed; continuing without search", exc_info=True)
        return [], ""
