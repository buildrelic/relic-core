"""End-to-end closed-loop smoke: ingest -> daemon inject -> daemon capture -> readback.

Proves the whole loop turns against a real FalkorDB with real OpenAI extraction:

1. spool two contract-true fixture episodes (a PR and an issue) for a throwaway repo
   and extract them with the real ``relic load`` CLI (the LLM half of ``relic ingest``,
   minus the GitHub fetch -- there is no offline fetch path, and a live pull would make
   the smoke flake on network and rate limits)
2. start ``relic daemon`` on a free port, scoped to the throwaway repo
3. POST /v1/daemon/inject with a real query and a cwd whose git origin resolves to the
   fixture repo, and assert a cited recall comes back
4. POST /v1/daemon/capture with a small session transcript, and assert the episode
   lands: 202 + the ``captures`` status counter + an Episodic node in the graph
5. edit-reingest: respool the PR with an edited body and re-run ``relic load`` in a
   fresh process, and assert the episode was superseded in place, not forked (REL-118;
   regression for the loader opening the default graph instead of the group's own)
6. tear down: kill the daemon and delete only the throwaway graph key

Everything runs in the throwaway per-repo graph ``loop-smoke__fixture-<run id>`` -- the
FalkorDB instance may be shared, so teardown deletes that graph key only. Run it via
``just loop-smoke``; it is marked ``e2e`` and skips itself without OPENAI_API_KEY or a
reachable FalkorDB (``docker compose up -d falkordb``).
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


def _falkordb_reachable() -> bool:
    settings = get_settings()
    try:
        with socket.create_connection((settings.falkordb_host, settings.falkordb_port), timeout=1):
            return True
    except OSError:
        return False


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.environ.get("OPENAI_API_KEY"),
        reason="needs OPENAI_API_KEY for live Graphiti extraction",
    ),
    pytest.mark.skipif(
        not _falkordb_reachable(),
        reason="needs a running FalkorDB (docker compose up -d falkordb)",
    ),
]

_RELIC = Path(sys.executable).parent / "relic"

# The loop turns through live LLM extraction; each episode takes tens of seconds.
_LOAD_TIMEOUT_S = 600
_DAEMON_BOOT_TIMEOUT_S = 60
_CAPTURE_TIMEOUT_S = 300


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


def _subprocess_env(settings) -> dict[str, str]:
    """Env for the relic subprocesses: explicit settings, no .env in their cwd.

    The subprocesses run in a temp cwd (so ``data/`` spool/checkpoints/registry stay
    isolated), which means pydantic-settings finds no ``.env`` there -- everything they
    need must arrive as env vars. Pin the provider to openai: OPENAI_API_KEY is the
    smoke's gate, and the embedder/reranker are OpenAI under every provider anyway.
    """
    env = os.environ.copy()
    env.update(
        {
            "OPENAI_API_KEY": settings.openai_api_key or "",
            "GRAPHITI_LLM_PROVIDER": "openai",
            "FALKORDB_HOST": settings.falkordb_host,
            "FALKORDB_PORT": str(settings.falkordb_port),
        }
    )
    if settings.falkordb_password:
        env["FALKORDB_PASSWORD"] = settings.falkordb_password
    # The daemon must scope by --repo/cwd alone, not by a developer's TARGET_REPO.
    env.pop("TARGET_REPO", None)
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


def _wait_for(condition, timeout_s: float, interval_s: float = 1.0) -> bool:
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


async def _drop_graph(settings, slug: str) -> None:
    """Delete ONLY this run's throwaway graph key (the FalkorDB may be shared).

    Best-effort: an early failure may mean the graph was never created, and a teardown
    raise must not mask the assertion that got us here.
    """
    from contextlib import suppress

    from falkordb.asyncio import FalkorDB

    assert slug.startswith("loop-smoke__"), f"refusing to drop non-throwaway graph {slug}"
    client = FalkorDB(
        host=settings.falkordb_host,
        port=settings.falkordb_port,
        password=settings.falkordb_password,
    )
    try:
        with suppress(Exception):
            await client.select_graph(slug).delete()
    finally:
        await client.aclose()


async def test_closed_loop_smoke(tmp_path: Path) -> None:
    from relic.graph import open_memory
    from relic.ingest import repo_group_id, spool_episodes

    settings = get_settings()
    run_id = uuid.uuid4().hex[:8]
    repo = f"loop-smoke/fixture-{run_id}"
    slug = repo_group_id(repo)
    env = _subprocess_env(settings)
    daemon_log = tmp_path / "daemon.log"
    checkout = _git_checkout_for(repo, tmp_path / "checkout")

    # -- 1. ingest: spool contract-true fixture episodes, extract with the real CLI --
    specs = _specs_for(repo, slug)
    spool_episodes(specs, slug, base=tmp_path / "data" / "spool")
    _relic_load(repo, tmp_path, env)

    daemon: subprocess.Popen | None = None
    engram = None
    try:
        engram = open_memory(database=slug, api_key=settings.openai_api_key)
        episodic = await engram.execute_read(
            "MATCH (e:Episodic {group_id: $g}) RETURN e.name AS name", g=slug
        )
        assert {row["name"] for row in episodic} == {spec.name for spec in specs}, (
            f"ingested episodes missing from graph {slug}: {episodic}"
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
            timeout=120,
        )
        assert status == 200
        context = body.get("context", "")
        assert context and "No memory found" not in context, (
            f"inject returned no recall from graph {slug}; daemon log:\n{_tail(daemon_log)}"
        )
        assert "source:" in context, f"recall came back uncited:\n{context}"
        assert f"https://github.com/{repo}" in context, (
            f"citation should anchor to the fixture repo's URL:\n{context}"
        )
        status, counters = _http_json("GET", f"{base}/v1/daemon/status")
        assert counters["injects"] == 1 and counters["inject_errors"] == 0, counters

        # -- 4. capture: session transcript -> 202 -> counters -> graph readback --
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

        # Write-back runs as a background task (LLM extraction); poll the loop counters.
        def _captured() -> bool:
            _, counters = _http_json("GET", f"{base}/v1/daemon/status")
            if counters["capture_errors"]:
                pytest.fail(f"capture errored; daemon log:\n{_tail(daemon_log, 60)}")
            return counters["captures"] == 1

        assert _wait_for(_captured, _CAPTURE_TIMEOUT_S), (
            f"capture never landed; daemon log:\n{_tail(daemon_log, 60)}"
        )

        rows = await engram.execute_read(
            "MATCH (e:Episodic {name: $name, group_id: $g}) RETURN e.content AS content",
            name=f"AgentSession {session_id}",
            g=slug,
        )
        assert len(rows) == 1, f"captured episode missing from graph {slug}: {rows}"
        assert "burst window" in (rows[0]["content"] or ""), (
            "captured episode lost the session transcript"
        )

        # -- 5. edit-reingest: an edited body supersedes in place, never forks --
        # A fresh `relic load` process supersedes before its first add, so this is the
        # regression for the loader opening the default graph instead of the group's own
        # (the removal would match nothing there and the re-add would fork a duplicate).
        edited_title = "swap the webhook rate limiter to a sliding window"
        edited_pr = _specs_for(repo, slug, pr_title=edited_title)[0]
        spool_episodes([edited_pr], slug, base=tmp_path / "data" / "spool")
        _relic_load(repo, tmp_path, env)
        rows = await engram.execute_read(
            "MATCH (e:Episodic {name: $name, group_id: $g}) RETURN e.content AS content",
            name=edited_pr.name,
            g=slug,
        )
        assert len(rows) == 1, (
            f"edited episode forked instead of superseding: {len(rows)} copies of "
            f"{edited_pr.name} in graph {slug}"
        )
        assert edited_title in (rows[0]["content"] or ""), "superseded episode kept the stale body"
    finally:
        if daemon is not None:
            daemon.terminate()
            try:
                daemon.wait(timeout=15)
            except subprocess.TimeoutExpired:
                daemon.kill()
        if engram is not None:
            await engram.close()
        await _drop_graph(settings, slug)
