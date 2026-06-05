"""End-to-end smoke test of the episodic pipeline.

Makes real OpenAI calls (kept to one tiny episode) against a running FalkorDB.
Verifies the load -> query path runs without error on real Graphiti + FalkorDB.
Skipped unless OPENAI_API_KEY is set and a FalkorDB instance is reachable
(`docker compose up -d falkordb`).
"""

import json
import os
import socket
from datetime import UTC, datetime

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
    pytest.mark.skipif(
        not os.environ.get("OPENAI_API_KEY"),
        reason="needs OPENAI_API_KEY for live Graphiti extraction",
    ),
    pytest.mark.skipif(
        not _falkordb_reachable(),
        reason="needs a running FalkorDB (docker compose up -d falkordb)",
    ),
]


async def test_load_and_query() -> None:
    from relic.graph.engram import make_engram
    from relic.graph.load import load_episodes
    from relic.graph.queries import reviewers_of
    from relic.ingest.mappers import EpisodeSpec

    body = json.dumps(
        {
            "repo": {"full_name": "demo/repo", "url": "https://github.com/demo/repo"},
            "pull_request": {
                "number": 1,
                "title": "add auth login",
                "url": "https://github.com/demo/repo/pull/1",
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
    episodes = [
        EpisodeSpec(
            name="PR demo/repo#1",
            body=body,
            source_description="github pull request",
            reference_time=datetime(2025, 1, 1, tzinfo=UTC),
            group_id="demo__repo",
        )
    ]
    # Isolate this test in its own FalkorDB database, dropped on the way out.
    engram = make_engram(database="relic_test")
    try:
        stats = await load_episodes(engram, episodes, group_id="demo__repo", progress=False)
        hits = await reviewers_of(engram, "auth", group_id="demo__repo")
    finally:
        await engram.driver.execute_query("MATCH (n) DETACH DELETE n")
        await engram.close()

    assert stats.episodes == 1
    assert isinstance(hits, list)  # pipeline ran end to end on real Graphiti + FalkorDB
