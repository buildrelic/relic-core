"""Ingest: pull engineering history from GitHub and Linear.

Public API for the ingest subsystem. Other subsystems import only what is listed
in ``__all__`` (``from relic.ingest import ...``), never an internal module. The
episode contract ``EpisodeSpec`` is not here: it lives in ``relic.contracts``, so
graph consumes it without importing ingest.
"""

from relic.ingest.checkpoint import checkpoint_path, clear, load_done, record_done
from relic.ingest.github import fetch_repo, make_github, resolve_github_token
from relic.ingest.linear import fetch_issues, linear_enabled
from relic.ingest.mappers import (
    FileChange,
    IssueRec,
    PullRequestRec,
    RepoBundle,
    ReviewRec,
    issue_to_episode,
    pr_to_episode,
    repo_group_id,
)
from relic.ingest.raw_store import dump_raw, raw_path

__all__ = [
    "FileChange",
    "IssueRec",
    "PullRequestRec",
    "RepoBundle",
    "ReviewRec",
    "checkpoint_path",
    "clear",
    "dump_raw",
    "fetch_issues",
    "fetch_repo",
    "issue_to_episode",
    "linear_enabled",
    "load_done",
    "make_github",
    "pr_to_episode",
    "raw_path",
    "record_done",
    "repo_group_id",
    "resolve_github_token",
]
