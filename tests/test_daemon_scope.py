"""Per-session scoping: resolve a session's repo from its working dir's git origin.

The daemon serves many repos, so each inject/capture scopes to the repo the session is
actually in (derived from cwd), falling back to the daemon's boot default otherwise.
"""

import subprocess

from relic.cli import _repo_from_cwd, _scope_for_cwd


def _git_repo(path, url):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", url], cwd=path, check=True)


def test_repo_from_cwd_parses_ssh_and_https(tmp_path):
    cases = {
        "ssh": ("git@github.com:buildrelic/relic-core.git", "buildrelic/relic-core"),
        "https_git": ("https://github.com/buildrelic/relic-core.git", "buildrelic/relic-core"),
        "https_bare": ("https://github.com/buildrelic/relic-core", "buildrelic/relic-core"),
        # host routing is case-insensitive, so the slug is lowercased and stable
        "mixed_case": ("https://github.com/BuildRelic/Relic-Core.git", "buildrelic/relic-core"),
    }
    for name, (url, expect) in cases.items():
        d = tmp_path / name
        d.mkdir()
        _git_repo(d, url)
        assert _repo_from_cwd(str(d)) == expect


def test_repo_from_cwd_none_without_git(tmp_path):
    assert _repo_from_cwd(str(tmp_path)) is None  # a dir with no git
    assert _repo_from_cwd("") is None
    assert _repo_from_cwd("/no/such/dir") is None


def test_scope_for_cwd_resolved_and_fallback(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    _git_repo(d, "git@github.com:owner/name.git")
    # a resolved repo scopes both the engram database and the recall group to its slug
    assert _scope_for_cwd(str(d), "default_db") == ("owner__name", "owner__name")
    # an unresolved cwd fails closed to the daemon's concrete default for BOTH, so recall
    # and capture agree on a group and recall never reads the whole graph unfiltered
    assert _scope_for_cwd("", "default_db") == ("default_db", "default_db")
    assert _scope_for_cwd("/no/such", "default_db") == ("default_db", "default_db")
