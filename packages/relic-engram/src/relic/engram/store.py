"""The engram store: Postgres relational memory plus an optional Firestore mirror.

One adapter, ``EngramStore``, owns every SQL statement and the document mirror. The
rest of the platform depends on the two Protocols (``EngramReader``/``EngramWriter``)
and the plain value types, never on asyncpg -- the same seam discipline the graph
adapter kept (ADR-0003), carried over to the store that replaced it (ADR-0007).

Supersession is structural here, not a cascade: ``memories`` is unique on
``(workspace_id, name)`` and ``add_episode`` is an upsert, so a re-presented episode
with changed bytes replaces its row in place. There is nothing to remove first and
no way to fork.

Postgres is authoritative: the typed episode body always lands in ``memories.body``.
Firestore, when configured, mirrors a rendered document for the web workspace UI;
an outage there degrades the mirror and never fails an ingest.
"""

from __future__ import annotations

import json
import re
import socket
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlparse

from relic.engram.derive import Derived, derive
from relic.obs import get_logger

if TYPE_CHECKING:
    import asyncpg

    from relic.config import Settings
    from relic.contracts import EpisodeSpec
    from relic.engram.documents import DocumentStore

log = get_logger("engram")


# --- Value types --------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class EngramHit:
    """One search result: a memory row's identity plus a query-focused snippet."""

    name: str
    title: str
    url: str | None
    snippet: str
    artifact_type: str
    scope: str
    rank: float
    reference_time: datetime | None = None


@dataclass(slots=True, frozen=True)
class ReviewerHit:
    """A person connected to work matching a query, with the memory that proves it."""

    name: str
    relation: str  # the memory_people role: reviewer | author | ...
    fact: str
    episodes: list[str]  # memory names supporting the hit
    profile_url: str | None = None


# --- Protocols ----------------------------------------------------------------


class EngramReader(Protocol):
    """Read side of the engram: full-text search over the memory rows."""

    async def search(
        self, query: str, *, scope: str | None = None, num_results: int = 10
    ) -> list[EngramHit]: ...


class EngramWriter(Protocol):
    """Write side of the engram: land typed episodes as rows plus documents."""

    async def add_episode(self, spec: EpisodeSpec) -> None: ...

    async def ensure_schema(self) -> None: ...


# --- Schema -------------------------------------------------------------------

# Applied idempotently by ensure_schema(). relic-core owns this DDL; the landing web
# app reads and edits against it directly, so changes here are a cross-repo contract
# change (see landing/relic-core-contract.md).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
  id          text PRIMARY KEY,
  name        text,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS people (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  text NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  display_name  text,
  github_login  text,
  linear_id     text,
  slack_id      text,
  email         text,
  created_at    timestamptz NOT NULL DEFAULT now()
);
-- Identity precedence: login, else email, else display name. Partial uniques
-- because Postgres treats NULLs as distinct: a plain UNIQUE(ws, github_login)
-- would let NULL-login duplicates through.
CREATE UNIQUE INDEX IF NOT EXISTS people_ws_login ON people (workspace_id, github_login)
  WHERE github_login IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS people_ws_email ON people (workspace_id, email)
  WHERE github_login IS NULL AND email IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS people_ws_name  ON people (workspace_id, display_name)
  WHERE github_login IS NULL AND email IS NULL AND display_name IS NOT NULL;

CREATE TABLE IF NOT EXISTS memories (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id   text NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  scope          text NOT NULL,
  source         text NOT NULL,
  artifact_type  text NOT NULL,
  name           text NOT NULL,
  title          text NOT NULL DEFAULT '',
  url            text,
  reference_time timestamptz,
  content_hash   text NOT NULL,
  schema_version int  NOT NULL,
  body           jsonb NOT NULL,
  document_id    text,
  search_text    text NOT NULL DEFAULT '',
  search_tsv     tsvector GENERATED ALWAYS AS
                   (to_tsvector('english',
                     coalesce(title,'') || ' ' || coalesce(search_text,''))) STORED,
  user_edited    boolean NOT NULL DEFAULT false,
  edited_by      text,
  edited_at      timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, name)
);
CREATE INDEX IF NOT EXISTS memories_tsv      ON memories USING gin (search_tsv);
CREATE INDEX IF NOT EXISTS memories_ws_scope ON memories (workspace_id, scope);

CREATE TABLE IF NOT EXISTS memory_people (
  memory_id uuid NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  person_id uuid NOT NULL REFERENCES people(id)   ON DELETE CASCADE,
  role      text NOT NULL,
  PRIMARY KEY (memory_id, person_id, role)
);
CREATE INDEX IF NOT EXISTS memory_people_person ON memory_people (person_id);

CREATE TABLE IF NOT EXISTS memory_paths (
  memory_id uuid NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  path      text NOT NULL,
  PRIMARY KEY (memory_id, path)
);
CREATE INDEX IF NOT EXISTS memory_paths_path ON memory_paths (path text_pattern_ops);

-- RESERVED (do not create yet): the pgvector fast-follow.
-- CREATE EXTENSION vector;
-- CREATE TABLE memory_embeddings (
--   memory_id uuid PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
--   embedding vector(1536), model text NOT NULL,
--   updated_at timestamptz NOT NULL DEFAULT now());
"""

_SEARCH_SQL = """
SELECT m.name, m.title, m.url, m.artifact_type, m.scope, m.reference_time,
       ts_rank_cd(m.search_tsv, q) AS rank,
       ts_headline('english', m.search_text, q,
         'StartSel="",StopSel="",MaxWords=30,MinWords=10,MaxFragments=1') AS snippet
FROM memories m, websearch_to_tsquery('english', $1) q
WHERE m.workspace_id = $2
  AND ($3::text IS NULL OR m.scope = $3)
  AND m.search_tsv @@ q
ORDER BY rank DESC, m.reference_time DESC NULLS LAST
LIMIT $4
"""

_REVIEWERS_SQL = """
SELECT coalesce(p.display_name, p.github_login, '') AS name,
       p.github_login,
       mp.role,
       m.name AS memory_name, m.url, m.title,
       count(*) OVER (PARTITION BY p.id) AS support
FROM memory_people mp
JOIN people   p ON p.id = mp.person_id
JOIN memories m ON m.id = mp.memory_id
WHERE m.workspace_id = $1
  AND ($2::text IS NULL OR m.scope = $2)
  AND mp.role IN ('reviewer', 'author', 'requested_reviewer')
  AND (EXISTS (SELECT 1 FROM memory_paths pa
               WHERE pa.memory_id = m.id AND pa.path ILIKE '%' || $3 || '%')
       OR m.search_tsv @@ websearch_to_tsquery('english', $3))
ORDER BY support DESC, m.reference_time DESC NULLS LAST
LIMIT $4
"""

# Content columns are refreshed only while user_edited is false: a human edit in the
# workspace UI wins over a re-ingest of the same artifact (ADR-0007). `--fresh`
# clears the checkpoint but still respects the edit; the row keeps the human's text.
_UPSERT_SQL = """
INSERT INTO memories (workspace_id, scope, source, artifact_type, name, title, url,
                      reference_time, content_hash, schema_version, body, search_text)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12)
ON CONFLICT (workspace_id, name) DO UPDATE SET
  scope = excluded.scope,
  source = excluded.source,
  artifact_type = excluded.artifact_type,
  title = CASE WHEN memories.user_edited THEN memories.title ELSE excluded.title END,
  url = excluded.url,
  reference_time = excluded.reference_time,
  content_hash = excluded.content_hash,
  schema_version = excluded.schema_version,
  body = excluded.body,
  search_text = CASE WHEN memories.user_edited THEN memories.search_text
                     ELSE excluded.search_text END,
  updated_at = now()
RETURNING id, user_edited
"""


class EngramStore:
    """The one adapter over Postgres (and, best-effort, Firestore)."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        workspace_id: str,
        documents: DocumentStore | None = None,
    ) -> None:
        self._pool = pool
        self._workspace_id = workspace_id
        self._documents = documents
        self._warned_no_mirror = False

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    async def ensure_schema(self) -> None:
        """Apply the DDL and upsert the workspace row. Idempotent, safe to re-run."""
        async with self._pool.acquire() as conn:
            await conn.execute(_SCHEMA)
            await conn.execute(
                "INSERT INTO workspaces (id) VALUES ($1) ON CONFLICT (id) DO NOTHING",
                self._workspace_id,
            )

    # --- Writer ---------------------------------------------------------------

    async def add_episode(self, spec: EpisodeSpec) -> None:
        """Land one typed episode: memory row, people, paths, then the doc mirror.

        The row upsert keys on ``(workspace_id, name)``, so a changed body replaces
        its prior row in place (supersession) and an identical replay is a no-op
        refresh. Content columns respect a human edit (``user_edited``). The
        Firestore mirror runs after the transaction commits and never raises.
        """
        derived = derive(spec)
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                _UPSERT_SQL,
                self._workspace_id,
                spec.group_id,
                derived.source,
                derived.artifact_type,
                spec.name,
                derived.title,
                derived.url,
                spec.reference_time,
                _sha256(spec.body),
                spec.schema_version,
                spec.body,
                derived.search_text,
            )
            memory_id = row["id"]
            user_edited = row["user_edited"]
            await self._replace_people(conn, memory_id, derived)
            await self._replace_paths(conn, memory_id, derived.paths)
        # The mirror is outside the transaction on purpose: a Firestore hiccup must
        # not roll back a landed memory. Skipped for user-edited rows so a re-ingest
        # never clobbers the human's markdown.
        if not user_edited:
            await self._mirror_document(memory_id, spec, derived)

    async def _replace_people(self, conn: Any, memory_id: Any, derived: Derived) -> None:
        """Upsert the people this memory names and rebuild its role join rows."""
        await conn.execute("DELETE FROM memory_people WHERE memory_id = $1", memory_id)
        for person, role in derived.people:
            person_id = await self._upsert_person(conn, person)
            if person_id is None:
                continue
            await conn.execute(
                "INSERT INTO memory_people (memory_id, person_id, role) VALUES ($1, $2, $3) "
                "ON CONFLICT DO NOTHING",
                memory_id,
                person_id,
                role,
            )

    async def _upsert_person(self, conn: Any, person: dict[str, str | None]) -> Any | None:
        """Find-or-create a person by the precedence key: login, else email, else name."""
        login = person.get("github_login")
        email = person.get("email")
        name = person.get("display_name")
        if login:
            return await conn.fetchval(
                """
                INSERT INTO people (workspace_id, github_login, display_name, email)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (workspace_id, github_login) WHERE github_login IS NOT NULL
                DO UPDATE SET display_name = coalesce(people.display_name, excluded.display_name),
                              email = coalesce(people.email, excluded.email)
                RETURNING id
                """,
                self._workspace_id,
                login,
                name,
                email,
            )
        if email:
            return await conn.fetchval(
                """
                INSERT INTO people (workspace_id, email, display_name)
                VALUES ($1, $2, $3)
                ON CONFLICT (workspace_id, email) WHERE github_login IS NULL AND email IS NOT NULL
                DO UPDATE SET display_name = coalesce(people.display_name, excluded.display_name)
                RETURNING id
                """,
                self._workspace_id,
                email,
                name,
            )
        if name:
            return await conn.fetchval(
                """
                INSERT INTO people (workspace_id, display_name)
                VALUES ($1, $2)
                ON CONFLICT (workspace_id, display_name)
                  WHERE github_login IS NULL AND email IS NULL AND display_name IS NOT NULL
                DO UPDATE SET display_name = excluded.display_name
                RETURNING id
                """,
                self._workspace_id,
                name,
            )
        return None  # identity-less ref (ghost account): no row, no join

    async def _replace_paths(self, conn: Any, memory_id: Any, paths: list[str]) -> None:
        await conn.execute("DELETE FROM memory_paths WHERE memory_id = $1", memory_id)
        for path in paths:
            await conn.execute(
                "INSERT INTO memory_paths (memory_id, path) VALUES ($1, $2) "
                "ON CONFLICT DO NOTHING",
                memory_id,
                path,
            )

    async def _mirror_document(self, memory_id: Any, spec: EpisodeSpec, derived: Derived) -> None:
        """Write the rendered document to Firestore, best-effort, then record its id."""
        if self._documents is None:
            if not self._warned_no_mirror:
                self._warned_no_mirror = True
                log.info("firestore not configured; documents stay local-only (memories.body)")
            return
        try:
            await self._documents.write(
                self._workspace_id,
                str(memory_id),
                {
                    "memoryName": spec.name,
                    "scope": spec.group_id,
                    "source": derived.source,
                    "artifactType": derived.artifact_type,
                    "body": json.loads(spec.body),
                    "markdown": derived.markdown,
                },
            )
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE memories SET document_id = $1 WHERE id = $2",
                    str(memory_id),
                    memory_id,
                )
        except Exception as exc:  # noqa: BLE001 - the mirror must never fail an ingest
            reason = f"{type(exc).__name__}: {exc}".splitlines()[0]
            log.warning("firestore mirror failed for %s: %s", spec.name, reason)

    # --- Reader ---------------------------------------------------------------

    async def search(
        self, query: str, *, scope: str | None = None, num_results: int = 10
    ) -> list[EngramHit]:
        """Full-text search over the workspace's memories, best match first.

        Two passes: websearch semantics first (every term must match), then an
        any-term fallback when that finds nothing. Hook prompts are natural
        language ("who worked on the webhook receiver?"), and one word absent
        from the corpus must not blank the whole recall; ts_rank_cd still puts
        the rows matching the most terms first.
        """
        if not query.strip():
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(_SEARCH_SQL, query, self._workspace_id, scope, num_results)
            if not rows:
                any_term = " OR ".join(re.findall(r"[\w.-]+", query))
                if any_term:
                    rows = await conn.fetch(
                        _SEARCH_SQL, any_term, self._workspace_id, scope, num_results
                    )
        return [
            EngramHit(
                name=r["name"],
                title=r["title"],
                url=r["url"],
                snippet=" ".join((r["snippet"] or "").split()),
                artifact_type=r["artifact_type"],
                scope=r["scope"],
                rank=float(r["rank"]),
                reference_time=r["reference_time"],
            )
            for r in rows
        ]

    async def reviewers_of(
        self, text: str, *, scope: str | None = None, limit: int = 10
    ) -> list[ReviewerHit]:
        """People connected to work matching ``text``, weighted by how often."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(_REVIEWERS_SQL, self._workspace_id, scope, text, limit)
        hits: list[ReviewerHit] = []
        seen: set[tuple[str, str]] = set()
        for r in rows:
            name = r["name"] or r["github_login"] or ""
            key = (name, r["role"])
            if not name or key in seen:
                continue
            seen.add(key)
            hits.append(
                ReviewerHit(
                    name=name,
                    relation=r["role"],
                    fact=f"{r['role']} on {r['title'] or r['memory_name']}",
                    episodes=[r["memory_name"]],
                    profile_url=(
                        f"https://github.com/{r['github_login']}" if r["github_login"] else None
                    ),
                )
            )
        return hits

    async def close(self) -> None:
        await self._pool.close()


# --- Factory + probe ----------------------------------------------------------


async def open_engram(settings: Settings | None = None) -> EngramStore:
    """Connect to Postgres, apply the schema, and wire the optional document mirror."""
    import asyncpg

    from relic.config import get_settings
    from relic.engram.documents import DocumentStore, firestore_configured

    settings = settings or get_settings()
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=5)
    documents = DocumentStore.from_settings(settings) if firestore_configured(settings) else None
    store = EngramStore(pool, workspace_id=settings.relic_workspace, documents=documents)
    await store.ensure_schema()
    return store


def postgres_reachable(dsn: str, *, timeout: float = 0.5) -> bool:
    """True when a TCP connection to the DSN's host:port succeeds. No query is run."""
    parsed = urlparse(dsn)
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _sha256(body: str) -> str:
    import hashlib

    return hashlib.sha256(body.encode("utf-8")).hexdigest()
