"""End-to-end closed-loop smoke: ingest -> daemon inject -> daemon capture -> readback.

Proves the whole loop turns against a real Postgres:

1. spool two contract-true fixture episodes (a PR and an issue) for a throwaway repo
   and land them with the real ``relic load`` CLI (the store half of ``relic ingest``,
   minus the GitHub fetch -- there is no offline fetch path, and a live pull would make
   the smoke flake on network and rate limits)
2. start ``relic daemon`` on a free port, scoped to the throwaway repo
3. POST /v1/daemon/inject with a real query and a cwd whose git origin resolves to the
   fixture repo, and assert a cited recall comes back
4. POST /v1/daemon/capture with a small session transcript, and assert the episode
   lands: 202 + the ``captures`` status counter + a memories row
5. edit-reingest: respool the PR with an edited body and re-run ``relic load`` in a
   fresh process, and assert the row refreshed in place, not forked (the upsert on
   (workspace_id, name) is the supersession mechanism now, ADR-0007)
6. tear down: kill the daemon and delete only the throwaway workspace (the FK
   cascade removes every row it owned)

Everything runs in the throwaway workspace ``loop-smoke-<run id>`` -- the Postgres
may be shared, so teardown deletes that workspace only. Run it via ``just
loop-smoke``; it is marked ``e2e`` and skips itself without a reachable Postgres
(``docker compose up -d postgres``). No API keys needed: the load is a
deterministic write now.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from relic.config import get_settings

_DSN = os.environ.get("DATABASE_URL", "postgresql://relic:relic@localhost:5432/relic")


def _postgres_up() -> bool:
    from relic.engram import postgres_reachable

    return postgres_reachable(_DSN)


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not _postgres_up(),
        reason="needs a running Postgres (docker compose up -d postgres)",
    ),
]

_RELIC = Path(sys.executable).parent / "relic"

# No LLM anywhere in the loop: these are generous bounds for process spawns only.
_LOAD_TIMEOUT_S = 120
_DAEMON_BOOT_TIMEOUT_S = 60
_CAPTURE_TIMEOUT_S = 60


def _fixture_episode_bodies(
    repo_full_name: str,
    pr_title: str = "add token bucket rate limiter to the webhook receiver",
) -> list[tuple[str, str, str, datetime]]:
    """(name, body, source_description, reference_time) for the fixture PR + issue.

    Bodies are built through the episode-body contract models, so the smoke feeds the
    loader exactly what the GitHub mappers produce (mappers.pr_to_episode's output
    shape) without a live fetch.
    """
    from relic.contracts.episode_body import (
        FileEntry,
        IssueEpisodeBody,
        IssueSection,
        PersonRef,
        PrEpisodeBody,
        PullRequestSection,
        RepoRef,
        ReviewEntry,
    )

    repo_url = f"https://github.com/{repo_full_name}"
    pr_body = PrEpisodeBody(
        context="merged PR, 1 review, 1 file",
        repo=RepoRef(full_name=repo_full_name, url=repo_url, default_branch="main"),
        pull_request=PullRequestSection(
            number=1,
            title=pr_title,
            description="Protects the webhook receiver from burst traffic.",
            url=f"{repo_url}/pull/1",
            state="merged",
            created_at="2025-01-01T00:00:00Z",
            merged_at="2025-01-02T00:00:00Z",
            author=PersonRef(login="alice", profile_url="https://github.com/alice"),
        ),
        reviews=[
            ReviewEntry(
                reviewer=PersonRef(login="bob", profile_url="https://github.com/bob"),
                state="APPROVED",
                comment="rate limiter approach looks right",
            )
        ],
        files=[FileEntry(path="src/webhook/receiver.py", additions=40, deletions=2)],
    )
    issue_body = IssueEpisodeBody(
        context="closed issue",
        issue=IssueSection(
            identifier=f"{repo_full_name}#2",
            title="webhook receiver falls over under burst traffic",
            description="Bursts of webhook deliveries overload the receiver.",
            state="closed",
            url=f"{repo_url}/issues/2",
            assignees=["alice"],
            created_at="2024-12-20T00:00:00Z",
            closed_at="2025-01-02T00:00:00Z",
        ),
    )
    return [
        (
            f"PR {repo_full_name}#1",
            pr_body.model_dump_json(),
            "github pull request",
            datetime(2025, 1, 2, tzinfo=UTC),
        ),
        (
            f"Issue {repo_full_name}#2",
            issue_body.model_dump_json(),
            "github issue",
            datetime(2025, 1, 2, tzinfo=UTC),
        ),
    ]


def _subprocess_env(workspace: str) -> dict[str, str]:
    """Env for the relic subprocesses: explicit settings, no .env in their cwd.

    The subprocesses run in a temp cwd (so ``data/`` spool/checkpoints/registry stay
    isolated), which means pydantic-settings finds no ``.env`` there -- everything they
    need must arrive as env vars. RELIC_WORKSPACE pins every write to this run's
    throwaway workspace.
    """
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": _DSN,
            "RELIC_WORKSPACE": workspace,
        }
    )
    # The daemon must scope by --repo/cwd alone, not by a developer's TARGET_REPO,
    # and documents must stay local-only (no Firestore mirror from a smoke run).
    unwanted = ("TARGET_REPO", "FIREBASE_PROJECT_ID",
                "FIREBASE_CLIENT_EMAIL", "FIREBASE_PRIVATE_KEY")
    for var in unwanted:
        env.pop(var, None)
    return env


def _http_json(
    method: str, url: str, payload: dict | None = None, timeout: float = 30
) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - loopback only
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _git_checkout_for(repo_full_name: str, path: Path) -> Path:
    """A working dir whose git origin resolves to ``repo_full_name``.

    This is what makes inject/capture exercise the daemon's per-session cwd scoping
    (_scope_for_cwd), the same path the Claude Code hooks hit.
    """
    path.mkdir(parents=True)
    for argv in (
        ["git", "init", "-q"],
        ["git", "remote", "add", "origin", f"https://github.com/{repo_full_name}.git"],
    ):
        subprocess.run(argv, cwd=path, check=True, capture_output=True)
    return path


def _tail(path: Path, lines: int = 30) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return "<no output>"


def _wait_for(condition, timeout_s: float, interval_s: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval_s)
    return False


def _specs_for(repo: str, slug: str, **body_overrides) -> list:
    from relic.contracts import EpisodeSpec

    return [
        EpisodeSpec(
            name=name,
            body=body,
            source_description=desc,
            reference_time=ref,
            group_id=slug,
        )
        for name, body, desc, ref in _fixture_episode_bodies(repo, **body_overrides)
    ]


def _relic_load(repo: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Run the real ``relic load`` CLI in a fresh process, as a user (or cron) would."""
    result = subprocess.run(
        [str(_RELIC), "load", "--repo", repo, "--no-progress"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=_LOAD_TIMEOUT_S,
    )
    assert result.returncode == 0, f"relic load failed:\n{result.stderr[-3000:]}"
    return result


async def test_closed_loop_smoke(tmp_path: Path) -> None:
    import asyncpg

    from relic.ingest import repo_group_id, spool_episodes

    get_settings()  # settings import sanity; the subprocesses carry their own env
    run_id = uuid.uuid4().hex[:8]
    workspace = f"loop-smoke-{run_id}"
    repo = f"loop-smoke/fixture-{run_id}"
    slug = repo_group_id(repo)
    env = _subprocess_env(workspace)
    daemon_log = tmp_path / "daemon.log"
    checkout = _git_checkout_for(repo, tmp_path / "checkout")

    # -- 1. ingest: spool contract-true fixture episodes, land them with the real CLI --
    specs = _specs_for(repo, slug)
    spool_episodes(specs, slug, base=tmp_path / "data" / "spool")
    _relic_load(repo, tmp_path, env)

    daemon: subprocess.Popen | None = None
    pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=2)
    try:
        rows = await pool.fetch(
            "SELECT name FROM memories WHERE workspace_id = $1 AND scope = $2",
            workspace,
            slug,
        )
        assert {row["name"] for row in rows} == {spec.name for spec in specs}, (
            f"ingested episodes missing from workspace {workspace}: {rows}"
        )

        # -- 2. daemon on a free port, scoped to the throwaway repo --
        port = _free_port()
        base = f"http://127.0.0.1:{port}"
        with daemon_log.open("wb") as sink:
            daemon = subprocess.Popen(
                [str(_RELIC), "daemon", "--repo", repo, "--port", str(port)],
                cwd=tmp_path,
                env=env,
                stdout=sink,
                stderr=subprocess.STDOUT,
            )

        def _daemon_up() -> bool:
            if daemon.poll() is not None:
                pytest.fail(f"daemon exited during boot:\n{_tail(daemon_log)}")
            try:
                status, body = _http_json("GET", f"{base}/v1/daemon/status", timeout=5)
                return status == 200 and body.get("status") == "ok"
            except OSError:
                return False

        assert _wait_for(_daemon_up, _DAEMON_BOOT_TIMEOUT_S), (
            f"daemon never came up on {base}:\n{_tail(daemon_log)}"
        )

        # -- 3. inject: real query + cwd -> a cited recall --
        status, body = _http_json(
            "POST",
            f"{base}/v1/daemon/inject",
            {
                "prompt": "who worked on rate limiting the webhook receiver?",
                "num_results": 5,
                "cwd": str(checkout),
            },
            timeout=30,
        )
        assert status == 200
        context = body.get("context", "")
        assert context and "No memory found" not in context, (
            f"inject returned no recall from workspace {workspace}; daemon log:\n"
            f"{_tail(daemon_log)}"
        )
        assert "source:" in context, f"recall came back uncited:\n{context}"
        assert f"https://github.com/{repo}" in context, (
            f"citation should anchor to the fixture repo's URL:\n{context}"
        )
        status, counters = _http_json("GET", f"{base}/v1/daemon/status")
        assert counters["injects"] == 1 and counters["inject_errors"] == 0, counters

        # -- 4. capture: session transcript -> 202 -> counters -> row readback --
        session_id = f"loop-smoke-{run_id}"
        transcript = "\n".join(
            json.dumps(turn)
            for turn in [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": "tighten the webhook rate limiter burst window",
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "text",
                                "text": "Lowered the token bucket burst window to 30 seconds"
                                " in src/webhook/receiver.py and updated the tests.",
                            }
                        ],
                    },
                },
            ]
        )
        status, body = _http_json(
            "POST",
            f"{base}/v1/daemon/capture",
            {"session_id": session_id, "transcript": transcript, "cwd": str(checkout)},
            timeout=30,
        )
        assert status == 202 and body == {"status": "accepted", "session_id": session_id}

        # Write-back runs as a background task; poll the loop counters.
        def _captured() -> bool:
            _, counters = _http_json("GET", f"{base}/v1/daemon/status")
            if counters["capture_errors"]:
                pytest.fail(f"capture errored; daemon log:\n{_tail(daemon_log, 60)}")
            return counters["captures"] == 1

        assert _wait_for(_captured, _CAPTURE_TIMEOUT_S), (
            f"capture never landed; daemon log:\n{_tail(daemon_log, 60)}"
        )

        rows = await pool.fetch(
            "SELECT search_text, artifact_type FROM memories"
            " WHERE workspace_id = $1 AND name = $2",
            workspace,
            f"AgentSession {session_id}",
        )
        assert len(rows) == 1, f"captured episode missing from workspace {workspace}: {rows}"
        assert rows[0]["artifact_type"] == "agent_session"
        assert "burst window" in (rows[0]["search_text"] or ""), (
            "captured episode lost the session transcript"
        )

        # -- 5. edit-reingest: an edited body refreshes in place, never forks --
        # A fresh `relic load` process upserts on (workspace_id, name), so a changed
        # body replaces its row; a second row for the same name would be a fork.
        edited_title = "swap the webhook rate limiter to a sliding window"
        edited_pr = _specs_for(repo, slug, pr_title=edited_title)[0]
        spool_episodes([edited_pr], slug, base=tmp_path / "data" / "spool")
        _relic_load(repo, tmp_path, env)
        rows = await pool.fetch(
            "SELECT title FROM memories WHERE workspace_id = $1 AND name = $2",
            workspace,
            edited_pr.name,
        )
        assert len(rows) == 1, (
            f"edited episode forked instead of refreshing: {len(rows)} copies of "
            f"{edited_pr.name} in workspace {workspace}"
        )
        assert rows[0]["title"] == edited_title, "refreshed episode kept the stale body"
    finally:
        if daemon is not None:
            daemon.terminate()
            try:
                daemon.wait(timeout=15)
            except subprocess.TimeoutExpired:
                daemon.kill()
        # Delete ONLY this run's throwaway workspace (the Postgres may be shared);
        # the FK cascade removes its memories, people, and join rows.
        await pool.execute("DELETE FROM workspaces WHERE id = $1", workspace)
        await pool.close()
