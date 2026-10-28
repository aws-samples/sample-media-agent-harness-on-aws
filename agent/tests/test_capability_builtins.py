"""The built-in capability catalog — what the Agents and Skills panels paint from.

Both panels used to fetch this from the agent runtime, so opening a tab woke a microVM
to be told what is essentially a constant. They now read from here instead, from two
different kinds of source:

* **Agents** come from `BUILTIN_CAPABILITIES` in this module — three literals, plus three
  `os.environ` lookups for whether each runtime is wired.
* **Skills** come from a DynamoDB row the *deploy* writes (`infra/lib/builtin-skills.ts`
  and the `BuiltinSkillCatalog` custom resource), not from files in this image. That is
  what lets a `SKILL.md` edit reach the panel on `cdk deploy` alone.

What these tests pin:

* **Enough to render.** A panel painting a name with no description or no `invokedVia`
  is a regression the runtime path did not have, so both sources must carry those
  fields — not just identity.
* **Honest about deployment.** `configured` says whether a sub-agent runtime is wired,
  and comes from an env var, so a task that never received those variables would report
  three healthy runtimes as unconfigured. `None` (does not apply) and `False` (applies,
  answer is no) are different, and the tests keep them apart.
* **A missing catalog is an error, not an empty list.** An unpublished row must surface
  as a partial error, or a failed deploy-time write reads as "this harness ships no
  built-in skills".
"""

from __future__ import annotations

from typing import Optional

import pytest

from capability.sources import (
    BUILTIN_CAPABILITIES,
    SourceUnavailable,
    read_builtins,
)

#: Env var per sub-agent, mirroring `main.py`'s `arnEnv` mapping.
ARN_ENVS = {
    "task-agent": "TASK_AGENT_RUNTIME_ARN",
    "render-agent": "RENDER_AGENT_RUNTIME_ARN",
    "planning-agent": "PLANNING_AGENT_RUNTIME_ARN",
}


async def _builtins(category: str):
    return await read_builtins(category)


# --- the static half -------------------------------------------------------


@pytest.mark.asyncio
async def test_all_sub_agents_are_present() -> None:
    rows = await _builtins("specialty-agent")
    slugs = {r.asset_id for r in rows}
    assert slugs == set(ARN_ENVS), f"expected the three sub-agents, got {sorted(slugs)}"


@pytest.mark.asyncio
async def test_every_sub_agent_carries_what_the_panel_renders() -> None:
    """The panel shows label, description and 'Invoked via: …'.

    Asserted per row rather than in aggregate: one entry missing a description is
    exactly the kind of gap that renders as a blank line instead of an error.
    """
    for row in await _builtins("specialty-agent"):
        assert row.display_name, f"{row.asset_id} has no display name"
        assert row.description, f"{row.asset_id} has no description to render"
        assert row.invoked_via, f"{row.asset_id} does not say how it is invoked"


@pytest.mark.asyncio
async def test_builtins_keep_their_reserved_identity() -> None:
    """R4's reserved identity, and the reason the parity allowlist exists.

    The legacy `/lab` path returns the Task Agent as a virtual `core-task-agent`
    row; this side returns `builtin:specialty-agent:task-agent`. Both are correct and
    the difference is deliberate — do not "fix" one into the other.
    """
    rows = {r.asset_id: r for r in await _builtins("specialty-agent")}
    task = rows["task-agent"]
    assert task.id == "builtin:specialty-agent:task-agent"
    assert task.source == "builtin"
    assert task.capability_class == "builtin"
    assert task.status == "published"


# --- the deployment half ---------------------------------------------------


@pytest.mark.asyncio
async def test_configured_is_false_when_the_arn_env_is_absent(monkeypatch) -> None:
    """A missing env var must read as NOT configured, not as unknown-and-hidden.

    This is the failure this test exists for: if the Capability API task definition
    never receives these variables, working runtimes render as unconfigured.
    Asserting `is False` (not falsy) keeps that distinct from `None`.
    """
    for env in ARN_ENVS.values():
        monkeypatch.delenv(env, raising=False)

    for row in await _builtins("specialty-agent"):
        assert row.configured is False, f"{row.asset_id} should be unconfigured"
        assert not row.runtime_id


@pytest.mark.asyncio
async def test_configured_and_runtime_id_come_from_the_arn(monkeypatch) -> None:
    """`runtimeId` is the ARN's last path segment, matching main.py's derivation."""
    monkeypatch.setenv(
        "TASK_AGENT_RUNTIME_ARN",
        "arn:aws:bedrock-agentcore:us-west-2:1234:runtime/mediaworks_task-ABC123",
    )
    for env in ("RENDER_AGENT_RUNTIME_ARN", "PLANNING_AGENT_RUNTIME_ARN"):
        monkeypatch.delenv(env, raising=False)

    rows = {r.asset_id: r for r in await _builtins("specialty-agent")}
    assert rows["task-agent"].configured is True
    assert rows["task-agent"].runtime_id == "mediaworks_task-ABC123"
    # The others are untouched by one being set.
    assert rows["render-agent"].configured is False


def test_the_descriptor_names_an_env_var_for_every_sub_agent() -> None:
    """Guards a new sub-agent being added without a way to tell if it is wired."""
    for entry in BUILTIN_CAPABILITIES:
        if entry["category"] != "specialty-agent":
            continue
        assert entry.get("arnEnv"), f"{entry['slug']} declares no arnEnv"


# --- skills ----------------------------------------------------------------


class FakeGatewayTable:
    """Stand-in for the boto3 Table `read_builtins` / `read_builtin_skill` are given.

    Models both access paths, because the split between them is the design: the LIST is a
    projected Query over `__skill__*`, and a VIEW is a GetItem on one of those keys.
    """

    def __init__(self, items: Optional[list[dict]] = None, *, raises: bool = False) -> None:
        self.items = items if items is not None else []
        self.raises = raises
        self.queries: list[dict] = []
        self.gets: list[dict] = []

    def query(self, **kwargs):
        self.queries.append(kwargs)
        if self.raises:
            raise RuntimeError("dynamodb is unhappy")
        prefix = kwargs["ExpressionAttributeValues"][":sk"]
        matched = [i for i in self.items if str(i.get("SK", "")).startswith(prefix)]
        # Honour ProjectionExpression, so a test notices if the list starts pulling bodies.
        projected = kwargs.get("ProjectionExpression")
        if projected:
            names = kwargs.get("ExpressionAttributeNames", {})
            keep = {names.get(f.strip(), f.strip()) for f in projected.split(",")}
            matched = [{k: v for k, v in i.items() if k in keep} for i in matched]
        return {"Items": matched}

    def get_item(self, **kwargs):
        self.gets.append(kwargs)
        if self.raises:
            raise RuntimeError("dynamodb is unhappy")
        sk = kwargs["Key"]["SK"]
        for item in self.items:
            if item.get("SK") == sk:
                return {"Item": item}
        return {}


def _skill_item(name: str, description: str = "", **extra: object) -> dict:
    return {
        "PK": "GATEWAY#CATALOG",
        "SK": f"__skill__{name}",
        "name": name,
        **({"description": description} if description else {}),
        "skillMd": f"---\nname: {name}\n---\n# {name}\n",
        "supportingFiles": [],
        **extra,
    }


@pytest.fixture(autouse=True)
def _clear_skill_catalog_cache():
    """The catalog cache is module state; a leak between tests would hide failures."""
    import capability.sources as mod

    mod._skill_catalog_cache = None
    yield
    mod._skill_catalog_cache = None


@pytest.mark.asyncio
async def test_skills_come_from_the_published_catalog() -> None:
    table = FakeGatewayTable([
        _skill_item("aws-research", "Looks up AWS facts."),
        _skill_item("pdf", "Does PDF things."),
    ])

    rows = {r.asset_id: r for r in await read_builtins("skill", gateway_table=table)}

    assert set(rows) == {"aws-research", "pdf"}
    assert rows["pdf"].description == "Does PDF things."
    assert rows["pdf"].id == "builtin:skill:pdf"
    assert rows["pdf"].capability_class == "builtin"
    # Queried by the prefix the CDK custom resources write, under the shared catalog PK.
    q = table.queries[0]
    assert q["ExpressionAttributeValues"] == {
        ":pk": "GATEWAY#CATALOG",
        ":sk": "__skill__",
    }


@pytest.mark.asyncio
async def test_the_list_does_not_drag_the_bodies_across() -> None:
    """The list must stay projected, and this is the assertion that keeps it that way.

    Each item carries the whole SKILL.md — ~67 KB across the seven. The list renders none
    of it, so an unprojected Query would silently make every Skills tab open that much
    heavier, with nothing visibly wrong. Verified two ways: the request asks for a
    projection, and the summaries it produces carry no body.
    """
    table = FakeGatewayTable([
        _skill_item("pdf", "Does PDF things."),
        _skill_item("aws-research", "Looks up AWS facts."),
    ])

    rows = await read_builtins("skill", gateway_table=table)

    projection = table.queries[0].get("ProjectionExpression")
    assert projection, "the list Query must project, not fetch whole items"
    assert "skillMd" not in projection
    # `name` is a DynamoDB reserved word, so it can only be projected via an alias.
    assert table.queries[0]["ExpressionAttributeNames"] == {"#n": "name"}
    for row in rows:
        assert not hasattr(row, "skill_md")
        assert "skillMd" not in row.to_json()


@pytest.mark.asyncio
async def test_one_skill_s_package_is_fetched_on_demand() -> None:
    """The View path: a GetItem on one key, returning what the detail view renders."""
    from capability.sources import read_builtin_skill

    table = FakeGatewayTable([
        _skill_item("pdf", "Does PDF things.", supportingFiles=["ref/forms.md", "run.py"]),
    ])

    package = await read_builtin_skill("pdf", table)

    assert package is not None
    assert package["name"] == "pdf"
    assert package["skillMd"].startswith("---")
    assert package["supportingFiles"] == ["ref/forms.md", "run.py"]
    # One key, not a scan — the point of per-skill items.
    assert table.gets[0]["Key"] == {"PK": "GATEWAY#CATALOG", "SK": "__skill__pdf"}


@pytest.mark.asyncio
async def test_an_unknown_skill_is_none_not_an_exception() -> None:
    """So the route can answer 404 rather than 503.

    A skill that does not exist is a bad request, not a broken catalog, and the two want
    different status codes — conflating them would make a typo look like an outage.
    """
    from capability.sources import read_builtin_skill

    table = FakeGatewayTable([_skill_item("pdf")])

    assert await read_builtin_skill("no-such-skill", table) is None


@pytest.mark.asyncio
async def test_an_unpublished_catalog_is_an_error_not_an_empty_list() -> None:
    """The failure must be visible.

    An empty list says "this harness ships no built-in skills", which is a product
    claim. A missing row means the deploy's catalog write did not land — so it has to
    surface as a partial error and a "Partial results" banner, not as a tidy empty
    panel that nobody investigates.
    """
    with pytest.raises(SourceUnavailable):
        await read_builtins("skill", gateway_table=FakeGatewayTable([]))

    with pytest.raises(SourceUnavailable):
        await read_builtins("skill", gateway_table=FakeGatewayTable(raises=True))

    # No table configured at all is a config fault, distinctly.
    with pytest.raises(SourceUnavailable):
        await read_builtins("skill", gateway_table=None)


@pytest.mark.asyncio
async def test_a_skill_without_a_description_stays_absent_not_blank() -> None:
    """Absent ≠ empty: the row renders no description line rather than a blank one."""
    table = FakeGatewayTable([_skill_item("mystery")])

    rows = await read_builtins("skill", gateway_table=table)

    assert rows[0].description is None


@pytest.mark.asyncio
async def test_the_catalog_is_cached_but_not_for_the_container_s_life() -> None:
    """Cached briefly, because the row changes under a running task.

    `cdk deploy` rewrites the row without replacing this task, so a life-of-process
    cache would serve the pre-deploy catalog indefinitely — the staleness this
    projection exists to remove, reintroduced one layer down.
    """
    import capability.sources as mod

    table = FakeGatewayTable([_skill_item("pdf", "x" * 30)])

    await read_builtins("skill", gateway_table=table)
    await read_builtins("skill", gateway_table=table)
    assert len(table.queries) == 1, "second read inside the TTL should be cached"

    # Age the cache past its TTL rather than sleeping.
    cached_at, cached = mod._skill_catalog_cache
    mod._skill_catalog_cache = (cached_at - mod._SKILL_CATALOG_TTL_SECONDS - 1, cached)

    await read_builtins("skill", gateway_table=table)
    assert len(table.queries) == 2, "the cache must expire, or a deploy is never seen"


@pytest.mark.asyncio
async def test_skills_do_not_claim_live_session_state() -> None:
    """`activated` is the runtime's business, not the catalog's.

    The catalog cannot know which skills are loaded into an agent session, and a
    default of "not activated" would be a false negative rendered as fact. The UI
    merges that in separately.
    """
    table = FakeGatewayTable([_skill_item("pdf", "x" * 30)])

    for row in await read_builtins("skill", gateway_table=table):
        assert row.configured is None, (
            f"{row.asset_id} reports a deployment/session state the catalog cannot know"
        )
