"""Ingest checkpoint: the per-repo ledger of episodes already landed."""

from relic.ingest.checkpoint import checkpoint_path, clear, load_done, record_done


def test_path_is_group_id_under_base(tmp_path) -> None:
    path = checkpoint_path("buildrelic__relic-core", base=tmp_path)
    assert path == tmp_path / "buildrelic__relic-core.log"


def test_missing_ledger_reads_as_empty(tmp_path) -> None:
    assert load_done(checkpoint_path("demo__repo", base=tmp_path)) == {}


def test_record_then_load_roundtrips(tmp_path) -> None:
    path = checkpoint_path("demo__repo", base=tmp_path)
    record_done(path, "PR demo/repo#1")
    record_done(path, "Issue REL-10")
    # No token given -> legacy name-only lines, which read back as token None.
    assert load_done(path) == {"PR demo/repo#1": None, "Issue REL-10": None}


def test_token_roundtrips(tmp_path) -> None:
    path = checkpoint_path("demo__repo", base=tmp_path)
    record_done(path, "Meeting not_a", "abc123")
    record_done(path, "PR demo/repo#1")  # legacy, no token
    assert load_done(path) == {"Meeting not_a": "abc123", "PR demo/repo#1": None}


def test_last_write_wins_on_supersede(tmp_path) -> None:
    # A superseded episode just re-records with its new token; the newest line wins.
    path = checkpoint_path("demo__repo", base=tmp_path)
    record_done(path, "Meeting not_a", "tok1")
    record_done(path, "Meeting not_a", "tok2")
    assert load_done(path) == {"Meeting not_a": "tok2"}


def test_duplicate_names_collapse(tmp_path) -> None:
    path = checkpoint_path("demo__repo", base=tmp_path)
    record_done(path, "PR demo/repo#1")
    record_done(path, "PR demo/repo#1")
    assert load_done(path) == {"PR demo/repo#1": None}


def test_clear_removes_ledger_and_is_safe_when_absent(tmp_path) -> None:
    path = checkpoint_path("demo__repo", base=tmp_path)
    record_done(path, "PR demo/repo#1")
    clear(path)
    assert not path.exists()
    assert load_done(path) == {}
    clear(path)  # no error on a missing ledger
