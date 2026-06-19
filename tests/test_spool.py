"""Episode spool: the durable queue that decouples raw capture from LLM extraction.

These pin the spool as a pure disk artifact (no graph, no network): an episode
written and read back is byte-faithful, a re-capture upserts by name instead of
duplicating, and the read order is stable and chronological so the sequential
loader sees an entity before the episode that references it.
"""

from datetime import UTC, datetime
from pathlib import Path

from relic.contracts import EpisodeSpec
from relic.ingest import (
    clear_spool,
    read_spool,
    sort_episodes,
    spool_count,
    spool_dir,
    spool_episodes,
)
from relic.ingest.spool import spool_episode


def _spec(name: str, *, group_id: str = "demo__repo", ref: datetime | None = None) -> EpisodeSpec:
    return EpisodeSpec(
        name=name,
        body='{"source_type": "pull_request", "url": "https://example/1"}',
        source_description="GitHub PR",
        reference_time=ref or datetime(2025, 1, 1, tzinfo=UTC),
        group_id=group_id,
    )


def test_round_trips_an_episode_faithfully(tmp_path: Path) -> None:
    spec = _spec("PR demo/repo#42", ref=datetime(2025, 3, 4, 5, 6, tzinfo=UTC))
    spool_episode(spec, "demo__repo", base=tmp_path)

    [back] = read_spool("demo__repo", base=tmp_path)
    assert back.name == spec.name
    assert back.body == spec.body
    assert back.source_description == spec.source_description
    assert back.reference_time == spec.reference_time
    assert back.group_id == spec.group_id
    assert back.schema_version == spec.schema_version


def test_recapture_upserts_by_name_not_appends(tmp_path: Path) -> None:
    # The same PR captured twice must leave one file, with the latest body.
    spool_episode(_spec("PR demo/repo#1"), "demo__repo", base=tmp_path)
    updated = EpisodeSpec(
        name="PR demo/repo#1",
        body='{"source_type": "pull_request", "url": "https://example/1", "v": 2}',
        source_description="GitHub PR",
        reference_time=datetime(2025, 1, 1, tzinfo=UTC),
        group_id="demo__repo",
    )
    spool_episode(updated, "demo__repo", base=tmp_path)

    specs = read_spool("demo__repo", base=tmp_path)
    assert len(specs) == 1
    assert specs[0].body == updated.body
    assert spool_count("demo__repo", base=tmp_path) == 1


def test_read_is_chronological_then_by_name(tmp_path: Path) -> None:
    spool_episodes(
        [
            _spec("PR demo/repo#3", ref=datetime(2025, 3, 1, tzinfo=UTC)),
            _spec("PR demo/repo#1", ref=datetime(2025, 1, 1, tzinfo=UTC)),
            _spec("PR demo/repo#2", ref=datetime(2025, 2, 1, tzinfo=UTC)),
        ],
        "demo__repo",
        base=tmp_path,
    )
    names = [spec.name for spec in read_spool("demo__repo", base=tmp_path)]
    assert names == ["PR demo/repo#1", "PR demo/repo#2", "PR demo/repo#3"]


def test_sort_episodes_is_oldest_first_then_by_name() -> None:
    # The canonical load order both the capture path and the spool read use.
    specs = [
        _spec("PR demo/repo#3", ref=datetime(2025, 3, 1, tzinfo=UTC)),
        _spec("PR demo/repo#1", ref=datetime(2025, 1, 1, tzinfo=UTC)),
        _spec("Issue A", ref=datetime(2025, 1, 1, tzinfo=UTC)),  # ties on time, sorts by name
    ]
    ordered = [spec.name for spec in sort_episodes(specs)]
    assert ordered == ["Issue A", "PR demo/repo#1", "PR demo/repo#3"]


def test_names_that_slugify_alike_do_not_collide(tmp_path: Path) -> None:
    # "Issue REL-10" and "Issue REL/10" both slug to "Issue_REL_10"; the name hash
    # keeps them as two files, two episodes.
    spool_episodes(
        [_spec("Issue REL-10"), _spec("Issue REL/10")],
        "demo__repo",
        base=tmp_path,
    )
    assert spool_count("demo__repo", base=tmp_path) == 2
    assert {spec.name for spec in read_spool("demo__repo", base=tmp_path)} == {
        "Issue REL-10",
        "Issue REL/10",
    }


def test_episodes_spool_under_the_capture_key_not_their_own_group(tmp_path: Path) -> None:
    # A Linear episode (its own group) captured during a GitHub ingest spools under the
    # GitHub key, but keeps its own group_id so the loader still partitions it.
    linear = _spec("Issue REL-7", group_id="linear")
    spool_episode(linear, "demo__repo", base=tmp_path)

    assert spool_count("demo__repo", base=tmp_path) == 1
    assert spool_count("linear", base=tmp_path) == 0
    assert read_spool("demo__repo", base=tmp_path)[0].group_id == "linear"


def test_missing_spool_reads_empty(tmp_path: Path) -> None:
    assert read_spool("never__captured", base=tmp_path) == []
    assert spool_count("never__captured", base=tmp_path) == 0


def test_clear_removes_the_spool_and_reports_count(tmp_path: Path) -> None:
    spool_episodes([_spec("PR demo/repo#1"), _spec("PR demo/repo#2")], "demo__repo", base=tmp_path)
    removed = clear_spool("demo__repo", base=tmp_path)
    assert removed == 2
    assert read_spool("demo__repo", base=tmp_path) == []
    assert not spool_dir("demo__repo", base=tmp_path).exists()
    # Idempotent: clearing an absent spool is a no-op, not an error.
    assert clear_spool("demo__repo", base=tmp_path) == 0
