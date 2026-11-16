"""Tests for Wave 1 option C (fail loudly) + option E (mint/persist grant).

Covers the orchestrator half of the signed-delegation-grant design:

* `_tool_is_user_gated` / `_user_gated_tools_in_plan` — the detector that
  decides whether a plan touches a user_jwt-gated (`usr-`) gateway tool, with
  the two-separator case pinned (`usr-x___aws___call_aws`).
* Option C — a plan needing a user_jwt-gated tool is REJECTED (not dispatched)
  when no grant can be minted, with an actionable message.
* Option E — when grants are enabled and a key is configured, a grant is minted
  and PERSISTED on the PLAN row beside actor_id; a non-gated plan mints nothing.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from security import delegation  # noqa: E402
from orchestration import plan_and_execute_tool as pae  # noqa: E402


class _Node:
    def __init__(self, node_id, tools, depends_on=None):
        self.id = node_id
        self.tools = list(tools)
        self.depends_on = list(depends_on or [])
        self.prompt = f"prompt for {node_id}"
        self.output_type = "json"
        self.output_key = None
        self.skills = []

    def to_dict(self):
        return {"id": self.id, "tools": self.tools, "depends_on": self.depends_on,
                "prompt": self.prompt, "output_type": self.output_type}


class _Plan:
    def __init__(self, nodes, goal="do stuff", plan_id="plan-display"):
        self.nodes = nodes
        self.goal = goal
        self.plan_id = plan_id


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


def test_user_gated_detects_usr_target():
    assert pae._tool_is_user_gated("usr-abc123___run_model")


def test_user_gated_two_separator_name_uses_first_split():
    """A bare tool name that itself contains '___' must not fool the detector."""
    assert pae._tool_is_user_gated("usr-abc123___aws___call_aws")
    # Platform aws-mcp target is NOT a usr- target.
    assert not pae._tool_is_user_gated("mediaworks-harness-aws-mcp___aws___call_aws")


def test_user_gated_detects_user_jwt_gateway_tool_via_catalog_set():
    """A non-usr- target on a user_jwt gateway (the aws-mcp documentation target
    on the harness gateway) is gated when the catalog-derived user_jwt tool set
    includes it — even though the name-only fast-path cannot tell.

    Regression for the research node that failed: no grant was minted because
    the aws-mcp tool isn't usr- prefixed, so the plan ran headless and could not
    reach the user_jwt harness gateway.
    """
    tool = "mediaworks-harness-aws-mcp___aws___search_documentation"
    plan = _Plan([_Node("research_media_services", [tool])])
    # Name-only fast-path alone: missed (the historic gap).
    assert pae._user_gated_tools_in_plan(plan) == []
    # With the catalog set (gateway auth_type == user_jwt): detected → grant required.
    assert pae._user_gated_tools_in_plan(plan, {tool}) == [tool]


def test_user_gated_ignores_platform_and_unqualified():
    assert not pae._tool_is_user_gated("media-outputs___get_download_url")
    assert not pae._tool_is_user_gated("code_interpreter")
    assert not pae._tool_is_user_gated("")


def test_user_gated_tools_in_plan_dedupes_and_sorts():
    plan = _Plan([
        _Node("a", ["usr-z9___t1", "code_interpreter"]),
        _Node("b", ["usr-a1___t2", "usr-z9___t1"]),
        _Node("c", ["media-outputs___get_download_url"]),
    ])
    assert pae._user_gated_tools_in_plan(plan) == ["usr-a1___t2", "usr-z9___t1"]


# ---------------------------------------------------------------------------
# Option C — reject when no grant can be minted
# ---------------------------------------------------------------------------


def _set_actor(monkeypatch, actor="USER#alice"):
    from jobs import context as jobs_context
    monkeypatch.setattr(jobs_context, "current_actor_id", lambda: actor)
    monkeypatch.setattr(jobs_context, "current_session_id", lambda: "sess-1")
    monkeypatch.setattr(jobs_context, "current_turn_id", lambda: "turn-1")


def test_option_c_rejects_gated_plan_when_grants_disabled(monkeypatch):
    _set_actor(monkeypatch)
    monkeypatch.delenv(delegation.ENABLED_ENV, raising=False)
    monkeypatch.setenv(delegation.SIGNING_KEY_ENV, "arn:aws:kms:us-west-2:1:key/k")
    plan = _Plan([_Node("n1", ["usr-abc123___run_model"])])

    result = pae._dispatch_plan_async(
        tool_use_id="tu-1", plan=plan, execution_mode="standard",
        cumulative_tokens={"inputTokens": 0, "outputTokens": 0}, session_id="s",
    )
    assert result["status"] == "error"
    text = result["content"][0]["text"]
    assert "usr-abc123___run_model" in text
    assert "not enabled" in text
    # It must NOT have reported an async dispatch.
    assert "executionSummary" not in result


def test_option_c_rejects_when_no_actor(monkeypatch):
    _set_actor(monkeypatch, actor="")
    monkeypatch.setenv(delegation.ENABLED_ENV, "true")
    monkeypatch.setenv(delegation.SIGNING_KEY_ENV, "arn:aws:kms:us-west-2:1:key/k")
    plan = _Plan([_Node("n1", ["usr-abc123___run_model"])])

    result = pae._dispatch_plan_async(
        tool_use_id="tu-1", plan=plan, execution_mode="standard",
        cumulative_tokens={"inputTokens": 0, "outputTokens": 0}, session_id="s",
    )
    assert result["status"] == "error"
    assert "actor identity" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# Option E — mint + persist
# ---------------------------------------------------------------------------


class _FakeKmsSign:
    def sign(self, *, KeyId, Message, MessageType, SigningAlgorithm):  # noqa: N803
        return {"Signature": b"\x30\x45test-signature-bytes"}


class _CapturingTable:
    def __init__(self):
        self.items = []

    def put_item(self, Item):  # noqa: N803
        self.items.append(Item)
        return {}


class _FakeStateStore:
    last = None

    def __init__(self, *args, **kwargs):
        self._table = _CapturingTable()
        _FakeStateStore.last = self


class _FakeManifest:
    def __init__(self, *args, **kwargs):
        self.s3_uri = "s3://bucket/plans/x/manifest.json"

    def create(self, **kwargs):
        return None


def _wire_persist(monkeypatch):
    """Stub the DDB/S3/registry side so the write path runs in-memory."""
    import state_store
    import planning.plan_manifest as plan_manifest
    monkeypatch.setattr(state_store, "StateStore", _FakeStateStore)
    monkeypatch.setattr(plan_manifest, "PlanManifest", _FakeManifest)
    monkeypatch.setattr(pae, "PLAN_STATE_TABLE", "test-plan-state")
    monkeypatch.setattr(pae, "JOB_REGISTRY_TABLE", "")  # skip job-registry block
    monkeypatch.setattr(pae, "SKILLS_S3_BUCKET", "test-bucket")


def test_option_e_mints_and_persists_grant_on_plan_row(monkeypatch):
    _set_actor(monkeypatch)
    monkeypatch.setenv(delegation.ENABLED_ENV, "true")
    monkeypatch.setenv(delegation.SIGNING_KEY_ENV, "arn:aws:kms:us-west-2:1:key/k")
    monkeypatch.setattr(delegation, "_kms_client", lambda: _FakeKmsSign())
    _wire_persist(monkeypatch)

    plan = _Plan([_Node("n1", ["usr-abc123___run_model"])])
    result = pae._dispatch_plan_async(
        tool_use_id="tu-1", plan=plan, execution_mode="standard",
        cumulative_tokens={"inputTokens": 0, "outputTokens": 0}, session_id="s",
    )
    assert result["status"] == "success"
    assert result["executionSummary"]["plan_status"] == "dispatched_async"

    items = _FakeStateStore.last._table.items
    plan_rows = [i for i in items if str(i.get("node_id")).startswith("PLAN#")]
    assert len(plan_rows) == 1
    grant = plan_rows[0].get("delegation_grant")
    assert isinstance(grant, str) and grant.count(".") == 2  # header.payload.sig
    assert plan_rows[0]["actor_id"] == "USER#alice"


def test_option_e_non_gated_plan_mints_no_grant(monkeypatch):
    _set_actor(monkeypatch)
    monkeypatch.setenv(delegation.ENABLED_ENV, "true")
    monkeypatch.setenv(delegation.SIGNING_KEY_ENV, "arn:aws:kms:us-west-2:1:key/k")

    def _must_not_sign():
        raise AssertionError("no grant should be minted for a non-gated plan")

    monkeypatch.setattr(delegation, "_kms_client", _must_not_sign)
    _wire_persist(monkeypatch)

    plan = _Plan([_Node("n1", ["code_interpreter", "media-outputs___get_download_url"])])
    result = pae._dispatch_plan_async(
        tool_use_id="tu-1", plan=plan, execution_mode="standard",
        cumulative_tokens={"inputTokens": 0, "outputTokens": 0}, session_id="s",
    )
    assert result["status"] == "success"
    plan_rows = [i for i in _FakeStateStore.last._table.items
                 if str(i.get("node_id")).startswith("PLAN#")]
    assert "delegation_grant" not in plan_rows[0]


def test_plan_row_carries_the_trusted_producing_project(monkeypatch):
    """The PLAN row (which fires the chainer) carries the producing project itself. Staging plan 73d92d46:
    the chainer dispatched before the job-registry row holding it was written, so the nodes ran with no
    project and their writes to projects/<pid>/sessions/<sid>/output/ were AccessDenied."""
    from jobs import context as jobs_context
    _set_actor(monkeypatch)
    monkeypatch.delenv(delegation.ENABLED_ENV, raising=False)
    _wire_persist(monkeypatch)
    pid = "p925d3f69f8324bf2a844f186835d8db9"
    monkeypatch.setattr(jobs_context, "trusted_project_id", lambda: pid)
    plan = _Plan([_Node("n1", ["code_interpreter"])])
    result = pae._dispatch_plan_async(
        tool_use_id="tu-1", plan=plan, execution_mode="standard",
        cumulative_tokens={"inputTokens": 0, "outputTokens": 0}, session_id="s",
    )
    assert result["status"] == "success"
    plan_rows = [i for i in _FakeStateStore.last._table.items if str(i.get("node_id")).startswith("PLAN#")]
    assert plan_rows[0]["producing_project_id"] == pid
    # And no project (an untrusted or absent one) stamps nothing.
    monkeypatch.setattr(jobs_context, "trusted_project_id", lambda: None)
    pae._dispatch_plan_async(
        tool_use_id="tu-2", plan=plan, execution_mode="standard",
        cumulative_tokens={"inputTokens": 0, "outputTokens": 0}, session_id="s",
    )
    plan_rows = [i for i in _FakeStateStore.last._table.items if str(i.get("node_id")).startswith("PLAN#")]
    assert "producing_project_id" not in plan_rows[0]


def test_a_runtime_maps_template_tools_need_a_grant_too():
    """A `map` node lists no tools; its children run `config.template.tools`. 0e53b848's recast map had the fal tools
    only there: no grant was minted and every child found the fal gateway "awaiting user token"."""
    node = _Node("recast_map", [])
    node.config = {"template": {"tools": ["clip_video", "usr-8ddf___submit_job", "usr-8ddf___check_job"]}}
    assert pae._user_gated_tools_in_plan(_Plan([node])) == ["usr-8ddf___check_job", "usr-8ddf___submit_job"]
