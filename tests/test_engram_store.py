"""EngramStore against a real Postgres: schema, upsert semantics, FTS, people.

These run whenever a Postgres answers at DATABASE_URL (CI's service container,
or `just up` locally) and self-skip otherwise. Each test gets its own throwaway
workspace, dropped on the way out (the FK cascade removes every row it owned),
so runs never touch shared data.
"""

import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from relic.contracts import EpisodeSpec
from relic.engram import postgres_reachable
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
        # The FK cascade drops the workspace's memories, people, and join rows.
        await conn.execute("DELETE FROM workspaces WHERE id = $1", workspace)
    await pool.close()


def _pr_spec(
    name: str = "PR demo/repo#1",
    *,
    title: str = "Add auth login",
    number: int = 1,
) -> EpisodeSpec:
    body = {
        "source_type": "pull_request",
        "schema_version": 2,
        "repo": {"full_name": "demo/repo", "url": "https://github.com/demo/repo"},
        "pull_request": {
            "number": number,
            "title": title,
            "description": "Adds reviewer routing for the auth module.",
            "url": f"https://github.com/demo/repo/pull/{number}",
            "state": "merged",
            "author": {"login": "alice", "profile_url": "https://github.com/alice"},
        },
        "reviews": [
            {
                "reviewer": {"login": "bob", "profile_url": "https://github.com/bob"},
                "state": "APPROVED",
                "comment": "ship it",
            }
        ],
        "requested_reviewers": [{"name": "carol", "kind": "user"}],
        "files": [{"path": "src/auth/token.py", "additions": 10, "deletions": 2}],
    }
    return EpisodeSpec(
        name=name,
        body=json.dumps(body),
        source_description="github pull request",
        reference_time=datetime(2025, 1, 1, tzinfo=UTC),
        group_id="demo__repo",
    )


async def _fetch_row(store: EngramStore, name: str):
    async with store._pool.acquire() as conn:  # noqa: SLF001 - test introspection
        return await conn.fetchrow(
            "SELECT * FROM memories WHERE workspace_id = $1 AND name = $2",
            store.workspace_id,
            name,
        )


async def test_ensure_schema_is_idempotent(store: EngramStore) -> None:
    await store.ensure_schema()
    await store.ensure_schema()  # a second run must be a no-op, not an error


async def test_add_episode_lands_a_full_row(store: EngramStore) -> None:
    await store.add_episode(_pr_spec())
    row = await _fetch_row(store, "PR demo/repo#1")
    assert row is not None
    assert row["scope"] == "demo__repo"
    assert row["source"] == "github"
    assert row["artifact_type"] == "pull_request"
    assert row["title"] == "Add auth login"
    assert row["url"] == "https://github.com/demo/repo/pull/1"
    assert row["content_hash"]
    assert json.loads(row["body"])["source_type"] == "pull_request"
    assert "auth" in row["search_text"]
    assert row["user_edited"] is False
    assert row["document_id"] is None  # no Firestore configured in tests


async def test_upsert_by_name_replaces_in_place(store: EngramStore) -> None:
    await store.add_episode(_pr_spec(title="Add auth login"))
    first = await _fetch_row(store, "PR demo/repo#1")
    await store.add_episode(_pr_spec(title="Add oauth login flow"))
    second = await _fetch_row(store, "PR demo/repo#1")
    assert second["id"] == first["id"]  # same row, no fork
    assert second["title"] == "Add oauth login flow"
    assert second["content_hash"] != first["content_hash"]


async def test_user_edited_content_survives_reingest(store: EngramStore) -> None:
    await store.add_episode(_pr_spec(title="Add auth login"))
    row = await _fetch_row(store, "PR demo/repo#1")
    async with store._pool.acquire() as conn:  # noqa: SLF001 - simulate the web edit
        await conn.execute(
            "UPDATE memories SET user_edited = true, search_text = 'zebra canary edit',"
            " edited_by = 'user_1' WHERE id = $1",
            row["id"],
        )
    await store.add_episode(_pr_spec(title="Retitled by re-ingest"))
    edited = await _fetch_row(store, "PR demo/repo#1")
    # The human's content columns win; the structured payload still refreshes.
    assert edited["title"] == "Add auth login"
    assert edited["search_text"] == "zebra canary edit"
    assert edited["user_edited"] is True
    assert json.loads(edited["body"])["pull_request"]["title"] == "Retitled by re-ingest"


async def test_fts_search_hits_and_scopes(store: EngramStore) -> None:
    await store.add_episode(_pr_spec())
    hits = await store.search("auth")
    assert len(hits) == 1
    hit = hits[0]
    assert hit.name == "PR demo/repo#1"
    assert hit.url == "https://github.com/demo/repo/pull/1"
    assert hit.artifact_type == "pull_request"
    # Path segments feed the index: "auth" matched src/auth/token.py content.
    assert await store.search("auth", scope="demo__repo")
    assert await store.search("auth", scope="other__repo") == []
    assert await store.search("quantum entanglement") == []


async def test_people_and_paths_land_with_roles(store: EngramStore) -> None:
    await store.add_episode(_pr_spec())
    async with store._pool.acquire() as conn:  # noqa: SLF001 - test introspection
        people = await conn.fetch(
            """
            SELECT p.github_login, mp.role FROM memory_people mp
            JOIN people p ON p.id = mp.person_id
            JOIN memories m ON m.id = mp.memory_id
            WHERE m.workspace_id = $1
            """,
            store.workspace_id,
        )
        paths = await conn.fetch(
            "SELECT pa.path FROM memory_paths pa JOIN memories m ON m.id = pa.memory_id "
            "WHERE m.workspace_id = $1",
            store.workspace_id,
        )
    roles = {(r["github_login"], r["role"]) for r in people}
    assert ("alice", "author") in roles
    assert ("bob", "reviewer") in roles
    assert ("carol", "requested_reviewer") in roles
    assert {r["path"] for r in paths} == {"src/auth/token.py"}


async def test_people_do_not_duplicate_on_reingest(store: EngramStore) -> None:
    await store.add_episode(_pr_spec())
    await store.add_episode(_pr_spec(title="v2"))
    async with store._pool.acquire() as conn:  # noqa: SLF001 - test introspection
        count = await conn.fetchval(
            "SELECT count(*) FROM people WHERE workspace_id = $1 AND github_login = 'alice'",
            store.workspace_id,
        )
    assert count == 1


async def test_reviewers_of_finds_people_by_path_and_text(store: EngramStore) -> None:
    await store.add_episode(_pr_spec())
    hits = await store.reviewers_of("auth")
    names = {(h.name, h.relation) for h in hits}
    assert ("bob", "reviewer") in names
    assert ("alice", "author") in names
    assert all(h.episodes == ["PR demo/repo#1"] for h in hits)


async def test_workspace_isolation(store: EngramStore) -> None:
    await store.add_episode(_pr_spec())
    other = EngramStore(store._pool, workspace_id=f"test-{uuid.uuid4().hex[:12]}")  # noqa: SLF001
    await other.ensure_schema()
    try:
        assert await other.search("auth") == []  # the other workspace sees nothing
    finally:
        async with store._pool.acquire() as conn:  # noqa: SLF001
            await conn.execute("DELETE FROM workspaces WHERE id = $1", other.workspace_id)


async def test_fts_falls_back_to_any_term_for_natural_language(store: EngramStore) -> None:
    # Hook prompts are natural language; websearch semantics AND every term, so one
    # word absent from the corpus ("worked") must not blank the recall.
    await store.add_episode(_pr_spec())
    hits = await store.search("who worked on the auth token change?")
    assert hits and hits[0].name == "PR demo/repo#1"


async def test_snippets_carry_no_headline_markup(store: EngramStore) -> None:
    # ts_headline's selectors are silenced; a snippet is plain prose, never markup.
    await store.add_episode(_pr_spec())
    hits = await store.search("reviewer routing auth")
    assert hits
    for hit in hits:
        assert "StopSel" not in hit.snippet and "<b>" not in hit.snippet
