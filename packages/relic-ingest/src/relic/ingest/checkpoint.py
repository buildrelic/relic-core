"""Ingest checkpoint: a per-repo ledger of episodes already landed in the graph.

Re-running ingest is otherwise additive: Graphiti mints a fresh uuid per
``add_episode``, so the same PR loaded twice becomes two episodes. The checkpoint
records the name of every episode that loaded cleanly, keyed by ``group_id``. A
re-run reads the ledger and skips what already landed, so it resumes a run cut
short by a failure instead of re-paying the LLM cost and duplicating nodes.

The key is the episode name (``PR owner/name#42``, ``Issue REL-10``), which the
mappers make deterministic and unique per item. The ledger is append-only and
flushed per line, so a crash mid-run keeps the progress made up to that point.
Clearing the graph for a repo and clearing its checkpoint go together: ``ingest
--fresh`` does the second, and reloads everything.
"""

from __future__ import annotations

from pathlib import Path

_BASE = "data/ingest"


def checkpoint_path(group_id: str, base: Path | str = _BASE) -> Path:
    """Path to the checkpoint ledger for ``group_id``: ``<base>/<group_id>.log``."""
    return Path(base) / f"{group_id}.log"


def load_done(path: Path) -> set[str]:
    """Return the set of episode names already landed, empty if the ledger is absent."""
    if not path.exists():
        return set()
    lines = path.read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip()}


def record_done(path: Path, name: str) -> None:
    """Append one episode name to the ledger, creating parent dirs as needed.

    Opens, writes, and flushes per call so an interrupted run keeps what it had
    already recorded. Names never contain newlines, so one per line is safe.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{name}\n")
        fh.flush()


def clear(path: Path) -> None:
    """Delete the ledger so the next run reloads everything. Safe if absent."""
    path.unlink(missing_ok=True)
