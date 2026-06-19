"""Phase-timing summary for an ingest run: where the wall-clock went.

Almost all of an ingest's time is LLM extraction in the graph, not the GitHub
fetch or the deterministic mapping. This renders that split as a compact
terminal block so the shape is visible at the end of every ``relic ingest``,
instead of being inferable only from two scattered log lines.

Pure and string-only (no rich dependency, no I/O), so it is unit tested against
a fixed example. The CLI gathers the per-phase durations and prints the result.
"""

from __future__ import annotations

_BAR_WIDTH = 26
_TABLE_WIDTH = 64


def _bar(fraction: float, width: int = _BAR_WIDTH) -> str:
    """A proportional unicode bar, or a thin marker when the share rounds to zero."""
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * width)
    return "█" * filled if filled else "▏"


def format_ingest_timing(
    repo: str,
    phases: list[tuple[str, float]],
    total_seconds: float,
    *,
    loaded: int,
    skipped: int,
    failed: int,
    per_episode_seconds: float,
    bulk: bool,
) -> str:
    """Render an ingest run's phase breakdown (time and share of total).

    ``phases`` is an ordered list of ``(label, seconds)``. Shares are taken
    against ``total_seconds``, so the phases plus any untracked remainder still
    read true against the total rather than summing to a misleading 100%.
    """
    denom = total_seconds if total_seconds > 0 else 1e-9
    rule = "─" * _TABLE_WIDTH
    lines = [
        f"ingest timing: {repo} ({'bulk' if bulk else 'sequential'})",
        rule,
        f"{'phase':<20}{'time':>9}{'share':>9}   bar",
        rule,
    ]
    for label, seconds in phases:
        share = seconds / denom
        lines.append(f"{label:<20}{seconds:>8.1f}s{share * 100:>8.1f}%   {_bar(share)}")
    lines.append(rule)
    lines.append(f"{'total':<20}{total_seconds:>8.1f}s{100.0:>8.1f}%")
    lines.append(rule)
    footer = (
        f"{loaded + skipped + failed} episodes: "
        f"{loaded} loaded · {skipped} skipped · {failed} failed"
    )
    if loaded + failed > 0:
        footer += f" · ~{per_episode_seconds:.1f}s/episode"
    lines.append(footer)
    return "\n".join(lines)
