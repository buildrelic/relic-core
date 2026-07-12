"""Standalone Notion ingest: per-workspace-scope grouping, capture, and spooling.

Covers the standalone `relic ingest --source notion` plumbing (group + spool one partition
per workspace) without a graph or the network. Mirrors test_granola_ingest.py.
"""

import logging

from relic import cli
from relic.ingest import read_spool
from relic.ingest.mappers import PageRec, page_to_episode


def _page(page_id: str, workspace_id: str) -> PageRec:
    return PageRec(
        id=page_id,
        title="architecture notes",
        url=f"https://www.notion.so/{page_id}",
        workspace_id=workspace_id,
        participants=["Zidan Kazi"],
        text="## Notes\n- ok",
        created_at="2026-06-22T15:00:11.000Z",
        last_edited_at="2026-06-22T15:48:02.000Z",
    )


def test_by_scope_groups_pages_by_workspace() -> None:
    specs = [
        page_to_episode(_page("pa", "ws-1")),
        page_to_episode(_page("pb", "ws-2")),
        page_to_episode(_page("pc", "ws-1")),
    ]
    grouped = cli._by_scope(specs)
    assert set(grouped) == {"notion__ws-1", "notion__ws-2"}
    assert len(grouped["notion__ws-1"]) == 2
    assert len(grouped["notion__ws-2"]) == 1


async def test_capture_notion_spools_one_partition_per_workspace(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    async def _fake_fetch(api_key, *, months, limit=None):
        assert api_key == "ntn_serverkey"  # capture reads the resolved key off settings
        return [_page("pa", "ws-1"), _page("pb", "ws-2")]

    monkeypatch.setattr("relic.ingest.fetch_pages", _fake_fetch)

    episodes, fetch_s, prepare_s = await cli._capture_notion(
        None, api_key="ntn_serverkey", months=12, log=logging.getLogger("t")
    )

    # both workspaces present, each spooled into its own notion__<workspace> partition
    assert {spec.group_id for spec in episodes} == {"notion__ws-1", "notion__ws-2"}
    assert {s.name for s in read_spool("notion__ws-1")} == {"Notion pa"}
    assert {s.name for s in read_spool("notion__ws-2")} == {"Notion pb"}
    assert fetch_s >= 0 and prepare_s >= 0
