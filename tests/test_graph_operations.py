"""Live graph-operation tests: recall / reviewers_of / graph_stats on a real graph.

These seed a small, known corpus into a real FalkorDB via the real ``load_episodes``
path (real LLM extraction) and then assert each read operation surfaces the
deterministic anchors -- a known reviewer login, a known PR url, the expected node
and edge types. The LLM's prose varies run to run, so assertions target the
structured facts that ingest writes deterministically, never exact wording.

Run sparingly -- each run makes real LLM calls. Gated behind ``RELIC_LIVE_TESTS=1``
and a reachable FalkorDB, so a normal ``just check`` skips them:

    RELIC_LIVE_TESTS=1 uv run pytest tests/test_graph_operations.py

The corpus is seeded once per module (the expensive LLM step); each operation then
queries the shared graph with its own short-lived engram connection. The engram's
database is set equal to the capture's ``group_id`` so episodes and search share one
FalkorDB database (Graphiti routes episodes to a database named after the group_id;
matching them avoids querying an empty default database).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import socket
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from relic.config import get_settings

# A single capture key, used as BOTH the FalkorDB database and the episode group_id.
# Underscores only: hyphens need escaping in the RediSearch fulltext scope.
GROUP_ID = "relic_graphops_test"
PR_URL = "https://github.com/demo/repo/pull/1"


def _falkordb_reachable() -> bool:
    settings = get_settings()
    try:
        with socket.create_connection((settings.falkordb_host, settings.falkordb_port), timeout=1):
            return True
    except OSError:
        return False


pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("RELIC_LIVE_TESTS"),
        reason="set RELIC_LIVE_TESTS=1 to run live graph-operation tests (real LLM calls)",
    ),
    pytest.mark.skipif(
        not _falkordb_reachable(),
        reason="needs a running FalkorDB (docker compose up -d falkordb)",
    ),
]


def _pr_episode():
    """One PR episode with a known author (alice) and reviewer (bob) over src/auth."""
    from relic.contracts import EpisodeSpec

    body = json.dumps(
        {
            "repo": {"full_name": "demo/repo", "url": "https://github.com/demo/repo"},
            "pull_request": {
                "number": 1,
                "title": "add auth login",
                "url": PR_URL,
                "state": "merged",
                "author": {"login": "alice", "profile_url": "https://github.com/alice"},
            },
            "reviews": [
                {"login": "bob", "profile_url": "https://github.com/bob", "state": "APPROVED"}
            ],
            "files": [
                {"path": "src/auth/login.py", "additions": 10, "deletions": 0, "status": "added"}
            ],
        }
    )
    return EpisodeSpec(
        name="PR demo/repo#1",
        body=body,
        source_description="github pull request",
        reference_time=datetime(2025, 1, 1, tzinfo=UTC),
        group_id=GROUP_ID,
    )


@pytest.fixture(scope="module")
def seeded_graph() -> Iterator[str]:
    """Seed the known corpus into FalkorDB once, yield the group_id, then wipe it.

    Synchronous so the one-time LLM-heavy load runs in its own event loop, decoupled
    from each test's own loop. Tests open their own engram to query the shared graph.
    """
    from relic.graph.engram import make_engram
    from relic.graph.load import load_episodes

    async def _seed() -> None:
        engram = make_engram(database=GROUP_ID)
        try:
            await load_episodes(engram, [_pr_episode()], group_id=GROUP_ID, progress=False)
        finally:
            await engram.close()

    async def _wipe() -> None:
        engram = make_engram(database=GROUP_ID)
        try:
            await engram.driver.execute_query("MATCH (n) DETACH DELETE n")
        finally:
            await engram.close()

    asyncio.run(_seed())
    try:
        yield GROUP_ID
    finally:
        asyncio.run(_wipe())


def _load_graph_stats():
    """Import eval/graph_stats.py by path (eval/ is a tools dir, not a package)."""
    path = Path(__file__).resolve().parent.parent / "eval" / "graph_stats.py"
    spec = importlib.util.spec_from_file_location("graph_stats", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_reviewers_of_surfaces_a_known_person(seeded_graph: str) -> None:
    from relic.graph.engram import make_engram
    from relic.graph.queries import reviewers_of

    engram = make_engram(database=GROUP_ID)
    try:
        hits = await reviewers_of(engram, "auth login", group_id=GROUP_ID)
    finally:
        await engram.close()

    names = {hit.name.lower() for hit in hits}
    assert names & {"alice", "bob"}, f"expected alice/bob among reviewers, got {names}"


async def test_recall_cites_the_source_pr(seeded_graph: str) -> None:
    from relic.graph.engram import make_engram
    from relic.graph.recall import recall

    engram = make_engram(database=GROUP_ID)
    try:
        answer = await recall(engram, "auth login", group_id=GROUP_ID)
    finally:
        await engram.close()

    assert not answer.is_empty, "recall returned no facts for the seeded PR"
    urls = {src.url for fact in answer.facts for src in fact.sources}
    assert PR_URL in urls, f"expected the seeded PR url among cited sources, got {urls}"


async def test_graph_stats_reports_the_landed_structure(seeded_graph: str) -> None:
    from relic.graph.engram import make_engram

    graph_stats = _load_graph_stats()

    engram = make_engram(database=GROUP_ID)
    try:
        structure = await graph_stats.structure(engram, GROUP_ID)
    finally:
        await engram.close()

    assert structure["episode_count"] >= 1, structure
    assert structure["entities_by_type"], "no typed entities landed"
    assert structure["edges_by_relation"], "no typed edges landed"
    assert any("person" in label.lower() for label in structure["entities_by_type"]), structure
