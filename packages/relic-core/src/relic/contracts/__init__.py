"""Relic contracts: the interface layer between owned subsystems.

The types here are the seams across which ingest, graph, and serve interact. A
consumer depends on a contract, never on the producing subsystem; the producer
fills it; the composition root (``relic.cli``) wires them together. ``SkillIR``
and its parts are re-exported from the ontology so every subsystem imports one
canonical name: ``from relic.contracts import SkillIR``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from relic.contracts.episode import EpisodeSpec
from relic.ontology import Citation, FieldSpec, SkillIR

# Recall as an injected function, so serve carries no graph dependency:
# (query, num_results) -> a formatted, cited answer block. graph supplies the
# implementation; the composition root injects it into serve.
RecallFn = Callable[[str, int], Awaitable[str]]

__all__ = [
    "Citation",
    "EpisodeSpec",
    "FieldSpec",
    "RecallFn",
    "SkillIR",
]
