# Memory and recall

Memory is a temporal knowledge graph: people, repos, pull requests, reviews,
issues, and the edges between them, extracted from episodes by Graphiti and stored
in FalkorDB. Recall queries that graph and returns facts, each carrying the source
it came from.

This doc covers the engram factory, per-repo partitioning, the recall and query
paths, the scorecard, and the FalkorDB workaround.

## The engram

[`make_engram`](../src/relic/graph/engram.py) builds the Graphiti client. It wires
FalkorDB as the graph driver and OpenAI for the three model jobs:

- **LLM.** `OpenAIClient` with `gpt-4o-mini` (extraction during ingest).
- **Embeddings.** `OpenAIEmbedder` with `text-embedding-3-small` (semantic search).
- **Reranker.** `OpenAIRerankerClient` with `gpt-4o-mini` (ordering search hits).

Connection params (`FALKORDB_*`) and the OpenAI key fall back to settings when not
passed, so a caller can build a default-local client with no arguments. If no
OpenAI key is found anywhere, it raises a clear error rather than letting the
OpenAI SDK raise a cryptic one. Clients are built here, never at import, so
`relic --help` stays key-free.

`ensure_indexes` runs `build_indices_and_constraints` (idempotent). FalkorDB
honors it, unlike the embedded Kuzu backend it replaced, so search works once it
has run. `load_episodes` calls it before the first episode.

## Per-repo partitioning

Each repo's memory lives in its own FalkorDB graph, keyed by `group_id`. This is
the multi-tenant partitioning FalkorDB was chosen for.

The mechanism: `repo_group_id("owner/name")` produces a slug like
`owner__name`. Graphiti routes group-scoped operations to a FalkorDB graph of that
name by cloning its driver (`driver.clone(database=group_id)` inside Graphiti's
`add_episode` and `search`). So:

- **Ingest** builds the engram with the default database (`FALKORDB_DATABASE`,
  default `relic`) but calls `add_episode(group_id=slug)`. Graphiti clones to the
  `slug` graph, and the writes land there.
- **Recall, query, and eval** build the engram with `database = group_id` and also
  pass `group_ids=[group_id]` to search. Both the connection and the filter target
  the same per-repo graph ([`_recall`, `_query`, `_eval`](../src/relic/cli.py)).
- **Without a repo** (`--repo` unset and no `TARGET_REPO`), `group_id` is `None`
  and operations use the default `relic` graph, which holds only ungrouped data.

The repo is resolved as `--repo` if given, else the `TARGET_REPO` setting. So a
common setup is to set `TARGET_REPO` once and let recall default to it.

## Recall: facts with sources

[`recall.py`](../src/relic/graph/recall.py) is the general path. It is the keystone
of the "every answer cites its source" promise.

```
RecallAnswer
├── query: str
└── facts: list[RecalledFact]
        ├── fact: str           # the edge's fact text
        ├── relation: str       # the edge name (e.g. REVIEWED)
        └── sources: list[Source]
                ├── label: str  # the episode name, e.g. "PR owner/name#12"
                └── url: str?    # the PR or issue URL, resolved best-effort
```

`recall(graphiti, query, group_id, num_results)`:

1. Calls `graphiti.search(query, group_ids, num_results)`: hybrid semantic plus
   keyword search, reranked. This is the portable Graphiti API, so it survived the
   Kuzu to FalkorDB migration unchanged.
2. For each returned edge, takes its `fact` text and `name` (the relation), and
   resolves its source episodes.
3. Returns a `RecallAnswer`. If search raises (for example FTS unavailable), it
   returns an empty answer. Recall never raises.

**Provenance resolution** (`_resolve_sources`) looks up each episode uuid via
`EpisodicNode.get_by_uuid`, using the episode name as the label and pulling the
canonical URL out of the episode body. The URL extractor reads the `pull_request`
or `issue` object's `url` field, ignoring the repo URL (which is not the source of
the fact). If a lookup fails it falls back to the uuid string, so provenance
degrades rather than disappears. Duplicate labels are dropped, order preserved.

`format_answer` renders the answer as a cited block: each fact on its own line,
each source indented beneath it with its URL.

## Query: people, with a fallback

[`queries.py`](../src/relic/graph/queries.py) is a narrower path used by
`relic query`. It finds people connected to work matching the text and returns
`ReviewerHit`s (name, relation, fact, episodes, profile_url).

`reviewers_of` tries Graphiti hybrid search first. For each result edge it loads
both endpoint nodes and keeps the ones labeled `Person`. If search returns nothing
or raises, it falls back to a deterministic Cypher walk over `RELATES_TO` edges
whose name is `REVIEWED` or `AUTHORED`, filtered by `group_id` and a case-
insensitive substring match on the fact, work name, or summary. The fallback means
the query still returns something when search is cold or unavailable. Like recall,
it never raises.

## Eval: the recall scorecard

[`scorecard.py`](../src/relic/scorecard.py) measures recall quality as a small set
of numbers. A gold set ([`eval/github_recall.json`](../eval/github_recall.json))
names a repo and a list of cases, each a question plus the PR or issue numbers
whose content answers it.

`relic eval` runs recall for each question, then `score_case` walks the recalled
facts in ranked order and records which expected PR or issue URLs were cited and how
highly. A match is an exact URL match. From that, `summarize` reports three
aggregates plus a per-case line:

- **Hit rate.** Share of cases where at least one expected source was cited. The
  coarsest signal, and what the scorecard reported originally.
- **MRR.** Mean reciprocal rank of the first fact that cites an expected source.
  Rank is counted in facts, the unit recall ranks and reranking reorders, so this
  is the number to tune recall reranking against.
- **Coverage.** For cases that expect several sources, the mean share found. One
  hit out of two expected PRs is half coverage, not a clean pass.

There is no LLM judge. Scoring is a deterministic URL comparison, so the ruler
costs nothing beyond the recall calls it measures. Use it to tell whether an
ingestion or retrieval change actually moved recall instead of eyeballing answers.

## The FalkorDB workaround

[`engram.py`](../src/relic/graph/engram.py) monkeypatches two graphiti-core
fulltext-query builders at `make_engram` time (`_patch_falkordb_empty_query`,
applied once). It fixes two issues in graphiti-core 0.29.x on FalkorDB:

1. **Empty fulltext queries.** When an extracted entity's name sanitizes to
   nothing (all punctuation or stopwords, common in real history: version tags,
   file paths, single symbols), the builder emits a query with empty trailing
   parens that RediSearch rejects, aborting `add_episode`. The patch returns an
   empty string instead, which Graphiti already treats as "skip the fulltext
   search."
2. **Hyphens in group_ids.** RediSearch treats `-` as a negation operator even
   inside quotes, so a `group_id` like `relic-core` breaks the query. The patch
   escapes hyphens in the `group_id` filter on the final query string.

Both are flagged to remove once upstream guards them. They matter because a slug
like `buildrelic__relic-core` contains a hyphen, and real repos produce entity
names that sanitize to empty.

## History: Kuzu to FalkorDB

Memory storage was migrated from embedded Kuzu to FalkorDB. Kuzu was zero-infra
but had two blockers: custom `group_id` partitioning crashed `add_episode` (its
driver lacked the multi-database support the path needs), and full-text search did
not work the way recall needs. FalkorDB gives both: per-repo graphs by `group_id`
and working RediSearch fulltext. The cost is that FalkorDB is a networked service
you must run (Docker locally), where Kuzu was a file. The recall and query code
did not change across the migration, because both are built on the portable
`graphiti.search` API. See [roadmap.md](roadmap.md).
