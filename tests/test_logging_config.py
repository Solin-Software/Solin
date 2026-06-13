from __future__ import annotations

import logging

from solin.core.foundation.logging_config import (
    LOG_FILENAME,
    LOG_HANDLER_NAME,
    configure_logging,
)


def _solin_handlers() -> list[logging.Handler]:
    return [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "name", "") == LOG_HANDLER_NAME
    ]


def test_configure_logging_installs_single_rotating_file_handler(tmp_path):
    log_path = configure_logging(tmp_path, level="DEBUG", force=True)
    logging.getLogger("solin.test").debug("hello from test")

    for handler in _solin_handlers():
        handler.flush()

    assert log_path == tmp_path / LOG_FILENAME
    assert log_path.exists()
    assert "hello from test" in log_path.read_text(encoding="utf-8")
    assert len(_solin_handlers()) == 1


def test_configure_logging_is_idempotent_without_force(tmp_path):
    configure_logging(tmp_path, level="INFO", force=True)
    first_handlers = _solin_handlers()

    configure_logging(tmp_path, level="WARNING")

    assert _solin_handlers() == first_handlers
    assert first_handlers[0].level == logging.WARNING
