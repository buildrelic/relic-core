"""POST /v1/ingest: per-user token threading, fallback, and the security boundary.

The web app passes the connecting user's github token in the body so ingest reads
their repos. The token must reach the ingest child through its environment, never the
argv (which leaks to the process list), and must never be logged or returned.
"""

import subprocess

import pytest
from starlette.testclient import TestClient

from relic.serve import build_http_app


def _client(trigger, *, token=None):
    async def connectors():
        return {"connectors": []}

    async def ingest_runs(repo, limit):
        return {"runs": [], "totals": {}}

    return TestClient(
        build_http_app(
            connectors=connectors, ingest_runs=ingest_runs, ingest_trigger=trigger, token=token
        )
    )


def test_post_ingest_passes_token_to_trigger():
    seen = {}

    async def trigger(repo, token):
        seen["repo"], seen["token"] = repo, token
        return {"status": "running", "repo": repo}

    resp = _client(trigger).post("/v1/ingest", json={"repo": "owner/name", "token": "gho_abc"})
    assert resp.status_code == 202
    assert seen == {"repo": "owner/name", "token": "gho_abc"}
    assert "gho_abc" not in resp.text  # the token is never echoed back


def test_post_ingest_without_token_is_none():
    seen = {}

    async def trigger(repo, token):
        seen["token"] = token
        return {"status": "running", "repo": repo}

    resp = _client(trigger).post("/v1/ingest", json={"repo": "owner/name"})
    assert resp.status_code == 202
    assert seen["token"] is None


def test_post_ingest_blank_token_is_none():
    seen = {}

    async def trigger(repo, token):
        seen["token"] = token
        return {"status": "running", "repo": repo}

    resp = _client(trigger).post("/v1/ingest", json={"repo": "owner/name", "token": "   "})
    assert resp.status_code == 202
    assert seen["token"] is None


def test_post_ingest_already_running_is_409():
    async def trigger(repo, token):
        return {"status": "already_running", "repo": repo}

    resp = _client(trigger).post("/v1/ingest", json={"repo": "owner/name", "token": "x"})
    assert resp.status_code == 409


def test_post_ingest_token_still_behind_bearer_auth():
    async def trigger(repo, token):
        return {"status": "running", "repo": repo}

    client = _client(trigger, token="secret")
    # the per-user token in the body does not bypass the endpoint's bearer auth
    unauthed = client.post("/v1/ingest", json={"repo": "owner/name", "token": "x"})
    assert unauthed.status_code == 401
    authed = client.post(
        "/v1/ingest",
        json={"repo": "owner/name"},
        headers={"Authorization": "Bearer secret"},
    )
    assert authed.status_code == 202


def test_trigger_ingest_passes_token_via_child_env_not_argv(monkeypatch):
    from relic import cli

    cli._INGEST_PROCS.clear()
    captured = {}

    class _FakeProc:
        def poll(self):
            return None

    def _fake_popen(args, env=None):
        captured["args"], captured["env"] = list(args), env
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)

    cli._trigger_ingest("owner/repo", "gho_secret")
    assert "gho_secret" not in captured["args"]  # never on the command line
    assert captured["env"]["GITHUB_TOKEN"] == "gho_secret"

    # no token: the child inherits the parent env (env=None), no override
    cli._INGEST_PROCS.clear()
    cli._trigger_ingest("owner/repo2", None)
    assert captured["env"] is None


@pytest.mark.parametrize(
    "bad",
    [
        "gho_ has space",  # internal whitespace
        "gho_\x00null",  # embedded NUL byte
        "#leading-hash",  # what the child's _clean_secret would silently drop
        "gho_" + "x" * 300,  # over the length bound
    ],
)
def test_post_ingest_rejects_malformed_token(bad):
    """A malformed token is a 400, never a silent run as the server/host identity."""
    called = False

    async def trigger(repo, token):
        nonlocal called
        called = True
        return {"status": "running", "repo": repo}

    resp = _client(trigger).post("/v1/ingest", json={"repo": "owner/name", "token": bad})
    assert resp.status_code == 400
    assert not called  # the bad token never reaches the ingest trigger
    assert bad not in resp.text


def test_post_ingest_strips_and_accepts_padded_token():
    seen = {}

    async def trigger(repo, token):
        seen["token"] = token
        return {"status": "running", "repo": repo}

    resp = _client(trigger).post(
        "/v1/ingest", json={"repo": "owner/name", "token": "  gho_ok123  "}
    )
    assert resp.status_code == 202
    assert seen["token"] == "gho_ok123"
