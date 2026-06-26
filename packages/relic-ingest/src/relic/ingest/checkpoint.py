"""Ingest checkpoint: a per-repo ledger of episodes already landed in the graph.

Re-running ingest is otherwise additive: Graphiti mints a fresh uuid per
``add_episode``, so the same PR loaded twice becomes two episodes. The checkpoint
records the name of every episode that loaded cleanly, keyed by ``group_id``. A
re-run reads the ledger and skips what already landed, so it resumes a run cut
short by a failure instead of re-paying the LLM cost and duplicating nodes.

The key is the episode name (``PR owner/name#42``, ``Issue REL-10``), which the
mappers make deterministic and unique per item. Each name carries a freshness
*token* (a content fingerprint computed by the loader): a re-run skips a name whose
token is unchanged and re-extracts one whose token moved, so an edited source
artifact actually refreshes its graph footprint instead of being silently skipped.
The ledger is append-only and flushed per line, so a crash mid-run keeps the
progress made up to that point; on a token change the new line wins (last write).
Clearing the graph for a repo and clearing its checkpoint go together: ``ingest
--fresh`` does the second, and reloads everything.
"""

from __future__ import annotations

from pathlib import Path

_BASE = "data/ingest"


def checkpoint_path(group_id: str, base: Path | str = _BASE) -> Path:
    """Path to the checkpoint ledger for ``group_id``: ``<base>/<group_id>.log``."""
    return Path(base) / f"{group_id}.log"


def load_done(path: Path) -> dict[str, str | None]:
    """Map each landed episode name to its freshness token, empty if the ledger is absent.

    The token lets the loader supersede an edited episode (token moved) while skipping an
    unchanged one. Tolerates legacy name-only lines (no tab) -> token ``None`` meaning
    "landed, token unknown", which the loader treats as skip (never supersede), so
    upgrading an old ledger never forces a re-ingest. Last write wins per name, so a
    superseded name's newest token is the one returned.
    """
    if not path.exists():
        return {}
    out: dict[str, str | None] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        name, sep, token = raw.partition("\t")
        out[name] = token if sep else None
    return out


def record_done(path: Path, name: str, token: str | None = None) -> None:
    """Append one episode name (and optional freshness token) to the ledger.

    Opens, writes, and flushes per call so an interrupted run keeps what it had
    already recorded. ``token=None`` writes the legacy name-only shape. Names and
    tokens never contain tabs or newlines, so one ``name\\ttoken`` line is safe.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = f"{name}\t{token}\n" if token is not None else f"{name}\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()


def compact(path: Path) -> None:
    """Rewrite the ledger to one line per name (newest token wins), bounding its growth.

    Supersession re-records a name on every content change, so the append-only log grows
    without bound on a long-lived repo. ``load_done`` already keeps the last line per name;
    this rewrites the file to match. A no-op when the ledger is absent or already compact
    (one line per name), so it is cheap to call after every run.
    """
    if not path.exists():
        return
    nonblank = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    done = load_done(path)
    if len(nonblank) == len(done):
        return  # already one line per name
    lines = [
        f"{name}\t{token}\n" if token is not None else f"{name}\n" for name, token in done.items()
    ]
    path.write_text("".join(lines), encoding="utf-8")


def clear(path: Path) -> None:
    """Delete the ledger so the next run reloads everything. Safe if absent."""
    path.unlink(missing_ok=True)
