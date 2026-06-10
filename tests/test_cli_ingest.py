"""CLI orchestration for `relic ingest`: gates, exit codes, and checkpoint wiring.

These exercise _ingest without touching GitHub, OpenAI, or FalkorDB: the fetch,
the engram, and the load are stubbed so the test pins the orchestration (preflight
gate, exit codes, --fresh). The load loop itself is covered in test_load.py.
"""

import pytest
from typer.testing import CliRunner

from relic.cli import app
from relic.graph.load import LoadStats
from relic.ingest.mappers import PullRequestRec, RepoBundle

runner = CliRunner()


def test_repo_without_slash_exits_2() -> None:
    result = runner.invoke(app, ["ingest", "--repo", "noslash"])
    assert result.exit_code == 2


def test_unreachable_graph_exits_1_before_fetching(monkeypatch: pytest.MonkeyPatch) -> None:
    # Preflight should stop the run before any GitHub call when the graph is down.
    monkeypatch.setattr("relic.graph.engram.falkordb_reachable", lambda *a, **k: False)

    def _fail_token(*_a, **_k):
        raise AssertionError("token resolution must not run when the graph is unreachable")

    monkeypatch.setattr("relic.ingest.github.resolve_github_token", _fail_token)

    result = runner.invoke(app, ["ingest", "--repo", "astral-sh/uv"])
    assert result.exit_code == 1


class _FakeGH:
    async def __aenter__(self) -> "_FakeGH":
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False


class _FakeEngram:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _bundle() -> RepoBundle:
    pr = PullRequestRec(
        number=1,
        title="add thing",
        url="https://github.com/demo/repo/pull/1",
        state="merged",
        author_login="alice",
        author_url="https://github.com/alice",
        created_at="2025-01-01T00:00:00Z",
        merged_at="2025-01-02T00:00:00Z",
    )
    return RepoBundle(
        full_name="demo/repo",
        url="https://github.com/demo/repo",
        default_branch="main",
        pull_requests=[pr],
        issues=[],
    )


def _patch_happy_path(monkeypatch: pytest.MonkeyPatch, tmp_path, *, stats: LoadStats) -> None:
    monkeypatch.chdir(tmp_path)  # data/raw and data/ingest land under tmp
    monkeypatch.setattr("relic.graph.engram.falkordb_reachable", lambda *a, **k: True)
    monkeypatch.setattr("relic.ingest.github.resolve_github_token", lambda *a, **k: "tok")
    monkeypatch.setattr("relic.ingest.github.make_github", lambda *a, **k: _FakeGH())
    monkeypatch.setattr("relic.graph.engram.make_engram", lambda *a, **k: _FakeEngram())

    async def _fetch_repo(*_a, **_k):
        return _bundle()

    async def _load(*_a, **_k):
        return stats

    monkeypatch.setattr("relic.ingest.github.fetch_repo", _fetch_repo)
    monkeypatch.setattr("relic.graph.load.load_episodes", _load)


def test_exit_1_when_every_episode_fails(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    stats = LoadStats(
        group_id="demo__repo",
        attempted=1,
        loaded=0,
        failed=1,
        failures=[("PR demo/repo#1", "RuntimeError: boom")],
        duration_s=0.1,
    )
    _patch_happy_path(monkeypatch, tmp_path, stats=stats)
    result = runner.invoke(app, ["ingest", "--repo", "demo/repo"])
    assert result.exit_code == 1


def test_exit_0_on_successful_load(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    stats = LoadStats(group_id="demo__repo", attempted=1, loaded=1, failed=0, duration_s=0.1)
    _patch_happy_path(monkeypatch, tmp_path, stats=stats)
    result = runner.invoke(app, ["ingest", "--repo", "demo/repo"])
    assert result.exit_code == 0


def _patch_loaders(monkeypatch: pytest.MonkeyPatch, stats: LoadStats) -> list[str]:
    """Patch both loaders to record which one _ingest routes to. Returns the call log."""
    called: list[str] = []

    async def _bulk(*_a, **_k):
        called.append("bulk")
        return stats

    async def _seq(*_a, **_k):
        called.append("seq")
        return stats

    monkeypatch.setattr("relic.graph.load.load_episodes_bulk", _bulk)
    monkeypatch.setattr("relic.graph.load.load_episodes", _seq)
    return called


def test_bulk_flag_routes_to_bulk_loader(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    stats = LoadStats(group_id="demo__repo", attempted=1, loaded=1, failed=0, duration_s=0.1)
    _patch_happy_path(monkeypatch, tmp_path, stats=stats)
    called = _patch_loaders(monkeypatch, stats)

    result = runner.invoke(app, ["ingest", "--repo", "demo/repo", "--bulk"])
    assert result.exit_code == 0
    assert called == ["bulk"]


def test_default_routes_to_sequential_loader(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    stats = LoadStats(group_id="demo__repo", attempted=1, loaded=1, failed=0, duration_s=0.1)
    _patch_happy_path(monkeypatch, tmp_path, stats=stats)
    called = _patch_loaders(monkeypatch, stats)

    result = runner.invoke(app, ["ingest", "--repo", "demo/repo"])
    assert result.exit_code == 0
    assert called == ["seq"]


def test_fresh_clears_the_checkpoint(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    stats = LoadStats(group_id="demo__repo", attempted=1, loaded=1, failed=0, duration_s=0.1)
    _patch_happy_path(monkeypatch, tmp_path, stats=stats)
    cleared: list = []
    monkeypatch.setattr("relic.ingest.checkpoint.clear", lambda path: cleared.append(path))

    result = runner.invoke(app, ["ingest", "--repo", "demo/repo", "--fresh"])
    assert result.exit_code == 0
    assert len(cleared) == 1  # --fresh wired the checkpoint clear
