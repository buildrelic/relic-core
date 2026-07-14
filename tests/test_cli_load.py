"""CLI orchestration for `relic load` and `relic ingest --no-load`: the capture/load split.

`relic load` runs only the store-write half, reading what a prior capture spooled.
`relic ingest --no-load` runs only the fast half and touches no store. These pin
that split without GitHub or Postgres: the spool is seeded on disk, the store and
loader are stubbed, so the tests cover routing, the --limit slice, the empty-spool
guard, and that --no-load never reaches the store. The load loop itself is covered
in test_load.py.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from relic.cli import app
from relic.contracts import EpisodeSpec
from relic.engram.load import LoadStats
from relic.ingest import repo_group_id, spool_episodes

runner = CliRunner()


class _FakeGH:
    async def __aenter__(self) -> "_FakeGH":
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False


class _FakeStore:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _spec(name: str, *, day: int = 1) -> EpisodeSpec:
    return EpisodeSpec(
        name=name,
        body='{"source_type": "pull_request", "url": "https://example/x"}',
        source_description="GitHub PR",
        reference_time=datetime(2025, 1, day, tzinfo=UTC),
        group_id="demo__repo",
    )


def _patch_store(monkeypatch: pytest.MonkeyPatch, *, stats: LoadStats) -> None:
    monkeypatch.setattr("relic.engram.postgres_reachable", lambda *a, **k: True)

    async def _open_engram(*_a, **_k):
        return _FakeStore()

    monkeypatch.setattr("relic.engram.open_engram", _open_engram)

    async def _load(*_a, **_k):
        return stats

    monkeypatch.setattr("relic.engram.load_episodes", _load)


def test_repo_without_slash_exits_2() -> None:
    result = runner.invoke(app, ["load", "--repo", "noslash"])
    assert result.exit_code == 2


def test_unreachable_store_exits_1_before_reading_spool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("relic.engram.postgres_reachable", lambda *a, **k: False)

    def _fail_read(*_a, **_k):
        raise AssertionError("spool must not be read when the store is unreachable")

    monkeypatch.setattr("relic.ingest.read_spool", _fail_read)

    result = runner.invoke(app, ["load", "--repo", "demo/repo"])
    assert result.exit_code == 1


def test_empty_spool_warns_and_exits_0(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # no spool written
    monkeypatch.setattr("relic.engram.postgres_reachable", lambda *a, **k: True)

    def _no_store(*_a, **_k):
        raise AssertionError("no store should be built when the spool is empty")

    monkeypatch.setattr("relic.engram.open_engram", _no_store)

    result = runner.invoke(app, ["load", "--repo", "demo/repo"])
    assert result.exit_code == 0  # empty spool is a no-op, not a failure


def test_load_lands_spooled_episodes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    spool_episodes([_spec("PR demo/repo#1")], repo_group_id("demo/repo"))
    stats = LoadStats(group_id="demo__repo", attempted=1, loaded=1, failed=0, duration_s=0.1)
    _patch_store(monkeypatch, stats=stats)

    result = runner.invoke(app, ["load", "--repo", "demo/repo"])
    assert result.exit_code == 0


def test_load_limit_slices_to_n_pending(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    spool_episodes(
        [
            _spec("PR demo/repo#1", day=1),
            _spec("PR demo/repo#2", day=2),
            _spec("PR demo/repo#3", day=3),
        ],
        repo_group_id("demo/repo"),
    )
    stats = LoadStats(group_id="demo__repo", attempted=1, loaded=1, failed=0, duration_s=0.1)
    monkeypatch.setattr("relic.engram.postgres_reachable", lambda *a, **k: True)

    async def _open_engram(*_a, **_k):
        return _FakeStore()

    monkeypatch.setattr("relic.engram.open_engram", _open_engram)
    seen: list[int] = []

    async def _load(_engram, episodes, **_k):
        seen.append(len(episodes))
        return stats

    monkeypatch.setattr("relic.engram.load_episodes", _load)

    result = runner.invoke(app, ["load", "--repo", "demo/repo", "--limit", "1"])
    assert result.exit_code == 0
    assert seen == [1]  # only one of the three pending episodes handed to the loader


def test_load_fresh_clears_the_checkpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    spool_episodes([_spec("PR demo/repo#1")], repo_group_id("demo/repo"))
    stats = LoadStats(group_id="demo__repo", attempted=1, loaded=1, failed=0, duration_s=0.1)
    _patch_store(monkeypatch, stats=stats)
    cleared: list = []
    monkeypatch.setattr("relic.ingest.clear", lambda path: cleared.append(path))

    result = runner.invoke(app, ["load", "--repo", "demo/repo", "--fresh"])
    assert result.exit_code == 0
    assert len(cleared) == 1


def _bundle():
    from relic.ingest.mappers import PullRequestRec, RepoBundle

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


def _pr(number: int, merged_month: int):
    from relic.ingest.mappers import PullRequestRec

    return PullRequestRec(
        number=number,
        title=f"pr {number}",
        url=f"https://github.com/demo/repo/pull/{number}",
        state="merged",
        author_login="alice",
        author_url="https://github.com/alice",
        created_at=f"2025-{merged_month:02d}-01T00:00:00Z",
        merged_at=f"2025-{merged_month:02d}-02T00:00:00Z",
    )


def test_combined_and_split_feed_loader_the_same_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The loader preserves feed order, so combined `ingest` and split `ingest --no-load`
    # + `load` must hand it the same sequence or the runs diverge. The fetch order here
    # (PR#1 newest, then #2 oldest, then #3) is deliberately not chronological.
    from relic.ingest.mappers import RepoBundle

    monkeypatch.chdir(tmp_path)
    bundle = RepoBundle(
        full_name="demo/repo",
        url="https://github.com/demo/repo",
        default_branch="main",
        pull_requests=[_pr(1, 3), _pr(2, 1), _pr(3, 2)],
        issues=[],
    )
    monkeypatch.setattr("relic.ingest.resolve_github_token", lambda *a, **k: "tok")
    monkeypatch.setattr("relic.ingest.make_github", lambda *a, **k: _FakeGH())

    async def _fetch_repo(*_a, **_k):
        return bundle

    monkeypatch.setattr("relic.ingest.fetch_repo", _fetch_repo)
    monkeypatch.setattr("relic.engram.postgres_reachable", lambda *a, **k: True)

    async def _open_engram(*_a, **_k):
        return _FakeStore()

    monkeypatch.setattr("relic.engram.open_engram", _open_engram)

    stats = LoadStats(group_id="demo__repo", attempted=3, loaded=3, failed=0, duration_s=0.1)
    runs: list[list[str]] = []

    async def _load(_engram, episodes, **_k):
        # The stub does not fire on_loaded, so the checkpoint stays empty and the split
        # `load` re-lands the full set from the spool.
        runs.append([spec.name for spec in episodes])
        return stats

    monkeypatch.setattr("relic.engram.load_episodes", _load)

    assert runner.invoke(app, ["ingest", "--repo", "demo/repo"]).exit_code == 0
    assert runner.invoke(app, ["load", "--repo", "demo/repo"]).exit_code == 0

    combined_order, split_order = runs
    assert combined_order == split_order
    # And the shared order is chronological (oldest merge first), the loader's intent.
    assert combined_order == ["PR demo/repo#2", "PR demo/repo#3", "PR demo/repo#1"]


def test_ingest_no_load_captures_without_touching_the_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The whole point of --no-load: fetch + map + spool, and never reach Postgres or
    # the loader. The spool is left for a later `relic load`.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("relic.ingest.resolve_github_token", lambda *a, **k: "tok")
    monkeypatch.setattr("relic.ingest.make_github", lambda *a, **k: _FakeGH())

    async def _fetch_repo(*_a, **_k):
        return _bundle()

    monkeypatch.setattr("relic.ingest.fetch_repo", _fetch_repo)

    def _no_probe(*_a, **_k):
        raise AssertionError("--no-load must not probe Postgres")

    def _no_store(*_a, **_k):
        raise AssertionError("--no-load must not build a store")

    monkeypatch.setattr("relic.engram.postgres_reachable", _no_probe)
    monkeypatch.setattr("relic.engram.open_engram", _no_store)

    result = runner.invoke(app, ["ingest", "--repo", "demo/repo", "--no-load"])
    assert result.exit_code == 0

    # The episode is now on the spool, ready to load.
    from relic.ingest import spool_count

    assert spool_count(repo_group_id("demo/repo")) == 1
