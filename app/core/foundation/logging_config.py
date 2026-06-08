"""Central logging configuration for the desktop runtime."""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

from app.core.foundation.constants import IS_DEV

LOG_LEVEL_ENV_VAR = "SOLIN_LOG_LEVEL"
LOG_FILENAME = "solin.log"
LOG_HANDLER_NAME = "solin-rotating-file"
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 5

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s:%(lineno)d - %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _coerce_level(value: str | int | None) -> int:
    if isinstance(value, int):
        return value
    raw = (value or os.getenv(LOG_LEVEL_ENV_VAR) or ("DEBUG" if IS_DEV else "INFO"))
    name = raw.strip().upper()
    if name.isdigit():
        return int(name)
    return logging._nameToLevel.get(name, logging.DEBUG if IS_DEV else logging.INFO)


def configure_logging(
    log_dir: str | os.PathLike[str],
    *,
    level: str | int | None = None,
    force: bool = False,
) -> Path:
    """
    Install Solin's rotating file handler and return the active log file path.

    The function is idempotent by default so tests and profile relaunch paths can
    call it safely without duplicating handlers.
    """
    target_dir = Path(log_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    log_path = target_dir / LOG_FILENAME
    resolved_level = _coerce_level(level)

    root = logging.getLogger()
    root.setLevel(resolved_level)
    logging.captureWarnings(True)

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    existing = next(
        (
            handler
            for handler in root.handlers
            if getattr(handler, "name", "") == LOG_HANDLER_NAME
        ),
        None,
    )
    if existing is not None:
        if force:
            root.removeHandler(existing)
            existing.close()
        else:
            existing.setLevel(resolved_level)
            existing.setFormatter(formatter)
            return log_path

    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.set_name(LOG_HANDLER_NAME)
    handler.setLevel(resolved_level)
    handler.setFormatter(formatter)
    root.addHandler(handler)

    for noisy_logger in ("PIL", "urllib3", "requests", "fontTools"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    logging.getLogger(__name__).debug("Logging configured at %s", log_path)
    return log_path
