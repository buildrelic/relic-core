"""Control-plane Zone grants (ADR-0005): which Zones a Person may see.

The graph holds each node's Zone *tag*; these grants are the *authorization*, kept
deliberately out of the graph so an access check is never itself a graph traversal.
Lookups fail closed: an unknown principal resolves to no Zones, so a caller that uses
the result as the recall scope sees nothing rather than everything.
"""

from relic.registry import connect, grant_zone, revoke_zone, zones_for_person


def _conn():
    return connect(":memory:")


def test_unknown_person_sees_no_zones() -> None:
    # Fail closed: no grant rows -> empty set, not "all zones".
    assert zones_for_person(_conn(), "person_nobody") == set()


def test_grant_then_lookup_returns_zone() -> None:
    conn = _conn()
    grant_zone(conn, "person_alice", "team-a")
    assert zones_for_person(conn, "person_alice") == {"team-a"}


def test_grants_union_across_zones() -> None:
    conn = _conn()
    grant_zone(conn, "person_alice", "team-a")
    grant_zone(conn, "person_alice", "kb")
    assert zones_for_person(conn, "person_alice") == {"team-a", "kb"}


def test_grant_is_idempotent() -> None:
    conn = _conn()
    grant_zone(conn, "person_alice", "team-a")
    grant_zone(conn, "person_alice", "team-a", source="api")
    assert zones_for_person(conn, "person_alice") == {"team-a"}


def test_revoke_removes_only_that_grant() -> None:
    conn = _conn()
    grant_zone(conn, "person_alice", "team-a")
    grant_zone(conn, "person_alice", "kb")
    revoke_zone(conn, "person_alice", "team-a")
    assert zones_for_person(conn, "person_alice") == {"kb"}


def test_revoke_absent_grant_is_a_noop() -> None:
    conn = _conn()
    revoke_zone(conn, "person_alice", "team-a")  # never granted
    assert zones_for_person(conn, "person_alice") == set()


def test_grants_are_isolated_per_person() -> None:
    conn = _conn()
    grant_zone(conn, "person_alice", "classified")
    assert zones_for_person(conn, "person_bob") == set()
