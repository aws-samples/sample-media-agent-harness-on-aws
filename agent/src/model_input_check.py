"""Check a model submit against THAT MODEL's own schema before anything is sent.

The fal MCP server's tools are generic: ``submit_job(endpoint_id, input: object)`` / ``run_model(...)`` /
``submit_jobs(requests=[{endpoint_id, input}])``. The MCP tool schema says nothing about what ``input`` a given
model accepts; that lives one level down, behind the server's own ``get_model_schema(endpoint_id)`` (an
OpenAPI document whose POST request body is the model's input JSON Schema).

This module replaces the hard-coded, endpoint-family argument rewriting that used to live in ``tool_args``
(``normalize_minimax_h3_args`` ADDED ``prompt_expansion_mode`` to every ``minimax/h3-max/*`` call — right for
image-to-video, which requires it, and a guaranteed 422 for ``recast``, whose schema forbids it). Nothing is
rewritten here. A submit whose ``input`` breaks the model's schema is REFUSED before the call, with a message
naming the problem and the fields the model actually accepts, so the agent fixes it in the same turn and
nothing is billed. Three checks only, all read from the schema:

* an unknown field when the schema says ``additionalProperties: false``;
* a missing ``required`` field;
* a value outside a field's ``enum``.

Types, ranges and formats are NOT checked: fal validates those itself, and a false refusal would be worse
than the 422 it saves. Everything fails OPEN: no schema tool, an unreadable or unexpected schema, or any error
fetching it means the call goes through unchecked, exactly as before.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Optional

#: Tool verbs that send a model ``input`` to generate (the batch tool carries one per request).
SUBMIT_VERBS = frozenset({"submit_job", "run_model", "submit_jobs"})
#: The sibling tool that returns a model's schema.
SCHEMA_VERB = "get_model_schema"
#: Schemas change rarely; a new model version is picked up within the hour.
SCHEMA_TTL_S = 3600.0
#: A failed fetch is not retried on every call.
FAILED_TTL_S = 60.0
_ENDPOINT_KEYS = ("endpoint_id", "model_id", "application")

_cache: dict[str, tuple[float, Optional[dict]]] = {}
_lock = threading.Lock()


def enabled() -> bool:
    """Kill switch: ``MODEL_INPUT_CHECK=off`` sends every submit unchecked."""
    return os.environ.get("MODEL_INPUT_CHECK", "on").strip().lower() not in ("off", "0", "false", "no")


def verb_of(tool_name: str) -> str:
    """``usr-abc___submit_job`` -> ``submit_job`` (gateway targets namespace tool names with ``___``)."""
    return (tool_name or "").rsplit("___", 1)[-1]


def schema_tool_name(server_tool_name: str) -> str:
    """The ``get_model_schema`` tool on the SAME server/target as ``server_tool_name``."""
    if "___" in (server_tool_name or ""):
        return server_tool_name.rsplit("___", 1)[0] + "___" + SCHEMA_VERB
    return SCHEMA_VERB


def _endpoint(d: Any) -> str:
    if isinstance(d, dict):
        for k in _ENDPOINT_KEYS:
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip().strip("/")
    return ""


def submissions(verb: str, arguments: Any) -> list[tuple[str, str, dict]]:
    """``[(where, endpoint, input)]`` for each model input a submit call sends (``where`` labels it in messages)."""
    if not isinstance(arguments, dict):
        return []
    if verb in ("submit_job", "run_model"):
        ep, inp = _endpoint(arguments), arguments.get("input")
        return [("input", ep, inp)] if ep and isinstance(inp, dict) else []
    if verb == "submit_jobs":
        out = []
        for i, req in enumerate(arguments.get("requests") or []):
            ep, inp = _endpoint(req), req.get("input") if isinstance(req, dict) else None
            if ep and isinstance(inp, dict):
                out.append((f"requests[{req.get('index', i)}].input", ep, inp))
        return out
    return []


# ------------------------------------------------------------------------------------------- schema parsing
def _resolve(node: Any, root: dict, depth: int = 0) -> Any:
    """Follow a local ``$ref`` (``#/components/schemas/X`` or ``#/$defs/X``) within ``root``."""
    while isinstance(node, dict) and isinstance(node.get("$ref"), str) and depth < 8:
        ref = node["$ref"]
        if not ref.startswith("#/"):
            return None
        cur: Any = root
        for part in ref[2:].split("/"):
            cur = cur.get(part) if isinstance(cur, dict) else None
        node, depth = cur, depth + 1
    return node


def _as_dict(payload: Any) -> Optional[dict]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            v = json.loads(payload)
        except ValueError:
            return None
        return v if isinstance(v, dict) else None
    return None


def input_schema(doc: Any, endpoint: str) -> Optional[dict]:
    """The model's INPUT JSON Schema from a ``get_model_schema`` payload, or ``None`` when it cannot be found.

    Accepts the OpenAPI document fal publishes (the POST request body of ``/<endpoint>``), a wrapper carrying it
    (``{"schema": …}`` / ``{"openapi": …}``), or an ``input_schema`` / ``inputSchema`` / ``input`` JSON Schema.
    """
    d = _as_dict(doc)
    if d is None:
        return None
    for wrap in ("schema", "openapi_schema", "openapiSchema", "data", "result"):
        inner = _as_dict(d.get(wrap))
        if inner and ("paths" in inner or "properties" in inner or "input_schema" in inner or "inputSchema" in inner):
            d = inner
            break
    paths = d.get("paths")
    if isinstance(paths, dict):
        op = (paths.get("/" + endpoint.strip("/")) or {}).get("post") if isinstance(
            paths.get("/" + endpoint.strip("/")), dict) else None
        if op is None:  # one POST in the document: that is the model's submit
            posts = [p["post"] for p in paths.values() if isinstance(p, dict) and isinstance(p.get("post"), dict)]
            op = posts[0] if len(posts) == 1 else None
        body = (((op or {}).get("requestBody") or {}).get("content") or {}).get("application/json") or {}
        schema = _resolve(body.get("schema"), d)
        return _self_contained(schema, d)
    for key in ("input_schema", "inputSchema", "input"):
        schema = _resolve(_as_dict(d.get(key)), d)
        if isinstance(schema, dict) and isinstance(schema.get("properties"), dict):
            return _self_contained(schema, d)
    return d if isinstance(d.get("properties"), dict) else None


def _self_contained(schema: Any, root: dict) -> Optional[dict]:
    """``schema`` carrying its document's ``components`` / ``$defs``, so a nested ``$ref`` still resolves once
    the schema is cached on its own."""
    if not (isinstance(schema, dict) and isinstance(schema.get("properties"), dict)):
        return None
    extra = {k: root[k] for k in ("components", "$defs") if isinstance(root.get(k), dict) and k not in schema}
    return {**schema, **extra} if extra else schema


# ----------------------------------------------------------------------------------------------- checking
def _allowed_values(prop: Any, root: dict) -> Optional[list]:
    """A field's ``enum`` (merged across ``anyOf``/``oneOf`` branches, ``null`` allowed when a branch is null);
    ``None`` when the field is not an enum."""
    prop = _resolve(prop, root)
    if not isinstance(prop, dict):
        return None
    if isinstance(prop.get("enum"), list):
        return list(prop["enum"])
    branches = prop.get("anyOf") or prop.get("oneOf")
    if not isinstance(branches, list):
        return None
    values: list = []
    for b in branches:
        b = _resolve(b, root)
        if not isinstance(b, dict):
            return None
        if isinstance(b.get("enum"), list):
            values += b["enum"]
        elif b.get("type") == "null":
            values.append(None)
        else:
            return None  # a free-form branch: not an enum
    return values or None


def problems(schema: dict, inp: dict, root: Optional[dict] = None) -> list[str]:
    """What in ``inp`` breaks ``schema`` (unknown field / missing required / value outside enum). Empty = OK."""
    root = root or schema
    props: dict = schema.get("properties") or {}
    out: list[str] = []
    if schema.get("additionalProperties") is False:
        for k in inp:
            if k not in props:
                out.append(f"`{k}` is not a field of this model")
    for k in schema.get("required") or []:
        if k not in inp:
            out.append(f"`{k}` is required")
    for k, v in inp.items():
        allowed = _allowed_values(props.get(k), root) if k in props else None
        if allowed is not None and v not in allowed:
            out.append(f"`{k}` must be one of {', '.join(json.dumps(a) for a in allowed)} (got {json.dumps(v)})")
    return out


def accepted_fields(schema: dict, root: Optional[dict] = None) -> str:
    """``video_url (required), resolution (768P|1080P), seed, …`` — what to tell the agent it may send."""
    root = root or schema
    required = set(schema.get("required") or [])
    props = schema.get("properties") or {}
    order = [n for n in (schema.get("x-fal-order-properties") or []) if n in props]
    names = order + [n for n in props if n not in order]
    parts = []
    for name in names:
        prop = props[name]
        allowed = _allowed_values(prop, root)
        p = _resolve(prop, root) if isinstance(prop, dict) else {}
        examples = p.get("examples") if isinstance(p, dict) else None
        detail = []
        if name in required:
            detail.append("required")
        if allowed is not None:
            detail.append("|".join("null" if a is None else str(a) for a in allowed))
        elif (isinstance(examples, list) and examples
              and all(isinstance(e, str) and len(e) <= 24 and "://" not in e for e in examples)):
            # Short value examples ("disabled", "balanced") help; sample URLs and long prompts do not.
            detail.append("e.g. " + "|".join(examples))
        parts.append(f"{name} ({'; '.join(detail)})" if detail else name)
    return ", ".join(parts)


# ---------------------------------------------------------------------------------------------- the cache
def cached_schema(endpoint: str, now: Optional[float] = None) -> tuple[bool, Optional[dict]]:
    """``(hit, schema)``: ``hit`` False means fetch it. A cached ``None`` is a recent failure (fail open)."""
    now = time.time() if now is None else now
    with _lock:
        entry = _cache.get(endpoint)
    if entry is None or entry[0] <= now:
        return False, None
    return True, entry[1]


def remember(endpoint: str, schema: Optional[dict], now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    with _lock:
        _cache[endpoint] = (now + (SCHEMA_TTL_S if schema is not None else FAILED_TTL_S), schema)


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def payload_of(result: Any) -> Any:
    """The JSON payload of an MCP tool result (``structuredContent``, a ``json`` block, or a JSON ``text`` block)."""
    if not isinstance(result, dict):
        return None
    if str(result.get("status", "success")).lower() == "error" or result.get("isError") is True:
        return None
    for key in ("structuredContent", "structured_content"):
        if isinstance(result.get(key), dict):
            return result[key]
    for block in result.get("content") or []:
        if isinstance(block, dict):
            if isinstance(block.get("json"), dict):
                return block["json"]
            if isinstance(block.get("text"), str):
                d = _as_dict(block["text"])
                if d is not None:
                    return d
    return None


def refusal_text(tool_name: str, failures: list[tuple[str, str, dict, list[str]]]) -> str:
    """The message the agent reads: what broke, per submission, and what each model accepts."""
    lines = ["Not submitted — nothing was sent or billed. The input does not match the model's own schema "
             "(from get_model_schema):"]
    for where, endpoint, schema, probs in failures:
        lines.append(f"- {endpoint} ({where}): " + "; ".join(probs) + ".")
        lines.append(f"  Accepted fields: {accepted_fields(schema)}.")
    lines.append(f"Call {verb_of(tool_name)} again with only the accepted fields, every required one, and "
                 "allowed values. Do not add a field the schema does not list.")
    return "\n".join(lines)
