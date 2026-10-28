import json
import os
import pathlib
import re
import sys

import pytest


@pytest.fixture
def mod(monkeypatch):
    # Fresh import each test so the module-level load() re-runs under the test's env.
    monkeypatch.delenv("RUNTIME_CONFIG_SECRET_ARN", raising=False)
    sys.modules.pop("runtime_config_bootstrap", None)
    import runtime_config_bootstrap as m
    return m


def test_noop_without_pointer(monkeypatch, mod):
    monkeypatch.delenv("RUNTIME_CONFIG_SECRET_ARN", raising=False)
    assert mod.load() == 0


def test_populates_and_does_not_override(monkeypatch, mod):
    monkeypatch.setenv("RUNTIME_CONFIG_SECRET_ARN", "arn:aws:secretsmanager:us-west-2:1:secret:cfg")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("SKILLS_S3_BUCKET", "already-set")  # must be preserved
    payload = {
        "SKILLS_S3_BUCKET": "from-secret",
        "ASSET_INDEX_TABLE": "idx",
        "DELEGATION_SIGNING_KEY_ARN": "arn:kms:...",
        "ASYNC_PLAN_CLIENT_SECRET": "shhh",
    }

    class _SM:
        def get_secret_value(self, SecretId):
            return {"SecretString": json.dumps(payload)}

    monkeypatch.setattr(mod, "_sm_client", lambda region: _SM())

    n = mod.load()
    assert os.environ["SKILLS_S3_BUCKET"] == "already-set"      # setdefault preserved existing
    assert os.environ["ASSET_INDEX_TABLE"] == "idx"             # new key populated
    assert os.environ["DELEGATION_SIGNING_KEY_ARN"] == "arn:kms:..."
    assert os.environ["ASYNC_PLAN_CLIENT_SECRET"] == "shhh"
    assert n >= 3


def test_failopen_on_fetch_error(monkeypatch, mod):
    monkeypatch.setenv("RUNTIME_CONFIG_SECRET_ARN", "arn:aws:secretsmanager:us-west-2:1:secret:cfg")

    def _boom(region):
        raise RuntimeError("no access")

    monkeypatch.setattr(mod, "_sm_client", _boom)
    assert mod.load() == 0  # never raises; leaves env as-is


def test_entrypoints_import_bootstrap_before_firstparty():
    root = pathlib.Path(__file__).resolve().parents[1] / "src"
    # The MCP proxy (agent/mcp-servers/aws-mcp/mcp_server.py) is a separate package with a tiny
    # env and is intentionally excluded. These five agent/src runtimes carry the large env.
    for f in ["main.py", "task_agent.py", "planning_agent.py",
              "render_agent.py"]:
        text = (root / f).read_text()
        assert "import runtime_config_bootstrap" in text, f"{f}: missing bootstrap import"
        boot = text.index("import runtime_config_bootstrap")
        firsts = [m.start() for m in re.finditer(r"^from (security|mcp_manager|model|tools)\b", text, re.M)]
        if firsts:  # entrypoint has a first-party import to anchor against
            assert boot < min(firsts), f"{f}: bootstrap import must precede first-party imports"


# --- runtime-ARN SSM parameters (RUNTIME_ARN_PARAM_PATH) ---------------------------------------

_ARN_KEYS = ("TASK_AGENT_RUNTIME_ARN", "PLANNING_AGENT_RUNTIME_ARN",
             "RENDER_AGENT_RUNTIME_ARN", "RUNTIME_ARN_PARAM_PATH")


@pytest.fixture
def arn_env(monkeypatch):
    # setenv-then-delenv so monkeypatch removes keys load() writes straight into os.environ.
    for k in _ARN_KEYS:
        monkeypatch.setenv(k, "placeholder")
        monkeypatch.delenv(k)
    return monkeypatch


class _SSM:
    def __init__(self, params, fail=None):
        self.params, self.fail, self.calls = params, fail, []

    def get_parameters_by_path(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise self.fail
        items = [{"Name": f"{kw['Path']}/{k}", "Value": v} for k, v in self.params.items()]
        if "NextToken" not in kw:
            return {"Parameters": items[:1], "NextToken": "1"} if len(items) > 1 else {"Parameters": items}
        return {"Parameters": items[1:]}


def test_hydrates_runtime_arns_from_ssm_path_after_secret(arn_env, mod):
    path = "/mediaworks-harness-test/runtime-arns"
    arn_env.setenv("RUNTIME_CONFIG_SECRET_ARN", "arn:aws:secretsmanager:us-west-2:1:secret:cfg")
    arn_env.setenv("RENDER_AGENT_RUNTIME_ARN", "already-set")  # setdefault: never overridden

    class _SM:
        def get_secret_value(self, SecretId):
            # The path arrives in the secret, exactly as the deploy writes it.
            return {"SecretString": json.dumps({"RUNTIME_ARN_PARAM_PATH": path})}

    ssm = _SSM({"TASK_AGENT_RUNTIME_ARN": "arn:task", "RENDER_AGENT_RUNTIME_ARN": "arn:render",
                "PLANNING_AGENT_RUNTIME_ARN": "arn:planning"})
    arn_env.setattr(mod, "_sm_client", lambda region: _SM())
    arn_env.setattr(mod, "_ssm_client", lambda region: ssm)

    n = mod.load()
    assert os.environ["RUNTIME_ARN_PARAM_PATH"] == path
    assert os.environ["TASK_AGENT_RUNTIME_ARN"] == "arn:task"
    assert os.environ["PLANNING_AGENT_RUNTIME_ARN"] == "arn:planning"
    assert os.environ["RENDER_AGENT_RUNTIME_ARN"] == "already-set"
    assert "TASK_AGENT_RUNTIME_ARN" in os.environ
    assert ssm.calls[0]["Path"] == path and len(ssm.calls) == 2  # paginated
    assert n == 3  # path + task + planning


def test_runtime_arns_fail_open(arn_env, mod):
    arn_env.setenv("RUNTIME_ARN_PARAM_PATH", "/p/runtime-arns")
    arn_env.setattr(mod, "_ssm_client", lambda region: _SSM({}, fail=RuntimeError("AccessDenied")))
    assert mod.load() == 0  # never raises
    assert "TASK_AGENT_RUNTIME_ARN" not in os.environ


def test_runtime_arns_client_failure_fails_open(arn_env, mod):
    arn_env.setenv("RUNTIME_ARN_PARAM_PATH", "/p/runtime-arns")

    def _boom(region):
        raise RuntimeError("no boto3")

    arn_env.setattr(mod, "_ssm_client", _boom)
    assert mod.load() == 0


def test_no_ssm_call_without_path(arn_env, mod):
    called = []
    arn_env.setattr(mod, "_ssm_client", lambda region: called.append(region))
    assert mod.load() == 0
    assert called == []
