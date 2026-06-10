---
name: graph-inspect
description: Inspect or debug the FalkorDB knowledge graph behind relic - what got extracted, why recall returns wrong or empty results, episode/entity/edge counts, group_id partitions. Use whenever recall quality looks off, after an ingest to verify what landed, for "what's in the graph", "query falkordb", or any direct Cypher poking at the memory graph.
---

# Inspecting the graph

The graph lives in FalkorDB (Redis protocol, localhost:6379 via `just up`). Each repo gets its OWN FalkorDB graph, keyed by the group_id slug: graphiti clones its driver with `database=group_id`, so `buildrelic/relic-core` lives in a graph literally named `buildrelic__relic-core`. The `FALKORDB_DATABASE` setting (`relic`) is only the default graph when nothing partitions; per-repo data is never in it. When recall is wrong, look at the graph directly before touching retrieval code: the usual question is "extraction problem or search problem?"

## Connecting

FalkorDB runs in docker, so use the container's redis-cli (host redis-cli works too if installed):

```
docker compose exec falkordb redis-cli GRAPH.LIST
docker compose exec falkordb redis-cli GRAPH.RO_QUERY buildrelic__relic-core "MATCH (n) RETURN count(n)"
```

Start with `GRAPH.LIST` to see which repo graphs exist. Use `GRAPH.RO_QUERY` for inspection, not `GRAPH.QUERY`: a plain query against a graph name that does not exist silently CREATES an empty graph key, which then pollutes `GRAPH.LIST`. For anything beyond one-liners, query from Python instead (async, same client relic uses): build the driver via `make_engram()` from `src/relic/graph/engram.py` or use the `falkordb` package directly.

## The schema you'll see

- Nodes: `Episodic` (one per ingested PR/issue, holds the source JSON), `Entity` (extracted people, repos, PRs, ...), `Community` (optional clusters).
- Edges: `MENTIONS` (episode → entity it referenced), `RELATES_TO` (entity → entity, carries the `fact` string that recall returns).
- Partitioning is by graph key, not by property: the slug comes from `repo_group_id()` in `src/relic/ingest/mappers.py` (`owner/name` becomes `owner__name`). A missing graph in `GRAPH.LIST` means that repo was never ingested; that is an ingest gap, not extraction or search.
- Relic's typed layer (PersonNode, Authored, Reviewed, TouchesPath, ...) is defined in `engram.py`; the type name lands as a label/property on the Graphiti node or edge.

## Useful queries

All run against the repo's graph, e.g. `... redis-cli GRAPH.RO_QUERY buildrelic__relic-core "<cypher>"`:

```
// did the ingest land? compare with the run's LoadStats
MATCH (e:Episodic) RETURN count(e)

// what entities did extraction produce?
MATCH (n:Entity) RETURN n.name LIMIT 50

// the facts recall searches over
MATCH (a)-[r:RELATES_TO]->(b) RETURN a.name, r.fact, b.name LIMIT 25

// trace a fact back to its source episode
MATCH (ep:Episodic)-[:MENTIONS]->(n:Entity {name: 'Paris'}) RETURN ep.name LIMIT 10
```

## Diagnosing bad recall

1. Episodes there but few entities/facts → extraction problem (model, prompt, episode content too thin after clipping). Inspect a raw episode body: it is stored on the Episodic node and verbatim under `data/raw/`.
2. Facts there but recall misses them → search problem. Try the deterministic path: `reviewers_of()` in `src/relic/graph/queries.py` falls back to a plain Cypher walk; if Cypher finds it and hybrid search does not, suspect fulltext/embedding search. Mind the RediSearch quirk: empty-sanitizing queries are guarded by `_patch_falkordb_empty_query()` in `engram.py`.
3. Wrong group_id is the boring cause to rule out first: `TARGET_REPO` env defaults recall/query when no flag is passed.

`just reset-graph` wipes the FalkorDB volume entirely (all repos). It is the nuclear option; prefer `--fresh` on a single repo's ingest.
