import json
from pathlib import Path

from relic.ingest.raw_store import dump_raw, raw_path


def test_dump_raw_writes_and_roundtrips(tmp_path: Path) -> None:
    payload = {"number": 4, "title": "Gcp setup", "nested": {"reviewers": ["paris-phan"]}}
    path = dump_raw(payload, source="github", ident="pr-4", base=tmp_path)
    assert path == raw_path(tmp_path, "github", "pr-4")
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8")) == payload
