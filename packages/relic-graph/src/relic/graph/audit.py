"""Zone-integrity audit (ADR-0006 Decision 1): is the graph well-tagged?

The write path refuses to persist a Zoned node or edge with no Zone, so a graph built
entirely under that rule is sound by construction. This audit is the safety net for data
that predates the rule (or arrives by another path): a read-only scan that flags every
Zoned node and edge missing a ``group_id``, so a graph can be *proven* clean or repaired.
It is the single most useful diagnostic to hand a downstream developer — "run this; if it
is clean, the access boundary holds."

It reaches the graph through the ``execute_read`` introspection hatch (the same one the
eval tools use), not the typed reader, because it must enumerate raw nodes and edges the
typed seam deliberately does not expose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, LiteralString, Protocol

from relic.graph.schema import GLOBAL_ENTITY_TYPES


class SupportsExecuteRead(Protocol):
    """The read-only Cypher hatch the audit needs (``GraphitiMemory`` satisfies it)."""

    async def execute_read(self, cypher: LiteralString, **params: Any) -> list[dict[str, Any]]: ...


@dataclass(slots=True, frozen=True)
class ZoneViolation:
    """One node or edge that breaks Zone integrity: it is Zoned but carries no Zone."""

    kind: str  # "node" or "edge"
    uuid: str
    detail: str


@dataclass(slots=True, frozen=True)
class ZoneAuditReport:
    """The result of a Zone-integrity scan: the untagged Zoned nodes and edges found."""

    untagged_nodes: list[ZoneViolation] = field(default_factory=list)
    untagged_edges: list[ZoneViolation] = field(default_factory=list)
    truncated: bool = False  # a scan hit the row limit; more violations may exist

    @property
    def violations(self) -> list[ZoneViolation]:
        return [*self.untagged_nodes, *self.untagged_edges]

    @property
    def is_clean(self) -> bool:
        return not self.untagged_nodes and not self.untagged_edges


# A Zoned node with no Zone: an Entity carrying none of the global-tier labels (those are
# Zone-exempt) and an empty/absent group_id. A global-only node legitimately has no Zone.
_UNTAGGED_NODES_CYPHER: LiteralString = """
    MATCH (n:Entity)
    WHERE (n.group_id IS NULL OR n.group_id = '')
      AND NOT any(label IN labels(n) WHERE label IN $global_labels)
    RETURN n.uuid AS uuid, labels(n) AS labels
    LIMIT $limit
"""

# Every fact (edge) is Zoned, so any RELATES_TO with no group_id is a violation outright.
_UNTAGGED_EDGES_CYPHER: LiteralString = """
    MATCH ()-[r:RELATES_TO]->()
    WHERE r.group_id IS NULL OR r.group_id = ''
    RETURN r.uuid AS uuid, r.name AS relation
    LIMIT $limit
"""


def _labels_of(row: dict[str, Any]) -> str:
    labels = [str(x) for x in (row.get("labels") or []) if x != "Entity"]
    return "/".join(labels) or "Entity"


async def audit_zone_integrity(
    memory: SupportsExecuteRead, *, limit: int = 1000
) -> ZoneAuditReport:
    """Scan for Zoned nodes and edges that carry no Zone. Read-only; never mutates.

    ``limit`` caps each scan so a pathological graph cannot exhaust memory; a hit on the
    cap sets ``truncated`` so the result never reads as "clean" when it merely stopped
    early. A clean report is the proof that the access boundary is structurally sound.
    """
    global_labels = sorted(GLOBAL_ENTITY_TYPES)
    node_rows = await memory.execute_read(
        _UNTAGGED_NODES_CYPHER, global_labels=global_labels, limit=limit
    )
    edge_rows = await memory.execute_read(_UNTAGGED_EDGES_CYPHER, limit=limit)
    nodes = [
        ZoneViolation(
            kind="node", uuid=str(r.get("uuid", "")), detail=f"untagged {_labels_of(r)} node"
        )
        for r in node_rows
    ]
    edges = [
        ZoneViolation(
            kind="edge",
            uuid=str(r.get("uuid", "")),
            detail=f"untagged {r.get('relation') or 'RELATES_TO'} edge",
        )
        for r in edge_rows
    ]
    return ZoneAuditReport(
        untagged_nodes=nodes,
        untagged_edges=edges,
        truncated=len(node_rows) >= limit or len(edge_rows) >= limit,
    )
