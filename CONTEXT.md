# Relic

The domain language for relic's memory layer: capturing team work, building a
temporal knowledge graph from it, and recalling facts with their sources.

## Language

**Memory**:
The temporal knowledge graph of what the team knows and decided: people, repos,
pull requests, reviews, issues, and the edges between them. Also the seam callers
read from and write to.
_Avoid_: engram, graph, store

**Capture**:
Feeding team work into memory as episodes, from which entities and relationships
are extracted.
_Avoid_: import

**Episode**:
One captured unit of work fed into memory (a pull request, an issue, a thread).
The unit extraction runs over.
_Avoid_: document, record, event

**Recall**:
Querying memory and getting back facts, each carrying the source it came from.
_Avoid_: lookup, search (when you mean recall)

**Provenance**:
The source an answer cites: the episode, and its URL, a recalled fact came from.
The product line "stored isn't remembered" is about lacking this.
_Avoid_: origin
