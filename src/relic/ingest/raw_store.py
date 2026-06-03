"""Raw store: dump original payloads to ./data/raw/<source>/<id>.json.

Every compiled skill can then cite an immutable source artifact (becomes R2 later).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def raw_path(base: Path, source: str, ident: str) -> Path:
    """Path for a raw artifact: <base>/<source>/<ident>.json."""
    return base / source / f"{ident}.json"


def dump_raw(
    payload: dict[str, Any],
    *,
    source: str,
    ident: str,
    base: Path | str = "data/raw",
) -> Path:
    """Write `payload` to <base>/<source>/<ident>.json and return the path."""
    path = raw_path(Path(base), source, ident)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path
