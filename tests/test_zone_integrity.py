"""Zone integrity (ADR-0006 Decision 1): refuse untagged writes, audit the graph.

``require_episode_zone`` is the fail-closed write guard; ``audit_zone_integrity`` is the
read-time safety net that proves an existing graph is well-tagged. The audit is exercised
against a stub ``execute_read`` so it needs no FalkorDB.
"""

from typing import Any

import pytest

from relic.graph import ZoneIntegrityError, audit_zone_integrity, require_episode_zone


@pytest.mark.parametrize("bad", [None, "", "   "])
def test_require_episode_zone_rejects_a_zoneless_write(bad: str | None) -> None:
    with pytest.raises(ZoneIntegrityError):
        require_episode_zone(bad)


def test_require_episode_zone_returns_the_trimmed_zone() -> None:
    assert require_episode_zone("  team-a  ") == "team-a"


class _AuditStub:
    """A minimal SupportsExecuteRead: returns canned node/edge rows, records the calls."""

    def __init__(self, node_rows: list[dict], edge_rows: list[dict]) -> None:
        self.node_rows = node_rows
        self.edge_rows = edge_rows
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute_read(self, cypher: str, **params: Any) -> list[dict]:
        self.calls.append((cypher, params))
        # The node scan is the only query that filters on a node's labels.
        return self.node_rows if "labels(n)" in cypher else self.edge_rows


async def test_audit_clean_graph_is_clean() -> None:
    report = await audit_zone_integrity(_AuditStub([], []))
    assert report.is_clean
    assert report.violations == []


async def test_audit_reports_untagged_nodes_and_edges() -> None:
    stub = _AuditStub(
        node_rows=[{"uuid": "n1", "labels": ["PullRequest", "Entity"]}],
        edge_rows=[{"uuid": "e1", "relation": "REVIEWED"}],
    )
    report = await audit_zone_integrity(stub)
    assert not report.is_clean
    assert len(report.violations) == 2
    assert report.untagged_nodes[0].uuid == "n1"
    assert "PullRequest" in report.untagged_nodes[0].detail
    assert report.untagged_edges[0].uuid == "e1"
    assert "REVIEWED" in report.untagged_edges[0].detail


async def test_audit_excludes_the_global_tier_from_the_node_scan() -> None:
    stub = _AuditStub([], [])
    await audit_zone_integrity(stub)
    node_call = next(c for c in stub.calls if "labels(n)" in c[0])
    assert set(node_call[1]["global_labels"]) == {"Person", "Repo", "Label", "File"}


async def test_audit_flags_truncation_at_the_row_limit() -> None:
    # Exactly `limit` rows means the scan may have stopped early -- never report "clean".
    stub = _AuditStub(node_rows=[{"uuid": "n1", "labels": ["Issue"]}], edge_rows=[])
    report = await audit_zone_integrity(stub, limit=1)
    assert report.truncated
