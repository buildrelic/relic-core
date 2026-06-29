"""Standalone Granola ingest: per-owner-scope grouping, capture, and spooling.

REL-117 wired granola as a side effect of a repo capture; REL-119's HTTP seam needs a
standalone `relic ingest --source granola`. These cover the new per-scope plumbing
(group + spool one partition per note owner) without a graph or the network.
"""

import logging

from relic import cli
from relic.ingest import read_spool
from relic.ingest.mappers import MeetingRec, meeting_to_episode


def _meeting(meeting_id: str, owner_email: str) -> MeetingRec:
    return MeetingRec(
        id=meeting_id,
        title="sync",
        url=f"https://notes.granola.ai/d/{meeting_id}",
        owner_email=owner_email,
        participants=["Zidan Kazi"],
        summary="## Notes\n- ok",
        transcript="Zidan: hi",
        occurred_at="2026-06-22T15:00:00Z",
        created_at="2026-06-22T15:00:00Z",
    )


def test_by_scope_groups_episodes_by_owner() -> None:
    specs = [
        meeting_to_episode(_meeting("not_a", "zidan@tryrelic.io")),
        meeting_to_episode(_meeting("not_b", "paris@tryrelic.io")),
        meeting_to_episode(_meeting("not_c", "zidan@tryrelic.io")),
    ]
    grouped = cli._by_scope(specs)
    assert set(grouped) == {"granola__zidan_tryrelic_io", "granola__paris_tryrelic_io"}
    assert len(grouped["granola__zidan_tryrelic_io"]) == 2
    assert len(grouped["granola__paris_tryrelic_io"]) == 1


async def test_capture_granola_spools_one_partition_per_owner(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    async def _fake_fetch(api_key, *, months, limit=None):
        assert api_key == "grn_serverkey"  # capture reads the resolved key off settings
        return [
            _meeting("not_a", "zidan@tryrelic.io"),
            _meeting("not_b", "paris@tryrelic.io"),
        ]

    monkeypatch.setattr("relic.ingest.fetch_meetings", _fake_fetch)

    episodes, fetch_s, prepare_s = await cli._capture_granola(
        None, api_key="grn_serverkey", months=12, log=logging.getLogger("t")
    )

    # both owners are present, each spooled into its own granola__<email> partition
    assert {spec.group_id for spec in episodes} == {
        "granola__zidan_tryrelic_io",
        "granola__paris_tryrelic_io",
    }
    assert {s.name for s in read_spool("granola__zidan_tryrelic_io")} == {"Meeting not_a"}
    assert {s.name for s in read_spool("granola__paris_tryrelic_io")} == {"Meeting not_b"}
    assert fetch_s >= 0 and prepare_s >= 0
