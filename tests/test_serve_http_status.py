"""serve-http status providers: per-source connector status + repo-scoped run totals.

These synthesize the web app's connector/ingest panels from the on-disk checkpoint
ledgers, so the per-source and per-repo accounting has to be exact: the web app fans
out one /v1/ingest/runs call per connected repo and sums totals.memories.
"""

import json
import os

from relic.cli import _connector_status, _ingest_runs
from relic.ingest import checkpoint_path, record_done, repo_group_id


def _ledger(repo: str, *names: str):
    """Write a per-repo checkpoint ledger under the cwd's data/ingest and return its path."""
    path = checkpoint_path(repo_group_id(repo))
    for name in names:
        record_done(path, name)
    return path


def test_connector_status_counts_prs_not_github_issues(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    # repoA: one PR plus one github issue (name has '#'); repoB: two PRs.
    _ledger("owner/repoA", "PR owner/repoA#1", "Issue owner/repoA#2")
    _ledger("owner/repoB", "PR owner/repoB#1", "PR owner/repoB#2")

    github = next(c for c in _connector_status()["connectors"] if c["source"] == "github")
    # three PRs total; the github issue does not inflate the "pull requests" count.
    assert github["itemCount"] == 3
    assert github["itemLabel"] == "pull requests"
    assert github["repos"] == ["owner/repoA", "owner/repoB"]


def test_connector_status_partitions_repos_by_source(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    # repoA is github-only; repoB has a github PR and a linear issue.
    _ledger("owner/repoA", "PR owner/repoA#1")
    _ledger("owner/repoB", "PR owner/repoB#1", "Issue REL-5")

    connectors = {c["source"]: c for c in _connector_status()["connectors"]}
    # the linear connector lists only the repo with linear items, not the github-only one.
    assert connectors["linear"]["itemCount"] == 1
    assert connectors["linear"]["repos"] == ["owner/repoB"]
    assert "owner/repoA" not in connectors["linear"]["repos"]
    assert connectors["github"]["repos"] == ["owner/repoA", "owner/repoB"]


def test_connector_status_last_sync_is_per_source(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    gh = _ledger("owner/ghrepo", "PR owner/ghrepo#1")
    lin = _ledger("owner/linrepo", "Issue REL-1")
    # linear ledger is the more recently touched one.
    os.utime(gh, (1_000_000, 1_000_000))
    os.utime(lin, (2_000_000, 2_000_000))

    connectors = {c["source"]: c for c in _connector_status()["connectors"]}
    # the newer linear sync must not bleed into github's lastSyncAt.
    assert connectors["github"]["lastSyncAt"] != connectors["linear"]["lastSyncAt"]
    assert connectors["github"]["lastSyncAt"].startswith("1970-01-12")
    assert connectors["linear"]["lastSyncAt"].startswith("1970-01-24")


def test_connector_status_surfaces_granola_row(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    # two meetings under one owner-scope, plus a github repo alongside it
    _ledger("owner/repoA", "PR owner/repoA#1")
    gran = checkpoint_path("granola__zidan_tryrelic_io")
    for name in ("Meeting abc123", "Meeting def456"):
        record_done(gran, name)

    connectors = {c["source"]: c for c in _connector_status()["connectors"]}
    granola = connectors["granola"]
    assert granola["itemCount"] == 2
    assert granola["itemLabel"] == "meetings"
    assert granola["repos"] == []  # meetings have no repo scope
    assert granola["connected"] is True  # landed meetings count as connected
    # the github card is unaffected by the granola ledger living alongside it
    assert connectors["github"]["repos"] == ["owner/repoA"]


def test_connector_status_granola_ledger_not_treated_as_repo(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    gran = checkpoint_path("granola__zidan_tryrelic_io")
    record_done(gran, "Meeting abc123")

    connectors = {c["source"]: c for c in _connector_status()["connectors"]}
    # a granola ledger must never be slugified into a fake "granola/<email>" repo on
    # the github or linear cards.
    assert all("granola" not in repo for repo in connectors["github"]["repos"])
    assert all("granola" not in repo for repo in connectors["linear"]["repos"])


def test_ingest_runs_totals_are_repo_scoped(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _ledger("owner/repoA", "PR owner/repoA#1", "PR owner/repoA#2")
    _ledger("owner/repoB", "PR owner/repoB#1", "PR owner/repoB#2", "PR owner/repoB#3")
    runs_path = tmp_path / "data" / "ingest" / "runs.jsonl"
    runs_path.write_text(
        json.dumps({"id": "a1", "repo": "owner/repoA"})
        + "\n"
        + json.dumps({"id": "b1", "repo": "owner/repoB"})
        + "\n",
        encoding="utf-8",
    )

    scoped = _ingest_runs("owner/repoA", 50)
    # totals follow the repo filter: repoA's two episodes, not the global five.
    assert scoped["totals"]["memories"] == 2
    assert [r["repo"] for r in scoped["runs"]] == ["owner/repoA"]

    glob = _ingest_runs(None, 50)
    assert glob["totals"]["memories"] == 5


def test_ingest_runs_filters_by_source(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    # a github repo ledger plus a granola owner-scope ledger
    _ledger("owner/repoA", "PR owner/repoA#1", "PR owner/repoA#2")
    gran = checkpoint_path("granola__zidan_tryrelic_io")
    for name in ("Meeting a", "Meeting b", "Meeting c"):
        record_done(gran, name)
    runs_path = tmp_path / "data" / "ingest" / "runs.jsonl"
    runs_path.write_text(
        json.dumps({"id": "gh1", "repo": "owner/repoA", "source": "GitHub"})
        + "\n"
        + json.dumps({"id": "gr1", "repo": None, "source": "Granola"})
        + "\n",
        encoding="utf-8",
    )

    granola = _ingest_runs(None, 50, "granola")
    # case-insensitive match on the stored "Granola"; the github run is filtered out
    assert [r["id"] for r in granola["runs"]] == ["gr1"]
    # totals scope to the granola ledgers (3 meetings), not the global five
    assert granola["totals"]["memories"] == 3

    # a per-repo github call (what the web app fans out) still drops the repo-less granola run
    gh = _ingest_runs("owner/repoA", 50)
    assert [r["id"] for r in gh["runs"]] == ["gh1"]
    assert gh["totals"]["memories"] == 2


def test_connector_status_surfaces_notion_row(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    # two pages under one workspace-scope, plus a github repo alongside it
    _ledger("owner/repoA", "PR owner/repoA#1")
    notion = checkpoint_path("notion__ws-1")
    for name in ("Notion pa", "Notion pb"):
        record_done(notion, name)

    connectors = {c["source"]: c for c in _connector_status()["connectors"]}
    row = connectors["notion"]
    assert row["itemCount"] == 2
    assert row["itemLabel"] == "pages"
    assert row["repos"] == []  # pages have no repo scope
    assert row["connected"] is True  # landed pages count as connected
    # the github card is unaffected by the notion ledger living alongside it
    assert connectors["github"]["repos"] == ["owner/repoA"]


def test_connector_status_notion_ledger_not_treated_as_repo(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    notion = checkpoint_path("notion__ws-1")
    record_done(notion, "Notion pa")

    connectors = {c["source"]: c for c in _connector_status()["connectors"]}
    # a notion ledger must never be slugified into a fake "notion/<ws>" repo on another card.
    assert all("notion" not in repo for repo in connectors["github"]["repos"])
    assert all("notion" not in repo for repo in connectors["linear"]["repos"])


def test_ingest_runs_filters_by_notion_source(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _ledger("owner/repoA", "PR owner/repoA#1", "PR owner/repoA#2")
    notion = checkpoint_path("notion__ws-1")
    for name in ("Notion a", "Notion b", "Notion c"):
        record_done(notion, name)
    runs_path = tmp_path / "data" / "ingest" / "runs.jsonl"
    runs_path.write_text(
        json.dumps({"id": "gh1", "repo": "owner/repoA", "source": "GitHub"})
        + "\n"
        + json.dumps({"id": "nt1", "repo": None, "source": "Notion"})
        + "\n",
        encoding="utf-8",
    )

    notion_runs = _ingest_runs(None, 50, "notion")
    # case-insensitive match on the stored "Notion"; the github run is filtered out
    assert [r["id"] for r in notion_runs["runs"]] == ["nt1"]
    # totals scope to the notion ledgers (3 pages), not the global five
    assert notion_runs["totals"]["memories"] == 3
