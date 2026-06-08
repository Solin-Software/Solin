"""Small helpers for intentionally ignored non-critical exceptions."""

from __future__ import annotations

import logging


def log_ignored_exception(
    logger_name: str,
    message: str = "Ignored non-critical exception",
) -> None:
    """Log the active exception without interrupting optional UI cleanup paths."""
    logging.getLogger(logger_name).debug(message, exc_info=True)
