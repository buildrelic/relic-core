"""POST /v1/ingest: source routing, per-user token threading, and the security boundary.

The web app passes a `source` plus the connecting user's secret in the body so ingest reads
their data. For github that's `{repo, token}`; for granola (a non-repo source) it's
`{source: "granola", token}` and the scope is derived server-side. The token must reach the
ingest child through its environment, never the argv (which leaks to the process list), and
must never be logged or returned.
"""

import subprocess

import pytest
from starlette.testclient import TestClient

from relic.serve import build_http_app


def _client(trigger, *, token=None):
    async def connectors():
        return {"connectors": []}

    async def ingest_runs(repo, limit, source):
        return {"runs": [], "totals": {}}

    return TestClient(
        build_http_app(
            connectors=connectors, ingest_runs=ingest_runs, ingest_trigger=trigger, token=token
        )
    )


def test_post_ingest_passes_source_repo_token_to_trigger():
    seen = {}

    async def trigger(source, repo, token, workspace=None):
        seen["source"], seen["repo"], seen["token"] = source, repo, token
        return {"status": "running", "repo": repo}

    resp = _client(trigger).post("/v1/ingest", json={"repo": "owner/name", "token": "gho_abc"})
    assert resp.status_code == 202
    assert seen == {"source": "github", "repo": "owner/name", "token": "gho_abc"}
    assert "gho_abc" not in resp.text  # the token is never echoed back


def test_post_ingest_without_token_is_none():
    seen = {}

    async def trigger(source, repo, token, workspace=None):
        seen["token"] = token
        return {"status": "running", "repo": repo}

    resp = _client(trigger).post("/v1/ingest", json={"repo": "owner/name"})
    assert resp.status_code == 202
    assert seen["token"] is None


def test_post_ingest_blank_token_is_none():
    seen = {}

    async def trigger(source, repo, token, workspace=None):
        seen["token"] = token
        return {"status": "running", "repo": repo}

    resp = _client(trigger).post("/v1/ingest", json={"repo": "owner/name", "token": "   "})
    assert resp.status_code == 202
    assert seen["token"] is None


def test_post_ingest_already_running_is_409():
    async def trigger(source, repo, token, workspace=None):
        return {"status": "already_running", "repo": repo}

    resp = _client(trigger).post("/v1/ingest", json={"repo": "owner/name", "token": "x"})
    assert resp.status_code == 409


def test_post_ingest_token_still_behind_bearer_auth():
    async def trigger(source, repo, token, workspace=None):
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


def test_post_ingest_github_missing_repo_is_400():
    """An explicit github source with no repo is a 400, never a run with an empty repo."""
    called = False

    async def trigger(source, repo, token, workspace=None):
        nonlocal called
        called = True
        return {"status": "running", "repo": repo}

    resp = _client(trigger).post("/v1/ingest", json={"source": "github", "token": "gho_x"})
    assert resp.status_code == 400
    assert not called


def test_post_ingest_unknown_source_is_400():
    called = False

    async def trigger(source, repo, token, workspace=None):
        nonlocal called
        called = True
        return {"status": "running"}

    resp = _client(trigger).post("/v1/ingest", json={"source": "slack", "token": "x"})
    assert resp.status_code == 400
    assert not called


def test_post_ingest_granola_no_repo_routes_with_token():
    """Granola carries a token but no repo; the trigger is called with source=granola, repo=None."""
    seen = {}

    async def trigger(source, repo, token, workspace=None):
        seen["source"], seen["repo"], seen["token"] = source, repo, token
        return {"status": "running", "source": "granola"}

    resp = _client(trigger).post("/v1/ingest", json={"source": "granola", "token": "grn_abc"})
    assert resp.status_code == 202
    assert seen == {"source": "granola", "repo": None, "token": "grn_abc"}
    assert "grn_abc" not in resp.text  # the grn_ key is never echoed back


def test_post_ingest_granola_without_token_falls_back():
    """No grn_ key still triggers (the child falls back to the server's own GRANOLA_API_KEY)."""
    seen = {}

    async def trigger(source, repo, token, workspace=None):
        seen["source"], seen["token"] = source, token
        return {"status": "running", "source": "granola"}

    resp = _client(trigger).post("/v1/ingest", json={"source": "granola"})
    assert resp.status_code == 202
    assert seen == {"source": "granola", "token": None}


def test_post_ingest_granola_already_running_is_409():
    async def trigger(source, repo, token, workspace=None):
        return {"status": "already_running", "source": "granola"}

    resp = _client(trigger).post("/v1/ingest", json={"source": "granola", "token": "grn_x"})
    assert resp.status_code == 409


@pytest.mark.parametrize(
    "bad",
    [
        "grn_ has space",  # internal whitespace
        "grn_\x00null",  # embedded NUL byte
        "#leading-hash",  # what the child's _clean_secret would silently drop
        "grn_" + "x" * 300,  # over the length bound
    ],
)
def test_post_ingest_granola_rejects_malformed_token(bad):
    """A malformed grn_ key is a 400, never a silent run as the server identity."""
    called = False

    async def trigger(source, repo, token, workspace=None):
        nonlocal called
        called = True
        return {"status": "running", "source": "granola"}

    resp = _client(trigger).post("/v1/ingest", json={"source": "granola", "token": bad})
    assert resp.status_code == 400
    assert not called  # the bad key never reaches the ingest trigger
    assert bad not in resp.text


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

    async def trigger(source, repo, token, workspace=None):
        nonlocal called
        called = True
        return {"status": "running", "repo": repo}

    resp = _client(trigger).post("/v1/ingest", json={"repo": "owner/name", "token": bad})
    assert resp.status_code == 400
    assert not called  # the bad token never reaches the ingest trigger
    assert bad not in resp.text


def test_post_ingest_strips_and_accepts_padded_token():
    seen = {}

    async def trigger(source, repo, token, workspace=None):
        seen["token"] = token
        return {"status": "running", "repo": repo}

    resp = _client(trigger).post(
        "/v1/ingest", json={"repo": "owner/name", "token": "  gho_ok123  "}
    )
    assert resp.status_code == 202
    assert seen["token"] == "gho_ok123"


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
    # keep this hermetic: the server-token fallback is covered in test_cli_token_fallback;
    # here we only care that the user token rides in env, not argv. without this the call
    # would read the runner's ambient .env / GITHUB_TOKEN.
    monkeypatch.setattr(cli, "_resolve_server_token", lambda: None)

    cli._trigger_ingest("github", "owner/repo", "gho_secret")
    assert "gho_secret" not in captured["args"]  # never on the command line
    assert captured["env"]["GITHUB_TOKEN"] == "gho_secret"

    # no token: the child inherits the parent env (env=None), no override
    cli._INGEST_PROCS.clear()
    cli._trigger_ingest("github", "owner/repo2", None)
    assert captured["env"] is None


def test_trigger_ingest_granola_keys_by_token_hash(monkeypatch):
    """Granola has no repo, so the in-flight guard keys on the grn_ key (one key ~ one user).

    A second trigger for the same key while the first runs is a 409; a different key runs.
    The grn_ key rides in GRANOLA_API_KEY env, never argv, and the run spawns --source granola.
    """
    from relic import cli

    cli._INGEST_PROCS.clear()
    captured = {}

    class _RunningProc:
        def poll(self):
            return None  # still running

    def _fake_popen(args, env=None):
        captured["args"], captured["env"] = list(args), env
        return _RunningProc()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)

    first = cli._trigger_ingest("granola", None, "grn_userkey")
    assert first == {"status": "running", "source": "granola"}
    assert "--source" in captured["args"] and "granola" in captured["args"]
    assert "--repo" not in captured["args"]
    assert "grn_userkey" not in captured["args"]  # never on the command line
    assert captured["env"]["GRANOLA_API_KEY"] == "grn_userkey"

    # same key while the first is in flight: a conflict, not a duplicate run
    again = cli._trigger_ingest("granola", None, "grn_userkey")
    assert again["status"] == "already_running"

    # a different user's key is a distinct scope, so it runs
    other = cli._trigger_ingest("granola", None, "grn_otherkey")
    assert other["status"] == "running"


def test_trigger_ingest_granola_no_token_inherits_env(monkeypatch):
    """No grn_ key: env=None so the child inherits the server's own GRANOLA_API_KEY."""
    from relic import cli

    cli._INGEST_PROCS.clear()
    captured = {}

    def _fake_popen(args, env=None):
        captured["args"], captured["env"] = list(args), env
        return type("P", (), {"poll": lambda self: None})()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    result = cli._trigger_ingest("granola", None, None)
    assert result == {"status": "running", "source": "granola"}
    assert captured["env"] is None
    assert "--source" in captured["args"] and "granola" in captured["args"]


def test_post_ingest_notion_no_repo_routes_with_token():
    """Notion carries a token but no repo; the trigger is called with source=notion, repo=None."""
    seen = {}

    async def trigger(source, repo, token, workspace=None):
        seen["source"], seen["repo"], seen["token"] = source, repo, token
        return {"status": "running", "source": "notion"}

    resp = _client(trigger).post("/v1/ingest", json={"source": "notion", "token": "ntn_abc"})
    assert resp.status_code == 202
    assert seen == {"source": "notion", "repo": None, "token": "ntn_abc"}
    assert "ntn_abc" not in resp.text  # the token is never echoed back


def test_post_ingest_notion_without_token_falls_back():
    """No token still triggers (the child falls back to the server's own NOTION_API_KEY)."""
    seen = {}

    async def trigger(source, repo, token, workspace=None):
        seen["source"], seen["token"] = source, token
        return {"status": "running", "source": "notion"}

    resp = _client(trigger).post("/v1/ingest", json={"source": "notion"})
    assert resp.status_code == 202
    assert seen == {"source": "notion", "token": None}


def test_post_ingest_notion_already_running_is_409():
    async def trigger(source, repo, token, workspace=None):
        return {"status": "already_running", "source": "notion"}

    resp = _client(trigger).post("/v1/ingest", json={"source": "notion", "token": "ntn_x"})
    assert resp.status_code == 409


def test_trigger_ingest_notion_keys_by_token_hash(monkeypatch):
    """Notion has no repo, so the in-flight guard keys on the token (one token ~ one workspace).

    A second trigger for the same token while the first runs is a 409; a different token runs.
    The token rides in NOTION_API_KEY env, never argv, and the run spawns --source notion.
    """
    from relic import cli

    cli._INGEST_PROCS.clear()
    captured = {}

    class _RunningProc:
        def poll(self):
            return None  # still running

    def _fake_popen(args, env=None):
        captured["args"], captured["env"] = list(args), env
        return _RunningProc()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)

    first = cli._trigger_ingest("notion", None, "ntn_userkey")
    assert first == {"status": "running", "source": "notion"}
    assert "--source" in captured["args"] and "notion" in captured["args"]
    assert "--repo" not in captured["args"]
    assert "ntn_userkey" not in captured["args"]  # never on the command line
    assert captured["env"]["NOTION_API_KEY"] == "ntn_userkey"

    # same token while the first is in flight: a conflict, not a duplicate run
    again = cli._trigger_ingest("notion", None, "ntn_userkey")
    assert again["status"] == "already_running"

    # a different token is a distinct scope, so it runs
    other = cli._trigger_ingest("notion", None, "ntn_otherkey")
    assert other["status"] == "running"


def test_trigger_ingest_notion_no_token_inherits_env(monkeypatch):
    """No token: env=None so the child inherits the server's own NOTION_API_KEY."""
    from relic import cli

    cli._INGEST_PROCS.clear()
    captured = {}

    def _fake_popen(args, env=None):
        captured["args"], captured["env"] = list(args), env
        return type("P", (), {"poll": lambda self: None})()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    result = cli._trigger_ingest("notion", None, None)
    assert result == {"status": "running", "source": "notion"}
    assert captured["env"] is None
    assert "--source" in captured["args"] and "notion" in captured["args"]


# --- the workspace field: the engram workspace a web-triggered run writes into ----


def test_post_ingest_passes_workspace_to_trigger():
    seen = {}

    async def trigger(source, repo, token, workspace=None):
        seen["workspace"] = workspace
        return {"status": "running", "repo": repo}

    resp = _client(trigger).post(
        "/v1/ingest", json={"repo": "owner/name", "workspace": "org_abc123"}
    )
    assert resp.status_code == 202
    assert seen["workspace"] == "org_abc123"


def test_post_ingest_without_workspace_is_none():
    seen = {}

    async def trigger(source, repo, token, workspace=None):
        seen["workspace"] = workspace
        return {"status": "running", "repo": repo}

    resp = _client(trigger).post("/v1/ingest", json={"repo": "owner/name"})
    assert resp.status_code == 202
    assert seen["workspace"] is None


def test_post_ingest_malformed_workspace_is_400_and_never_reaches_trigger():
    async def trigger(source, repo, token, workspace=None):
        raise AssertionError("a malformed workspace must not reach the trigger")

    client = _client(trigger)
    for bad in ("has space", "#comment", "x" * 200):
        resp = client.post("/v1/ingest", json={"repo": "owner/name", "workspace": bad})
        assert resp.status_code == 400, bad
        assert resp.json() == {"error": "invalid workspace"}
