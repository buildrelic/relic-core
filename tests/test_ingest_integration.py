"""End-to-end smoke test of the episodic pipeline. Skipped unless OPENAI_API_KEY is set.

Makes real OpenAI calls (kept to one tiny episode) against an embedded Kuzu store.
Verifies the load -> query path runs without error on real Graphiti + Kuzu.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="needs OPENAI_API_KEY for live Graphiti extraction",
)


async def test_load_and_query_in_memory(tmp_path: Path) -> None:
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
    engram = make_engram(str(tmp_path / "engram.kuzu"))
    try:
        stats = await load_episodes(engram, episodes, group_id="demo__repo", progress=False)
        hits = await reviewers_of(engram, "auth", group_id="demo__repo")
    finally:
        await engram.close()

    assert stats.episodes == 1
    assert isinstance(hits, list)  # pipeline ran end to end on real Graphiti + Kuzu
