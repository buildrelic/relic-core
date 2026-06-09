"""Unit tests for the pure helpers in the GitHub connector (no network)."""

from relic.ingest.github import _pr_state


def test_pr_state_merged_takes_precedence() -> None:
    # merged_at set wins even though GitHub still reports state "closed".
    assert _pr_state({"state": "closed", "merged_at": "2025-01-02T00:00:00Z"}) == "merged"


def test_pr_state_closed_without_merge() -> None:
    assert _pr_state({"state": "closed", "merged_at": None}) == "closed"


def test_pr_state_draft() -> None:
    assert _pr_state({"state": "open", "draft": True}) == "draft"


def test_pr_state_open() -> None:
    assert _pr_state({"state": "open", "draft": False}) == "open"


def test_pr_state_closed_draft_is_closed_not_draft() -> None:
    # A draft that was closed without ever merging is closed; closure outranks draft.
    assert _pr_state({"state": "closed", "draft": True, "merged_at": None}) == "closed"
