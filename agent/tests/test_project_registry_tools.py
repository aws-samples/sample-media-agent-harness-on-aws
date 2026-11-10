"""list_projects / switch_project / create_project / delete_project and the registry client under them."""
from __future__ import annotations

import asyncio

import pytest

from project_search import project_tools as PT
from project_search import turn_actions
from project_search.registry_client import RegistryClient

PID = "p0123456789abcdef0123456789abcdef"
LAB = "p11111111111111111111111111111111"
ROWS = [
    {"projectId": PID, "name": "Season Promo", "role": "owner", "personal": False, "indexing": True},
    {"projectId": LAB, "name": "workshop-lab01", "role": "member", "personal": False, "indexing": False},
    {"projectId": "ppersonal", "name": "Personal", "role": "owner", "personal": True},
    {"projectId": "pmine", "name": "Old cuts", "role": "owner", "personal": False, "activeAssetCount": 3},
]


class FakeRegistry:
    def __init__(self, rows=None, create=None):
        self.rows, self.created = list(ROWS if rows is None else rows), []
        self._create = create

    def list(self):
        return {"projects": self.rows}

    def create(self, name, *, description="", indexing=True):
        self.created.append({"name": name, "description": description, "indexing": indexing})
        if self._create is not None:
            return self._create
        return {"project": {"projectId": "pnew", "name": name}}


def run(coro):
    return asyncio.run(coro)


def tools(reg, current=PID, name="Season Promo"):
    ts = PT.make_project_tools(current, function_name="asset-fn", client=object(), token_source=lambda: "tok",
                               project_name=name, registry=reg)
    return {t.tool_name: t for t in ts}


@pytest.fixture(autouse=True)
def turn(monkeypatch):
    monkeypatch.setattr(PT, "_request_ids", lambda: ("sess-1", "t1"))
    turn_actions._PENDING.clear()
    yield
    turn_actions._PENDING.clear()


# ------------------------------------------------------------------------------------------------- list_projects

def test_list_projects_marks_the_current_one_and_hides_ids():
    out = run(tools(FakeRegistry())["list_projects"]())
    names = [p["name"] for p in out["projects"]]
    assert names == ["Season Promo", "workshop-lab01", "Personal", "Old cuts"]
    assert out["projects"][0]["current"] is True and "current" not in out["projects"][1]
    assert all("projectId" not in p for p in out["projects"])
    assert out["projects"][3]["activeAssetCount"] == 3 and out["current"] == "Season Promo"


def test_list_projects_passes_a_registry_error_through():
    class Down(FakeRegistry):
        def list(self):
            return {"error": "unavailable", "message": "try again"}
    assert run(tools(Down())["list_projects"]())["error"] == "unavailable"


# ------------------------------------------------------------------------------------------------ switch_project

def test_switch_project_records_one_open_request_with_the_prompt():
    out = run(tools(FakeRegistry())["switch_project"](name="  WORKSHOP-lab01 ", prompt="start lab01"))
    assert out["status"] == "opening_new_chat" and out["project"]["name"] == "workshop-lab01"
    assert turn_actions.take_open_project("sess-1") == {
        "projectId": LAB, "projectName": "workshop-lab01", "prompt": "start lab01"}
    assert turn_actions.take_open_project("sess-1") is None   # taken once


def test_switch_project_to_the_current_project_opens_nothing():
    out = run(tools(FakeRegistry())["switch_project"](name="season promo", prompt="x"))
    assert out["status"] == "already_here"
    assert turn_actions.take_open_project("sess-1") is None


def test_only_the_first_switch_of_a_turn_counts():
    ts = tools(FakeRegistry())
    assert run(ts["switch_project"](name="workshop-lab01"))["status"] == "opening_new_chat"
    assert run(ts["switch_project"](name="Old cuts"))["error"] == "already_switching"
    assert turn_actions.take_open_project("sess-1")["projectId"] == LAB


def test_switch_project_unknown_name_offers_near_matches_and_opens_nothing():
    out = run(tools(FakeRegistry())["switch_project"](name="lab"))
    assert out["error"] == "project_not_found" and out["candidates"] == ["workshop-lab01"]
    assert run(tools(FakeRegistry())["switch_project"](name="nothing-like-it"))["error"] == "project_not_found"
    assert turn_actions.take_open_project("sess-1") is None


def test_switch_project_with_two_projects_of_that_name_asks():
    rows = ROWS + [{"projectId": "pdup", "name": "workshop-lab01", "role": "owner", "personal": False}]
    out = run(tools(FakeRegistry(rows))["switch_project"](name="workshop-lab01"))
    assert out["error"] == "ambiguous_name" and turn_actions.take_open_project("sess-1") is None


def test_the_prompt_is_capped():
    run(tools(FakeRegistry())["switch_project"](name="Old cuts", prompt="x" * 5000))
    assert len(turn_actions.take_open_project("sess-1")["prompt"]) == turn_actions.PROMPT_MAX_CHARS


# ------------------------------------------------------------------------------------------------ create_project

def test_create_project_creates_it_and_opens_a_chat_there():
    reg = FakeRegistry()
    out = run(tools(reg)["create_project"](name="  Golf   promo ", description="Q3", indexing=False, prompt="hi"))
    assert reg.created == [{"name": "Golf promo", "description": "Q3", "indexing": False}]
    assert out["created"] is True and out["status"] == "opening_new_chat"
    assert turn_actions.take_open_project("sess-1") == {"projectId": "pnew", "projectName": "Golf promo",
                                                         "prompt": "hi"}


def test_create_project_refusals_open_nothing():
    reg = FakeRegistry(create={"error": "name_taken", "message": "You already have a project with that name"})
    assert run(tools(reg)["create_project"](name="Old cuts"))["error"] == "name_taken"
    assert run(tools(FakeRegistry())["create_project"](name="   "))["error"] == "invalid_name"
    assert turn_actions.take_open_project("sess-1") is None


# ------------------------------------------------------------------------------------------------ delete_project

def test_delete_project_only_offers_a_card_and_deletes_nothing():
    reg = FakeRegistry()
    out = run(tools(reg)["delete_project"](name="old cuts"))
    assert out["status"] == "awaiting_user_confirmation"
    assert out["markdown"] == "[Delete project “Old cuts”](project:delete?id=pmine&name=Old%20cuts)"
    assert reg.created == [] and turn_actions.take_open_project("sess-1") is None


def test_delete_project_refuses_personal_and_projects_the_user_does_not_own():
    ts = tools(FakeRegistry())
    assert ts["delete_project"] and run(ts["delete_project"](name="Personal"))["error"] == "personal_project"
    assert run(ts["delete_project"](name="workshop-lab01"))["error"] == "not_owner"


def test_the_new_tools_are_named_in_the_turn_prompt_and_never_take_a_project_id():
    from project_search import turn as T
    for name in ("list_projects", "switch_project", "create_project", "delete_project"):
        assert f"`{name}`" in T._ORGANIZE_LINE
        assert name in PT.TOOL_NAMES
    for t in tools(FakeRegistry()).values():
        props = t.tool_spec["inputSchema"]["json"]["properties"]
        assert not {"project_id", "projectId", "id"} & set(props)


# ------------------------------------------------------------------------------------------------ RegistryClient

class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


def client(resp, origin="https://d1.cloudfront.net", token="tok"):
    s = FakeSession(resp)
    return RegistryClient(origin=lambda: origin, token=lambda: token, session=s), s


def test_client_calls_the_proxy_with_the_users_token_and_no_redirects():
    c, s = client(FakeResp(200, {"projects": [{"projectId": "p", "name": "A"}, "junk"]}))
    assert c.list() == {"projects": [{"projectId": "p", "name": "A"}]}
    method, url, kw = s.calls[0]
    assert (method, url) == ("GET", "https://d1.cloudfront.net/projects")
    assert kw["headers"]["Authorization"] == "Bearer tok" and kw["allow_redirects"] is False and kw["timeout"]


def test_client_create_sends_name_indexing_and_description():
    c, s = client(FakeResp(201, {"project": {"projectId": "pn", "name": "N"}}))
    assert c.create("N", description="d", indexing=False)["project"]["projectId"] == "pn"
    assert s.calls[0][0:2] == ("POST", "https://d1.cloudfront.net/projects")
    assert s.calls[0][2]["json"] == {"name": "N", "indexing": False, "description": "d"}


def test_client_maps_errors_and_never_raises():
    c, _ = client(FakeResp(409, {"error": {"code": "name_taken", "message": "taken"}}))
    assert c.create("x") == {"error": "name_taken", "message": "taken"}
    c, _ = client(FakeResp(200, ValueError("html")))           # CloudFront's SPA fallback
    assert c.list()["error"] == "unavailable"
    c, _ = client(OSError("boom"))
    assert c.list()["error"] == "unavailable"


def test_client_refuses_without_a_token_or_an_https_origin():
    c, s = client(FakeResp(200, {}), token="")
    assert c.list()["error"] == "unauthenticated" and s.calls == []
    from project_search import registry_client as RC
    for bad in ("http://d1.cloudfront.net", "https://d1.cloudfront.net/x", "", "d1.cloudfront.net"):
        import runtime_arns
        orig = runtime_arns.get
        runtime_arns.get = lambda name, _v=bad: _v
        try:
            assert RC._origin() == ""
        finally:
            runtime_arns.get = orig
    c, s = client(FakeResp(200, {}), origin="")
    assert c.list()["error"] == "unavailable" and s.calls == []


def test_main_takes_the_turns_open_request_once():
    import main
    turn_actions.request_open_project("sess-9", "pid", "Name", "go")
    assert main._take_open_project("sess-9") == {"projectId": "pid", "projectName": "Name", "prompt": "go"}
    assert main._take_open_project("sess-9") is None
