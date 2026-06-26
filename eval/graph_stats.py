"""Graph-structure measurement harness: the gate for validated ingestion waves.

End-to-end skill quality cannot be measured until the Phase 4 detector and compiler
land, so this measures the proxies that predict it. Run it on a real repo's graph
before and after an ingestion change and diff the numbers, so a change is shown to
lift extraction quality rather than assumed to.

It is read-only: it opens the repo's graph through the Memory seam's eval-only
``execute_read`` escape hatch (ADR-0003) and runs Cypher counts, never a write. It needs
FalkorDB up and the repo already ingested.

    uv run python eval/graph_stats.py --repo owner/name [--area src/auth] [--json out.json]

Metrics:
  - structure        entities by type, edges by relation, episode count
  - dedup_forking    Person names that resolve to more than one node (support splits)
  - citation_integrity  share of PR/issue episodes whose body yields a citation URL
  - routing_probe    for --area, the people who reviewed/authored work touching it,
                     ranked by support: the closest proxy for review-routing skill quality

Placement note: this reads the graph (Abhinav's domain). It lives in eval/ as a
standalone tool, modeled on the Cypher idiom in relic.graph.queries. Fold it into a
`relic` subcommand once we agree where it belongs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import TYPE_CHECKING, Any, LiteralString

from relic.graph import open_memory
from relic.graph.recall import _extract_url
from relic.ingest import repo_group_id

if TYPE_CHECKING:
    from relic.graph import GraphitiMemory


async def _query(
    memory: GraphitiMemory, cypher: LiteralString, **params: Any
) -> list[dict[str, Any]]:
    """Run a read-only Cypher query via the seam's eval escape hatch. Empty on any failure."""
    try:
        return await memory.execute_read(cypher, **params)
    except Exception as exc:  # noqa: BLE001 - a schema/label mismatch is a zero result, not a crash
        print(f"  (query failed: {type(exc).__name__}: {exc})", file=sys.stderr)
        return []


async def structure(memory: GraphitiMemory, group_id: str) -> dict[str, Any]:
    """Entity-type counts, edge-relation counts, and the episode total."""
    entities = await _query(
        memory,
        """
        MATCH (n:Entity) WHERE n.group_id = $g
        UNWIND labels(n) AS label
        WITH label WHERE label <> 'Entity'
        RETURN label AS key, count(*) AS n ORDER BY n DESC
        """,
        g=group_id,
    )
    edges = await _query(
        memory,
        """
        MATCH (:Entity)-[r:RELATES_TO]->(:Entity) WHERE r.group_id = $g
        RETURN r.name AS key, count(*) AS n ORDER BY n DESC
        """,
        g=group_id,
    )
    episodes = await _query(
        memory,
        "MATCH (e:Episodic) WHERE e.group_id = $g RETURN count(*) AS n",
        g=group_id,
    )
    return {
        "entities_by_type": {row["key"]: row["n"] for row in entities},
        "edges_by_relation": {row["key"]: row["n"] for row in edges},
        "episode_count": episodes[0]["n"] if episodes else 0,
    }


async def dedup_forking(memory: GraphitiMemory, group_id: str) -> dict[str, Any]:
    """Person nodes whose normalized name appears more than once: support-splitting forks.

    A forked person splits AUTHORED/REVIEWED support across duplicates, so a real
    procedure may never clear the detector's threshold. A rising fork rate after a
    change is a regression even if raw counts look better.
    """
    rows = await _query(
        memory,
        """
        MATCH (p:Entity) WHERE p.group_id = $g AND 'Person' IN labels(p)
        WITH toLower(trim(p.name)) AS norm, count(*) AS n
        WHERE n > 1
        RETURN norm AS name, n ORDER BY n DESC
        """,
        g=group_id,
    )
    total_people = await _query(
        memory,
        "MATCH (p:Entity) WHERE p.group_id = $g AND 'Person' IN labels(p) RETURN count(*) AS n",
        g=group_id,
    )
    forked = sum(row["n"] - 1 for row in rows)
    people = total_people[0]["n"] if total_people else 0
    return {
        "person_nodes": people,
        "forked_names": [{"name": r["name"], "nodes": r["n"]} for r in rows],
        "fork_rate": round(forked / people, 3) if people else 0.0,
    }


async def citation_integrity(memory: GraphitiMemory, group_id: str) -> dict[str, Any]:
    """Share of PR/issue episodes whose body still yields a citation URL (invariant I3).

    This must stay at or near 1.0. A drop means a body-schema change moved the
    pull_request.url / issue.url key and silently broke every skill citation.
    """
    rows = await _query(
        memory,
        "MATCH (e:Episodic) WHERE e.group_id = $g RETURN e.content AS content",
        g=group_id,
    )
    total = len(rows)
    resolved = sum(1 for r in rows if _extract_url(r.get("content")))
    return {
        "episodes": total,
        "with_citation_url": resolved,
        "integrity": round(resolved / total, 3) if total else 0.0,
    }


async def routing_probe(memory: GraphitiMemory, group_id: str, area: str) -> dict[str, Any]:
    """People who reviewed/authored work touching ``area``, ranked by support.

    The closest proxy for review-routing skill quality: it is the shape of query the
    Phase 4 detector will run. The match is on the edge fact / work summary mentioning
    the area, which works on today's graph and sharpens as TOUCHES_PATH and file-area
    structure land. Deduped, countable results here mean a routing skill is mineable.
    """
    rows = await _query(
        memory,
        """
        MATCH (person:Entity)-[rel:RELATES_TO]->(work:Entity)
        WHERE rel.group_id = $g AND rel.name IN ['REVIEWED', 'AUTHORED']
          AND (
            toLower(coalesce(rel.fact, '')) CONTAINS toLower($area)
            OR toLower(coalesce(work.name, '')) CONTAINS toLower($area)
            OR toLower(coalesce(work.summary, '')) CONTAINS toLower($area)
          )
        RETURN person.name AS person, rel.name AS relation, count(*) AS support
        ORDER BY support DESC
        """,
        g=group_id,
        area=area,
    )
    return {
        "area": area,
        "candidates": [
            {"person": r["person"], "relation": r["relation"], "support": r["support"]}
            for r in rows
        ],
    }


async def run(repo: str, area: str | None) -> dict[str, Any]:
    group_id = repo_group_id(repo)
    memory = open_memory(database=group_id)
    try:
        report: dict[str, Any] = {
            "repo": repo,
            "group_id": group_id,
            "structure": await structure(memory, group_id),
            "dedup_forking": await dedup_forking(memory, group_id),
            "citation_integrity": await citation_integrity(memory, group_id),
        }
        if area:
            report["routing_probe"] = await routing_probe(memory, group_id, area)
        return report
    finally:
        await memory.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only graph-structure metrics for one repo.")
    parser.add_argument("--repo", required=True, help="owner/name; selects the per-repo graph")
    parser.add_argument("--area", default=None, help="path prefix for the routing probe")
    parser.add_argument("--json", dest="json_out", default=None, help="write the report here")
    args = parser.parse_args()

    report = asyncio.run(run(args.repo, args.area))
    text = json.dumps(report, indent=2)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            fh.write(text)
    print(text)


if __name__ == "__main__":
    main()
