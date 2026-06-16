"""Serve: deliver verified skills to coding agents.

Public API for the serve subsystem: the MCP server, the file emitter, the catalog
index, and the SKILL.md renderer. Other subsystems and the composition root import
``from relic.serve import ...``. Serve depends only on ``relic.contracts`` and its
own internals (registry, render) plus an injected ``RecallFn``: it never imports
ingest or graph.
"""

from relic.serve.catalog import render_catalog
from relic.serve.emit_files import (
    emit_catalog,
    emit_skill,
    emit_verified,
    prune_unverified,
    skill_dir,
    skill_path,
)
from relic.serve.mcp_server import build_server, input_schema
from relic.serve.render import render

__all__ = [
    "build_server",
    "emit_catalog",
    "emit_skill",
    "emit_verified",
    "input_schema",
    "prune_unverified",
    "render",
    "render_catalog",
    "skill_dir",
    "skill_path",
]
