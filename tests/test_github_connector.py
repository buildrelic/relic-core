"""Unit tests for the pure helpers in the GitHub connector (no network)."""

from relic.ingest.github import _parent_identifier, _pr_state


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


def test_parent_identifier_parses_subissue_url() -> None:
    # GitHub returns the parent's REST URL inline on a sub-issue.
    url = "https://api.github.com/repos/astral-sh/uv/issues/18506"
    assert _parent_identifier(url) == "astral-sh/uv#18506"


def test_parent_identifier_none_for_top_level_issue() -> None:
    assert _parent_identifier(None) is None


def test_parent_identifier_none_on_unparseable_url() -> None:
    assert _parent_identifier("https://example.com/not-an-issue") is None
