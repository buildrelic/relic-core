"""Logging setup: relic logs go to stderr, scoped to the relic logger, idempotent."""

import logging

from relic.obs import configure_logging, get_logger


def test_get_logger_namespaces_under_relic() -> None:
    assert get_logger("ingest").name == "relic.ingest"
    assert get_logger("load").name == "relic.load"


def test_configure_is_idempotent_and_scoped() -> None:
    configure_logging()
    relic_logger = logging.getLogger("relic")
    assert len(relic_logger.handlers) == 1
    # propagation off keeps relic logs from echoing third-party INFO via the root handler
    assert relic_logger.propagate is False

    configure_logging(verbose=True)  # re-calling adjusts level, never stacks handlers
    assert len(relic_logger.handlers) == 1
    assert relic_logger.level == logging.DEBUG

    configure_logging(verbose=False)
    assert relic_logger.level == logging.INFO


def test_logs_go_to_stderr_not_stdout(capsys) -> None:
    configure_logging()
    get_logger("probe").info("diagnostic-line")
    captured = capsys.readouterr()
    assert "diagnostic-line" in captured.err
    assert "diagnostic-line" not in captured.out
