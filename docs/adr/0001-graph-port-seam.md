# ADR-0001: A Memory seam over Graphiti

Status: Accepted
Date: 2026-06-11

## Context

relic talks to Graphiti (the library over the FalkorDB graph) by passing a raw
`Graphiti` handle everywhere: `cli.py` builds it in four places, `load.py` calls
`add_episode` and `add_episode_bulk` on it, and `recall.py` and `queries.py` reach
through `graphiti.driver` to run `EpisodicNode.get_by_uuid`,
`EntityNode.get_by_uuid`, and a raw Cypher query. Two costs follow. A graphiti-core
version bump can break several files at once: the coupling includes two monkeypatches
pinned to 0.29.x and direct reads of `edge.fact`, `edge.episodes`, and `node.labels`.
And the recall path is hard to test: the Cypher fallback has no test, and recall is
faked ad hoc in two places.

This is not about swapping graph databases. Graphiti already abstracts that, and the
recall code survived the Kuzu to FalkorDB migration unchanged. The seam buys
version-churn locality and a single test surface, not backend independence.

## Decision

Introduce a Memory seam: two interfaces, `MemoryReader` and `MemoryWriter`, that
`recall`/`queries` and `load` depend on instead of Graphiti. A `GraphitiMemory`
adapter implements both and holds the raw `Graphiti` as a private field. The factory
`open_memory(settings, repo)` builds it (renamed from `make_engram`), and it lives in
`graph/memory.py` (renamed from `graph/engram.py`). The word "engram" is retired (see
CONTEXT.md).

Five decisions, taken in a design session:

1. **Thin interfaces plus free functions, not a facade.** Callers write
   `recall(reader, query)`, not `memory.recall(query)`. The recall logic stays in
   free functions that take a reader. A `Memory` facade was rejected: it would sit on
   top of these interfaces anyway, so it is optional sugar addable later with no
   rework. Its only real win is call-site ergonomics, which help internal Python
   entry points only. The web and desktop surfaces reach memory over MCP, not by
   importing these functions.

2. **Purpose methods, not a generic query.** The reader exposes
   `reviewer_walk(text, repo)`, with the Cypher moved inside the adapter, rather than
   a generic `run_query(cypher, params)`. A generic method cannot be faked sensibly
   (a fake would have to interpret Cypher), so the one untested path would stay
   untested. Nothing FalkorDB- or Cypher-specific crosses the seam.

3. **Translate at the seam.** The reader returns small relic value types
   (`MemoryEdge` and the like), not Graphiti's edge and node objects. The reads of
   `edge.fact`, `edge.episodes`, and `node.labels` move inside the adapter.
   Passthrough was rejected: it would leave the version-churny attribute reads in
   `recall`/`queries`, so the seam would contain nothing.

4. **Reader and writer are separate interfaces.** `MemoryReader` (search,
   get_episode, get_entity, reviewer_walk) and `MemoryWriter` (add_episode,
   add_episode_bulk, build_indices). One `GraphitiMemory` satisfies both. The split
   keeps each consumer's dependency and its test fake narrow.

5. **Naming.** Seam: `MemoryReader` / `MemoryWriter`. Production adapter:
   `GraphitiMemory`. Test adapter: `FakeMemory`, one shared fake in `conftest`
   replacing the two ad hoc ones. Factory: `open_memory`. Module: `graph/memory.py`.

## Consequences

- A graphiti-core upgrade lands in one file (`graph/memory.py`), not four.
- The two ad hoc test fakes collapse into one `FakeMemory`, and the Cypher fallback
  becomes testable for the first time.
- `recall`, `queries`, `load`, `cli`, and the tests change which type they hold and
  import. The recall and load logic itself does not change.
- A `Memory` facade is deliberately not built. Revisit only if internal call sites
  feel clunky, or if a non-Graphiti backend ever appears (unlikely: Graphiti already
  abstracts the database).
