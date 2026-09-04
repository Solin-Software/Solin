"""Bounded Qt diagnostics routed into Solin's rotating application log."""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol


# Internal release switch. Keep enabled unless a Qt runtime regression requires
# temporarily restoring stderr-only handling.
QT_DIAGNOSTIC_LOGGING_ENABLED = True

_QT_LOGGER_NAME = "solin.qt"
_QT_BASE_LOGGING_RULES = ("qt.qpa.mime=false",)
_QT_DIAGNOSTIC_LOGGING_RULES = (
    "qt.multimedia.warning=true",
    "qt.multimedia.critical=true",
    "qt.multimedia.*.warning=true",
    "qt.multimedia.*.critical=true",
)
_QT_DISABLED_LOGGING_RULES = ("qt.multimedia.ffmpeg=false",)
_LEGACY_DISABLED_RULES = frozenset(_QT_DISABLED_LOGGING_RULES)

_IGNORED_MESSAGE_FRAGMENTS = (
    "cannot be used as a native child widget",
    # Some playable H.264/AVI streams carry no packet PTS. Qt reconstructs the
    # cadence successfully but emits this warning once per frame, so routing it
    # through Python creates sustained work without an actionable diagnostic.
    "QFFmpeg::Demuxer received AVPacket with pts == AV_NOPTS_VALUE",
    # Video frames are converted on a worker thread precisely because that
    # thread has no RHI. Qt reports the CPU fallback once per conversion, so
    # routing it through Python would put file logging on the frame path.
    "No RHI backend. Using CPU conversion.",
)
_MAX_MESSAGE_LENGTH = 8 * 1024
_MAX_CONTEXT_LENGTH = 512
_MAX_PENDING_MESSAGES = 128
_MAX_WARNING_FINGERPRINTS = 128
_WARNING_REPEAT_LIMIT = 3
_WARNING_REPEAT_WINDOW_SECONDS = 60.0


class _MessageContext(Protocol):
    category: str | None
    file: str | None
    line: int
    function: str | None


@dataclass(frozen=True, slots=True)
class _QtLogRecord:
    level: int
    severity: str
    category: str
    source: str
    line: int
    function: str
    message: str

    @property
    def warning_fingerprint(self) -> tuple[str, str, int, str, str]:
        return (self.category, self.source, self.line, self.function, self.message)

    def render(self) -> str:
        context = [f"severity={self.severity}", f"category={self.category}"]
        if self.source:
            source = f"{self.source}:{self.line}" if self.line > 0 else self.source
            context.append(f"source={source}")
        if self.function:
            context.append(f"function={self.function}")
        return f"[{' '.join(context)}] {self.message}"


@dataclass(slots=True)
class _WarningWindow:
    started_at: float
    emitted: int = 1
    suppressed: int = 0


class QtMessageRouter:
    """Route actionable Qt messages without allowing warning floods."""

    def __init__(
        self,
        *,
        logger: logging.Logger | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._logger = logger or logging.getLogger(_QT_LOGGER_NAME)
        self._clock = clock
        self._lock = threading.Lock()
        self._file_logging_ready = False
        self._pending: deque[_QtLogRecord] = deque()
        self._dropped_pending = 0
        self._warning_windows: OrderedDict[tuple[str, str, int, str, str], _WarningWindow] = (
            OrderedDict()
        )

    def handle(self, mode: object, context: _MessageContext, message: str) -> None:
        """Qt message-handler boundary; no exception may escape into Qt."""
        try:
            if _is_ignored_message(message):
                return
            severity = _severity_for_mode(mode)
            if severity is None:
                return
            level, severity_name = severity
            record = _build_record(level, severity_name, context, message)
            for prepared in self._prepare_records(record):
                self._dispatch(prepared)
        except Exception as exc:  # noqa: BLE001 - must not cross the native Qt boundary
            _write_stderr(f"Could not route Qt diagnostic: {exc}\n{message}")

    def mark_file_logging_ready(self) -> None:
        """Flush the bounded startup buffer after the file handler exists."""
        with self._lock:
            if self._file_logging_ready:
                return
            self._file_logging_ready = True
            pending = tuple(self._pending)
            self._pending.clear()
            dropped = self._dropped_pending
            self._dropped_pending = 0

        if dropped:
            self._emit(
                _QtLogRecord(
                    level=logging.WARNING,
                    severity="warning",
                    category="solin.qt",
                    source="",
                    line=0,
                    function="",
                    message=(
                        f"Dropped {dropped} early Qt diagnostics because the startup "
                        "buffer reached its bounded capacity."
                    ),
                )
            )
        for record in pending:
            self._emit(record)

    def _prepare_records(self, record: _QtLogRecord) -> tuple[_QtLogRecord, ...]:
        if record.level != logging.WARNING:
            return (record,)

        now = self._clock()
        fingerprint = record.warning_fingerprint
        with self._lock:
            window = self._warning_windows.pop(fingerprint, None)
            if window is None or now - window.started_at >= _WARNING_REPEAT_WINDOW_SECONDS:
                summary: _QtLogRecord | None = None
                if window is not None and window.suppressed:
                    summary = replace(
                        record,
                        message=(
                            f"Suppressed {window.suppressed} identical Qt warnings during "
                            f"the previous {_WARNING_REPEAT_WINDOW_SECONDS:.0f} seconds: "
                            f"{record.message}"
                        ),
                    )
                self._warning_windows[fingerprint] = _WarningWindow(started_at=now)
                self._trim_warning_windows()
                return (summary, record) if summary is not None else (record,)

            if window.emitted < _WARNING_REPEAT_LIMIT:
                window.emitted += 1
                self._warning_windows[fingerprint] = window
                return (record,)

            window.suppressed += 1
            self._warning_windows[fingerprint] = window
            if window.suppressed == 1:
                return (
                    replace(
                        record,
                        message=(
                            "Further identical Qt warnings will be suppressed for "
                            f"{_WARNING_REPEAT_WINDOW_SECONDS:.0f} seconds: {record.message}"
                        ),
                    ),
                )
            return ()

    def _trim_warning_windows(self) -> None:
        while len(self._warning_windows) > _MAX_WARNING_FINGERPRINTS:
            self._warning_windows.popitem(last=False)

    def _dispatch(self, record: _QtLogRecord) -> None:
        with self._lock:
            if not self._file_logging_ready:
                if len(self._pending) >= _MAX_PENDING_MESSAGES:
                    self._pending.popleft()
                    self._dropped_pending += 1
                self._pending.append(record)
                if record.severity == "fatal":
                    _write_stderr(record.render())
                return
        self._emit(record)

    def _emit(self, record: _QtLogRecord) -> None:
        try:
            self._logger.log(record.level, record.render())
            if record.severity == "fatal":
                for handler in logging.getLogger().handlers:
                    handler.flush()
        except Exception as exc:  # noqa: BLE001 - last-resort crash diagnostic path
            _write_stderr(f"Could not write Qt diagnostic: {exc}\n{record.render()}")


_ROUTER = QtMessageRouter()


def configure_qt_logging_rules(
    *,
    enabled: bool = QT_DIAGNOSTIC_LOGGING_ENABLED,
) -> None:
    """Set Qt category rules before importing Qt while preserving caller rules."""
    existing = os.environ.get("QT_LOGGING_RULES", "")
    entries = [entry.strip() for entry in existing.split(";") if entry.strip()]
    if enabled:
        entries = [entry for entry in entries if entry not in _LEGACY_DISABLED_RULES]
        required = _QT_BASE_LOGGING_RULES + _QT_DIAGNOSTIC_LOGGING_RULES
    else:
        required = _QT_BASE_LOGGING_RULES + _QT_DISABLED_LOGGING_RULES
    configured = set(entries)
    entries.extend(rule for rule in required if rule not in configured)
    os.environ["QT_LOGGING_RULES"] = ";".join(entries)


def install_qt_message_handler(
    *,
    enabled: bool = QT_DIAGNOSTIC_LOGGING_ENABLED,
) -> object:
    """Install the bounded file router and return Qt's previous handler."""
    from PySide6.QtCore import qInstallMessageHandler

    handler = _ROUTER.handle if enabled else _stderr_message_handler
    return qInstallMessageHandler(handler)


def mark_qt_file_logging_ready() -> None:
    _ROUTER.mark_file_logging_ready()


def _stderr_message_handler(_mode: object, _context: _MessageContext, message: str) -> None:
    if not _is_ignored_message(message):
        _write_stderr(message)


def _severity_for_mode(mode: object) -> tuple[int, str] | None:
    name = getattr(mode, "name", "")
    if name == "QtWarningMsg":
        return logging.WARNING, "warning"
    if name == "QtCriticalMsg":
        return logging.CRITICAL, "critical"
    if name == "QtFatalMsg":
        return logging.CRITICAL, "fatal"
    return None


def _build_record(
    level: int,
    severity: str,
    context: _MessageContext,
    message: str,
) -> _QtLogRecord:
    source = _bounded_text(getattr(context, "file", "") or "", _MAX_CONTEXT_LENGTH)
    source = source.replace("\\", "/").rsplit("/", 1)[-1]
    line = getattr(context, "line", 0)
    return _QtLogRecord(
        level=level,
        severity=severity,
        category=_bounded_text(
            getattr(context, "category", "") or "default",
            _MAX_CONTEXT_LENGTH,
        ),
        source=source,
        line=line if isinstance(line, int) else 0,
        function=_bounded_text(
            getattr(context, "function", "") or "",
            _MAX_CONTEXT_LENGTH,
        ),
        message=_bounded_text(message, _MAX_MESSAGE_LENGTH),
    )


def _bounded_text(value: object, maximum: int) -> str:
    text = str(value).replace("\r", "\\r").replace("\n", "\\n")
    if len(text) <= maximum:
        return text
    return f"{text[: maximum - 1]}…"


def _is_ignored_message(message: object) -> bool:
    text = str(message)
    return any(fragment in text for fragment in _IGNORED_MESSAGE_FRAGMENTS)


def _write_stderr(message: object) -> None:
    stream = sys.stderr
    if stream is None:
        return
    try:
        stream.write(f"{message}\n")
        stream.flush()
    except (OSError, RuntimeError, ValueError):
        return


__all__ = [
    "QT_DIAGNOSTIC_LOGGING_ENABLED",
    "QtMessageRouter",
    "configure_qt_logging_rules",
    "install_qt_message_handler",
    "mark_qt_file_logging_ready",
]
