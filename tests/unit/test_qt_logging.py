from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum

import pytest

from solin.core.foundation.qt_logging import (
    QtMessageRouter,
    configure_qt_logging_rules,
)


class _Mode(Enum):
    QtDebugMsg = 0
    QtWarningMsg = 1
    QtCriticalMsg = 2
    QtFatalMsg = 3
    QtInfoMsg = 4


@dataclass(frozen=True)
class _Context:
    category: str = "qt.scenegraph.general"
    file: str = r"C:\qt\src\qsgcontext.cpp"
    line: int = 412
    function: str = "QSGContext::initialize"


class _RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[tuple[int, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append((record.levelno, record.getMessage()))


def _router(*, clock=lambda: 0.0) -> tuple[QtMessageRouter, _RecordingHandler]:
    logger = logging.Logger("solin.qt.test", level=logging.DEBUG)
    handler = _RecordingHandler()
    logger.addHandler(handler)
    router = QtMessageRouter(logger=logger, clock=clock)
    return router, handler


@pytest.mark.parametrize(
    "existing",
    [
        "custom.category.debug=true",
        "custom.category.debug=true;qt.qpa.mime=false",
        " ; custom.category.debug=true ; ; qt.qpa.mime=false ; ",
    ],
)
def test_qt_logging_rules_preserve_caller_rules_without_duplicates(monkeypatch, existing) -> None:
    monkeypatch.setenv(
        "QT_LOGGING_RULES",
        existing,
    )

    configure_qt_logging_rules()
    configure_qt_logging_rules()

    rules = [rule for rule in os.environ["QT_LOGGING_RULES"].split(";") if rule]
    assert rules == ["custom.category.debug=true", "qt.qpa.mime=false"]


@pytest.mark.parametrize("existing", [None, "", " ; ; "])
def test_qt_logging_rules_configure_generic_defaults(monkeypatch, existing) -> None:
    if existing is None:
        monkeypatch.delenv("QT_LOGGING_RULES", raising=False)
    else:
        monkeypatch.setenv("QT_LOGGING_RULES", existing)

    configure_qt_logging_rules()

    assert os.environ["QT_LOGGING_RULES"] == "qt.qpa.mime=false"


def test_router_buffers_startup_warning_then_preserves_qt_context() -> None:
    router, handler = _router()

    router.handle(_Mode.QtWarningMsg, _Context(), "Scene graph initialization failed")
    assert handler.messages == []

    router.mark_file_logging_ready()

    assert len(handler.messages) == 1
    level, message = handler.messages[0]
    assert level == logging.WARNING
    assert "severity=warning" in message
    assert "category=qt.scenegraph.general" in message
    assert "source=qsgcontext.cpp:412" in message
    assert "function=QSGContext::initialize" in message
    assert message.endswith("Scene graph initialization failed")


def test_router_records_warning_critical_and_fatal_but_not_debug_or_info() -> None:
    router, handler = _router()
    router.mark_file_logging_ready()

    router.handle(_Mode.QtDebugMsg, _Context(), "debug detail")
    router.handle(_Mode.QtInfoMsg, _Context(), "informational detail")
    router.handle(_Mode.QtWarningMsg, _Context(), "warning detail")
    router.handle(_Mode.QtCriticalMsg, _Context(), "critical detail")
    router.handle(_Mode.QtFatalMsg, _Context(), "fatal detail")

    assert [level for level, _message in handler.messages] == [
        logging.WARNING,
        logging.CRITICAL,
        logging.CRITICAL,
    ]
    assert "severity=warning" in handler.messages[0][1]
    assert "severity=critical" in handler.messages[1][1]
    assert "severity=fatal" in handler.messages[2][1]


def test_router_suppresses_known_benign_native_child_warning() -> None:
    router, handler = _router()
    router.mark_file_logging_ready()

    router.handle(
        _Mode.QtWarningMsg,
        _Context(),
        "QQuickWidget cannot be used as a native child widget",
    )

    assert handler.messages == []


def test_router_bounds_repeated_warnings_but_never_suppresses_critical() -> None:
    now = [0.0]
    router, handler = _router(clock=lambda: now[0])
    router.mark_file_logging_ready()

    for _index in range(10):
        router.handle(_Mode.QtWarningMsg, _Context(), "Render queue stalled")
    for _index in range(5):
        router.handle(_Mode.QtCriticalMsg, _Context(), "Renderer crashed")

    warning_messages = [message for level, message in handler.messages if level == logging.WARNING]
    critical_messages = [
        message for level, message in handler.messages if level == logging.CRITICAL
    ]
    assert len(warning_messages) == 4
    assert "Further identical Qt warnings will be suppressed" in warning_messages[-1]
    assert len(critical_messages) == 5

    now[0] = 61.0
    router.handle(_Mode.QtWarningMsg, _Context(), "Render queue stalled")

    warning_messages = [message for level, message in handler.messages if level == logging.WARNING]
    assert "Suppressed 7 identical Qt warnings" in warning_messages[-2]
    assert warning_messages[-1].endswith("Render queue stalled")


def test_router_keeps_each_log_entry_on_one_bounded_line() -> None:
    router, handler = _router()
    router.mark_file_logging_ready()

    router.handle(_Mode.QtWarningMsg, _Context(), "first line\r\n" + "x" * 9_000)

    message = handler.messages[0][1]
    assert "\n" not in message
    assert "\r" not in message
    assert "\\r\\n" in message
    assert message.endswith("…")
