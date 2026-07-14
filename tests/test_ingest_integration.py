"""End-to-end smoke of the episodic pipeline against a real Postgres.

spec -> load_episodes -> recall/reviewers_of, plus the refresh-in-place path.
Runs whenever a Postgres answers at DATABASE_URL (CI's service container, or
`just up` locally) and self-skips otherwise. No LLM, no API keys: the load is a
deterministic write now (ADR-0007).
"""

import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from relic.contracts import EpisodeSpec
from relic.engram import postgres_reachable, recall, reviewers_of
from relic.engram.load import _content_token, load_episodes
from relic.engram.store import EngramStore

_DSN = os.environ.get("DATABASE_URL", "postgresql://relic:relic@localhost:5432/relic")

pytestmark = pytest.mark.skipif(
    not postgres_reachable(_DSN),
    reason="needs a running Postgres (`just up`)",
)


@pytest.fixture
async def store() -> AsyncIterator[EngramStore]:
    import asyncpg

    pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=2)
    workspace = f"test-{uuid.uuid4().hex[:12]}"
    s = EngramStore(pool, workspace_id=workspace)
    await s.ensure_schema()
    yield s
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM workspaces WHERE id = $1", workspace)
    await pool.close()


def _pr_spec(title: str = "add auth login") -> EpisodeSpec:
    body = json.dumps(
        {
            "source_type": "pull_request",
            "schema_version": 2,
            "repo": {"full_name": "demo/repo", "url": "https://github.com/demo/repo"},
            "pull_request": {
                "number": 7,
                "title": title,
                "url": "https://github.com/demo/repo/pull/7",
                "state": "merged",
                "author": {"login": "alice", "profile_url": "https://github.com/alice"},
            },
            "reviews": [
                {"reviewer": {"login": "bob"}, "state": "APPROVED", "comment": "looks right"}
            ],
            "files": [{"path": "src/auth/login.py", "additions": 10, "deletions": 0}],
        }
    )
    return EpisodeSpec(
        name="PR demo/repo#7",
        body=body,
        source_description="github pull request",
        reference_time=datetime(2025, 1, 1, tzinfo=UTC),
        group_id="demo__repo",
    )


async def test_load_then_recall_cites_the_source(store: EngramStore) -> None:
    stats = await load_episodes(store, [_pr_spec()], group_id="demo__repo", progress=False)
    assert stats.loaded == 1

    answer = await recall(store, "auth login", scope="demo__repo")
    assert not answer.is_empty
    fact = answer.facts[0]
    assert "add auth login" in fact.fact
    assert fact.sources[0].label == "PR demo/repo#7"
    assert fact.sources[0].url == "https://github.com/demo/repo/pull/7"

    hits = await reviewers_of(store, "auth", scope="demo__repo")
    assert {h.name for h in hits} >= {"alice", "bob"}


async def test_refresh_replaces_episode_in_place(store: EngramStore) -> None:
    # A re-presented edited episode (changed token) refreshes the row in place, never forks.
    v1 = _pr_spec("add auth login")
    s1 = await load_episodes(store, [v1], group_id="demo__repo", progress=False)
    v2 = _pr_spec("add oauth login flow")
    s2 = await load_episodes(
        store,
        [v2],
        group_id="demo__repo",
        skip={"PR demo/repo#7": _content_token(v1)},
        progress=False,
    )
    assert s1.loaded == 1
    assert s2.superseded == 1 and s2.loaded == 0  # refreshed, not loaded fresh

    async with store._pool.acquire() as conn:  # noqa: SLF001 - test introspection
        rows = await conn.fetch(
            "SELECT title FROM memories WHERE workspace_id = $1 AND name = $2",
            store.workspace_id,
            "PR demo/repo#7",
        )
    assert len(rows) == 1  # exactly one row for the name -> no fork
    assert rows[0]["title"] == "add oauth login flow"  # the refreshed body, in place
