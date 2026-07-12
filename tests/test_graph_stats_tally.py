"""Unit tests for graph_stats' expected-edge tally (REL-94 coverage measurement).

Pure-function tests: the tally reads exact body data (author login, reviewers, files,
linked issues, assignees, parent), so its counts must mirror the episode-body contract
precisely -- coverage below 1.0 is then attributable to extraction, never to the meter.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_graph_stats():
    """Import eval/graph_stats.py by path (eval/ is a tools dir, not a package)."""
    path = Path(__file__).resolve().parent.parent / "eval" / "graph_stats.py"
    spec = importlib.util.spec_from_file_location("graph_stats", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tally(body: dict) -> dict[str, int]:
    gs = _load_graph_stats()
    expected = dict.fromkeys(gs._COVERAGE_EDGES, 0)
    gs._tally_expected_edges(body, expected)
    return expected


def test_pr_body_tallies_every_exactly_derivable_edge() -> None:
    expected = _tally(
        {
            "source_type": "pull_request",
            "pull_request": {
                "author": {"login": "alice"},
                "co_authors": [{"name": "carol", "email": "c@x.io"}],
            },
            "reviews": [
                {"reviewer": {"login": "bob"}, "state": "CHANGES_REQUESTED"},
                {"reviewer": {"login": "bob"}, "state": "APPROVED"},  # re-review: one edge
                {"reviewer": None, "state": "APPROVED"},  # ghost reviewer: no Person
            ],
            "requested_reviewers": [
                {"name": "dana", "kind": "user"},
                {"name": "core-team", "kind": "team"},  # team request: no Person node
            ],
            "files": [{"path": "a.py"}, {"path": "b.py"}],
            "linked_issues": [{"identifier": "demo/repo#7", "relation": "closes"}],
        }
    )
    assert expected == {
        "AUTHORED": 2,  # author + one co-author
        "REVIEWED": 1,  # distinct reviewers, ghosts dropped
        "REQUESTED_REVIEW": 1,  # person-kind only
        "TOUCHES_PATH": 2,
        "CLOSES": 1,
        "ASSIGNED_TO": 0,
        "PARENT_OF": 0,
    }


def test_issue_body_tallies_assignees_and_parent() -> None:
    expected = _tally(
        {
            "source_type": "issue",
            "issue": {"assignees": ["alice", "bob"], "parent": "demo/repo#1"},
        }
    )
    assert expected["ASSIGNED_TO"] == 2
    assert expected["PARENT_OF"] == 1
    assert expected["AUTHORED"] == 0


def test_ghost_author_and_missing_sections_tally_zero() -> None:
    # A ghost/deleted PR opener has author: null; an empty body must count nothing.
    assert _tally({"pull_request": {"author": None}})["AUTHORED"] == 0
    assert all(n == 0 for n in _tally({}).values())
