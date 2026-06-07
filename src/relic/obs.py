"""Observability: one place to wire up logging for the CLI.

Logs are diagnostics, not results. They go to stderr so stdout stays a clean
channel for what a command actually produces: a recall answer, a catalog, the MCP
stream. The handler attaches to the ``relic`` logger only, with propagation off,
so turning it on never surfaces graphiti, httpx, or openai INFO chatter from the
root logger. Third-party logs stay suppressed by Python's default until you ask
for them.

Call ``configure_logging`` once at CLI entry, then ``get_logger("ingest")`` in a
module to emit under ``relic.ingest``.
"""

from __future__ import annotations

import logging

_NAMESPACE = "relic"
_configured = False


def configure_logging(*, verbose: bool = False) -> None:
    """Attach a stderr handler to the ``relic`` logger. Idempotent.

    Call once at CLI entry. ``verbose`` drops the level to DEBUG (per-episode
    timing, full tracebacks); the default is INFO (phase counts and the run
    summary). Re-calling only adjusts the level: it never stacks handlers.
    """
    from rich.console import Console
    from rich.logging import RichHandler

    global _configured
    level = logging.DEBUG if verbose else logging.INFO
    logger = logging.getLogger(_NAMESPACE)
    # Keep relic logs off the root logger so we never trip the default handler
    # into echoing third-party INFO chatter.
    logger.propagate = False
    logger.setLevel(level)

    if not _configured:
        handler = RichHandler(
            console=Console(stderr=True),
            show_time=False,
            show_path=False,
            markup=False,
            rich_tracebacks=verbose,
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        _configured = True
    for handler in logger.handlers:
        handler.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    """Return the ``relic.<name>`` logger. Pass a short module name, e.g. ``"ingest"``."""
    return logging.getLogger(f"{_NAMESPACE}.{name}")
