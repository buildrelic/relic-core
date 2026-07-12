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
    from relic.contracts import EpisodeSpec
    from relic.graph import open_memory
    from relic.graph.load import load_episodes
    from relic.graph.queries import reviewers_of

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
    engram = open_memory(database="relic_test")
    try:
        stats = await load_episodes(engram, episodes, group_id="demo__repo", progress=False)
        hits = await reviewers_of(engram, "auth", group_id="demo__repo")
    finally:
        await engram.execute_read("MATCH (n) DETACH DELETE n")  # eval-only escape hatch
        await engram.close()

    assert stats.episodes == 1
    assert isinstance(hits, list)  # pipeline ran end to end on real Graphiti + FalkorDB


async def test_supersede_refreshes_episode_in_place() -> None:
    # REL-118: re-presenting an edited episode (changed token) supersedes the prior one --
    # refreshed in place against real FalkorDB, not forked into a second Episodic node.
    from relic.contracts import EpisodeSpec
    from relic.graph import open_memory
    from relic.graph.load import _content_token, load_episodes

    def _pr_spec(title: str) -> EpisodeSpec:
        body = json.dumps(
            {
                "repo": {"full_name": "demo/repo", "url": "https://github.com/demo/repo"},
                "pull_request": {
                    "number": 7,
                    "title": title,
                    "url": "https://github.com/demo/repo/pull/7",
                    "state": "merged",
                    "author": {"login": "alice", "profile_url": "https://github.com/alice"},
                },
            }
        )
        return EpisodeSpec(
            name="PR demo/repo#7",
            body=body,
            source_description="github pull request",
            reference_time=datetime(2025, 1, 1, tzinfo=UTC),
            group_id="demo__repo",
        )

    engram = open_memory(database="relic_test")
    try:
        v1 = _pr_spec("add auth login")
        s1 = await load_episodes(engram, [v1], group_id="demo__repo", progress=False)
        # Re-present with edited content and v1's token in the skip map -> supersede.
        v2 = _pr_spec("add oauth login flow")
        s2 = await load_episodes(
            engram,
            [v2],
            group_id="demo__repo",
            skip={"PR demo/repo#7": _content_token(v1)},
            progress=False,
        )
        rows = await engram.execute_read(  # eval-only escape hatch
            "MATCH (e:Episodic {name: $name, group_id: $g}) RETURN e.content AS content",
            name="PR demo/repo#7",
            g="demo__repo",
        )
    finally:
        await engram.execute_read("MATCH (n) DETACH DELETE n")  # eval-only escape hatch
        await engram.close()

    assert s1.loaded == 1
    assert s2.superseded == 1 and s2.loaded == 0  # refreshed, not loaded fresh
    assert len(rows) == 1  # exactly one Episodic node for the name -> no fork
    assert rows[0]["content"] == v2.body  # the refreshed body, in place


async def test_subject_node_written_deterministically() -> None:
    # REL-99 / ADR-0002: the PullRequest Subject node and its dropped edges (IN_REPO,
    # CLOSES + the linked Issue) land deterministically from the body, anchored to the PR
    # rather than collapsed onto Repo -- regardless of what the LLM extractor materializes.
    from relic.contracts import EpisodeSpec
    from relic.graph import open_memory
    from relic.graph.load import load_episodes

    body = json.dumps(
        {
            "source_type": "pull_request",
            "repo": {"full_name": "demo/repo", "url": "https://github.com/demo/repo"},
            "pull_request": {
                "number": 5,
                "title": "add auth login",
                "url": "https://github.com/demo/repo/pull/5",
                "state": "merged",
                "author": {"login": "alice", "profile_url": "https://github.com/alice"},
            },
            "linked_issues": [{"identifier": "demo/repo#1", "relation": "closes"}],
        }
    )
    spec = EpisodeSpec(
        name="PR demo/repo#5",
        body=body,
        source_description="github pull request",
        reference_time=datetime(2025, 1, 1, tzinfo=UTC),
        group_id="demo__repo",
    )
    engram = open_memory(database="relic_test")
    try:
        await load_episodes(engram, [spec], group_id="demo__repo", progress=False)
        subjects = await engram.execute_read(
            "MATCH (n:PullRequest) WHERE n.group_id = $g RETURN n.name AS name", g="demo__repo"
        )
        in_repo = await engram.execute_read(
            "MATCH (pr:PullRequest)-[r:RELATES_TO {name: 'IN_REPO'}]->(repo:Repo) "
            "WHERE pr.group_id = $g RETURN repo.name AS repo",
            g="demo__repo",
        )
        closes = await engram.execute_read(
            "MATCH (pr:PullRequest)-[r:RELATES_TO {name: 'CLOSES'}]->(i:Issue) "
            "WHERE pr.group_id = $g RETURN i.name AS issue",
            g="demo__repo",
        )
    finally:
        await engram.execute_read("MATCH (n) DETACH DELETE n")  # eval-only escape hatch
        await engram.close()

    assert any(s["name"] == "demo/repo#5" for s in subjects)  # Subject present (~100%)
    assert any(r["repo"] == "demo/repo" for r in in_repo)  # IN_REPO anchored to the PR
    assert any(c["issue"] == "demo/repo#1" for c in closes)  # linked Issue + CLOSES landed
