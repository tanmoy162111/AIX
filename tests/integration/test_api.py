"""The HTTP API (PLAYBOOK §24, M9.1): auth scopes, run lifecycle, SSE, approvals, artifacts."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

import repos
from aix.api.app import create_app
from aix.api.auth import API_SCOPES, Scope
from aix.security.approvals import ensure_token
from cli_env import hermetic
from verif_env import write_fast_config

READ_TOKEN = "read-token-0123456789"
RUN_TOKEN = "run-token-0123456789"
APPROVE_TOKEN = "approve-token-0123456789"
TOKENS = {
    READ_TOKEN: frozenset({Scope.READ}),
    RUN_TOKEN: frozenset({Scope.READ, Scope.RUN}),
    APPROVE_TOKEN: frozenset({Scope.READ, Scope.APPROVE}),
}


def h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def write_scripts(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    steps: dict[str, dict[str, object]] = {
        "implement": {"write_files": {"feature.py": "def f():\n    return 1\n"}},
        "test": {"write_files": {"tests/test_feature.py": "def test_x():\n    assert True\n"}},
        "document": {"write_files": {"docs/feature.md": "# Feature\n"}},
    }
    for name, body in steps.items():
        (d / f"{name}.yaml").write_text(
            yaml.safe_dump({"match": {"task_type": name}, "attempts": [body]})
        )
    return d


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    hermetic(tmp_path, monkeypatch)
    monkeypatch.delenv("AIX_AGENT_CONTEXT", raising=False)
    proj = repos.materialize_sample_py(tmp_path / "proj")
    (proj / ".aix").mkdir()
    write_fast_config(proj)
    monkeypatch.setenv("AIX_FAKE_SCRIPTS", str(write_scripts(tmp_path / "scripts")))
    return proj


@pytest.fixture
def client(project: Path) -> Iterator[TestClient]:
    with TestClient(create_app(project, tokens=TOKENS)) as c:
        yield c


def wait_terminal(client: TestClient, run_id: str, timeout: float = 120.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        doc = client.get(f"/runs/{run_id}", headers=h(READ_TOKEN)).json()
        if doc["status"] in ("completed", "failed", "cancelled"):
            client.get(f"/runs/{run_id}/events", headers=h(READ_TOKEN))  # follows until settled
            return doc
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} did not finish")


def start(client: TestClient, goal: str = "Add a retry option") -> str:
    r = client.post("/runs", json={"goal": goal}, headers=h(RUN_TOKEN))
    assert r.status_code == 202, r.text
    return r.json()["run_id"]


# ---- auth ---------------------------------------------------------------------------------------


def test_every_endpoint_requires_a_bearer_token(client: TestClient) -> None:
    for method, path in [
        ("GET", "/runs"),
        ("GET", "/runs/x"),
        ("GET", "/runs/x/tasks"),
        ("GET", "/runs/x/events"),
        ("GET", "/approvals"),
        ("GET", "/artifacts/x"),
        ("POST", "/runs"),
        ("POST", "/runs/x/cancel"),
        ("POST", "/approvals/x"),
    ]:
        assert client.request(method, path).status_code == 401, (method, path)
        bad = client.request(method, path, headers=h("nope"))
        assert bad.status_code == 401, (method, path)
        assert bad.headers["www-authenticate"].lower().startswith("bearer")


def test_scopes_are_enforced(client: TestClient) -> None:
    assert client.get("/runs", headers=h(READ_TOKEN)).status_code == 200
    assert client.post("/runs", json={"goal": "x"}, headers=h(READ_TOKEN)).status_code == 403
    assert client.post("/runs/x/cancel", headers=h(READ_TOKEN)).status_code == 403
    body = {"decision": "grant"}
    assert client.post("/approvals/x", json=body, headers=h(READ_TOKEN)).status_code == 403
    assert client.post("/approvals/x", json=body, headers=h(RUN_TOKEN)).status_code == 403
    assert client.get("/approvals", headers=h(APPROVE_TOKEN)).status_code == 200


def test_api_scopes_are_a_closed_set() -> None:
    assert {s.value for s in API_SCOPES} == {"read", "run", "approve"}


# ---- run lifecycle ------------------------------------------------------------------------------


def test_run_lifecycle_tasks_events_and_artifacts(client: TestClient) -> None:
    run_id = start(client)
    doc = wait_terminal(client, run_id)
    assert doc["status"] == "completed", doc
    assert doc["goal"] == "Add a retry option"
    assert doc["branch"] == f"aix/run/{run_id}"

    assert [r["run_id"] for r in client.get("/runs", headers=h(READ_TOKEN)).json()] == [run_id]

    tasks = client.get(f"/runs/{run_id}/tasks", headers=h(READ_TOKEN)).json()
    assert tasks and {t["status"] for t in tasks} == {"completed"}
    assert {"task_id", "type", "title", "agent", "attempts"} <= set(tasks[0])

    r = client.get(f"/runs/{run_id}/events?follow=false", headers=h(READ_TOKEN))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    types = [e["event"] for e in events]
    assert types[0] == "run.created" and "run.completed" in types
    assert [int(e["id"]) for e in events] == sorted(int(e["id"]) for e in events)

    arts = [e for e in events if e["event"] == "artifact.created"]
    assert arts
    art_id = json.loads(arts[0]["data"])["payload"]["artifact"]["id"]
    meta = client.get(f"/artifacts/{art_id}", headers=h(READ_TOKEN))
    assert meta.status_code == 200 and meta.json()["id"] == art_id
    content = client.get(f"/artifacts/{art_id}/content", headers=h(READ_TOKEN))
    assert content.status_code == 200 and len(content.content) == meta.json()["size"]


def test_unknown_ids_are_404(client: TestClient) -> None:
    for path in ("/runs/run_nope", "/runs/run_nope/tasks", "/runs/run_nope/events"):
        assert client.get(path, headers=h(READ_TOKEN)).status_code == 404, path
    assert client.get("/artifacts/art_nope", headers=h(READ_TOKEN)).status_code == 404
    assert client.post("/runs/run_nope/cancel", headers=h(RUN_TOKEN)).status_code == 404


def test_invalid_run_requests_are_rejected_before_anything_is_recorded(
    client: TestClient, project: Path
) -> None:
    assert client.post("/runs", json={}, headers=h(RUN_TOKEN)).status_code == 422
    r = client.post("/runs", json={"goal": "x", "skill": "no-such-skill"}, headers=h(RUN_TOKEN))
    assert r.status_code == 400 and "no-such-skill" in r.json()["detail"]
    dirty = project / "app" / "dirty.py"
    dirty.write_text("x = 1\n")
    repos.git(project, "add", "-A")
    r = client.post("/runs", json={"goal": "Add a retry option"}, headers=h(RUN_TOKEN))
    assert r.status_code == 409
    assert client.get("/runs", headers=h(READ_TOKEN)).json() == []


def test_sse_resumes_after_last_event_id(client: TestClient) -> None:
    run_id = start(client)
    wait_terminal(client, run_id)
    everything = parse_sse(
        client.get(f"/runs/{run_id}/events?follow=false", headers=h(READ_TOKEN)).text
    )
    cut = everything[3]["id"]
    rest = parse_sse(
        client.get(
            f"/runs/{run_id}/events?follow=false",
            headers={**h(READ_TOKEN), "Last-Event-ID": cut},
        ).text
    )
    assert [e["id"] for e in rest] == [e["id"] for e in everything[4:]]
    via_query = parse_sse(
        client.get(
            f"/runs/{run_id}/events?follow=false&after_seq={cut}", headers=h(READ_TOKEN)
        ).text
    )
    assert [e["id"] for e in via_query] == [e["id"] for e in rest]


def test_following_sse_ends_when_the_run_finishes(client: TestClient) -> None:
    run_id = start(client)
    r = client.get(f"/runs/{run_id}/events", headers=h(READ_TOKEN))  # follows until terminal
    events = parse_sse(r.text)
    types = [e["event"] for e in events]
    assert "run.completed" in types
    # artifacts (report, manifest) are written after the terminal event; the stream waits for them
    last = json.loads(events[-1]["data"])
    assert events[-1]["event"] == "artifact.created"
    assert last["payload"]["artifact"]["type"] == "manifest"
    assert wait_terminal(client, run_id)["status"] == "completed"


# ---- approvals ----------------------------------------------------------------------------------


def start_high_risk(client: TestClient) -> tuple[str, str]:
    run_id = start(client, "Deploy to production")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        pending = client.get("/approvals", headers=h(READ_TOKEN)).json()
        if pending:
            return run_id, pending[0]["id"]
        time.sleep(0.1)
    raise AssertionError("no approval requested")


def test_grant_requires_the_approve_scope_and_resumes_the_run(client: TestClient) -> None:
    run_id, apv = start_high_risk(client)
    assert client.get(f"/runs/{run_id}", headers=h(READ_TOKEN)).json()["status"] == (
        "waiting_approval"
    )
    body = {"decision": "grant"}
    assert client.post(f"/approvals/{apv}", json=body, headers=h(RUN_TOKEN)).status_code == 403
    r = client.post(f"/approvals/{apv}", json=body, headers=h(APPROVE_TOKEN))
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["approval_id"] == apv and doc["decision"] == "grant" and doc["run_id"] == run_id
    assert wait_terminal(client, run_id)["status"] == "completed"
    assert client.get("/approvals", headers=h(READ_TOKEN)).json() == []
    # the recorded channel is the API token channel
    events = parse_sse(
        client.get(f"/runs/{run_id}/events?follow=false", headers=h(READ_TOKEN)).text
    )
    granted = next(e for e in events if e["event"] == "approval.granted")
    assert json.loads(granted["data"])["payload"]["channel"] == "api_token"
    # deciding twice is a conflict, not a second grant
    again = client.post(f"/approvals/{apv}", json=body, headers=h(APPROVE_TOKEN))
    assert again.status_code == 404


def test_deny_fails_the_run(client: TestClient) -> None:
    run_id, apv = start_high_risk(client)
    r = client.post(
        f"/approvals/{apv}",
        json={"decision": "deny", "reason": "too risky"},
        headers=h(APPROVE_TOKEN),
    )
    assert r.status_code == 200 and r.json()["decision"] == "deny"
    assert wait_terminal(client, run_id)["status"] == "failed"


def test_approval_refused_inside_an_agent_context(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, apv = start_high_risk(client)
    monkeypatch.setenv("AIX_AGENT_CONTEXT", "1")
    r = client.post(f"/approvals/{apv}", json={"decision": "grant"}, headers=h(APPROVE_TOKEN))
    assert r.status_code == 403 and "agent context" in r.json()["detail"]
    assert client.get("/approvals", headers=h(READ_TOKEN)).json()  # still pending


def test_bad_decision_is_a_validation_error(client: TestClient) -> None:
    r = client.post("/approvals/x", json={"decision": "maybe"}, headers=h(APPROVE_TOKEN))
    assert r.status_code == 422


# ---- cancel -------------------------------------------------------------------------------------


def test_cancel_a_run_waiting_for_approval(client: TestClient) -> None:
    run_id, _ = start_high_risk(client)
    r = client.post(f"/runs/{run_id}/cancel", headers=h(RUN_TOKEN))
    assert r.status_code == 200 and r.json()["result"] == "cancelled"
    assert client.get(f"/runs/{run_id}", headers=h(READ_TOKEN)).json()["status"] == "cancelled"
    again = client.post(f"/runs/{run_id}/cancel", headers=h(RUN_TOKEN))
    assert again.json()["result"] == "already"


# ---- tokens & serving ---------------------------------------------------------------------------


def test_default_tokens_come_from_the_config_dir(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(project)  # tokens=None: the api token file + the approval token
    with TestClient(app) as c:
        assert c.get("/runs").status_code == 401
        approval = ensure_token()
        assert c.get("/approvals", headers=h(approval)).status_code == 200  # read + approve
        assert c.post("/runs", json={"goal": "x"}, headers=h(approval)).status_code == 403
    from aix.api.auth import ensure_api_token

    api = ensure_api_token()
    assert api != approval
    with TestClient(app) as c:
        assert c.post("/runs/none/cancel", headers=h(api)).status_code == 404  # has run scope
        assert (
            c.post("/approvals/none", json={"decision": "grant"}, headers=h(api)).status_code == 403
        )


def parse_sse(text: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.split("\n"):
            if line.startswith(":") or ":" not in line:
                continue
            k, _, v = line.partition(":")
            fields[k] = v.removeprefix(" ")
        if fields:
            out.append(fields)
    return out
