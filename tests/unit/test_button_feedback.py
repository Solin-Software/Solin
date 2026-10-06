from __future__ import annotations

import sys

import pytest
from PySide6.QtCore import QCoreApplication, QElapsedTimer, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton, QWidget

from solin.styles.theme import PALETTE
from solin.widgets.common.button_feedback import ButtonSuccessFlash


def _wait_until(predicate, timeout_ms: int = 2_000) -> None:
    timer = QElapsedTimer()
    timer.start()
    while not predicate():
        QCoreApplication.processEvents()
        assert timer.elapsed() < timeout_ms, "Timed out waiting for button feedback"
        QTest.qWait(5)


@pytest.mark.parametrize("feedback_first", [False, True])
def test_success_feedback_disconnects_when_shared_owner_is_destroyed(
    monkeypatch, feedback_first: bool
) -> None:
    errors: list[BaseException] = []
    monkeypatch.setattr(sys, "excepthook", lambda _kind, error, _trace: errors.append(error))
    owner = QWidget()
    if feedback_first:
        feedback = ButtonSuccessFlash(owner, duration_ms=5_000)
        button = QPushButton("Return", owner)
    else:
        button = QPushButton("Return", owner)
        feedback = ButtonSuccessFlash(owner, duration_ms=5_000)
    feedback.flash(button)

    owner.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert errors == []


def test_success_feedback_releases_animation_when_button_is_destroyed() -> None:
    feedback = ButtonSuccessFlash(duration_ms=5_000)
    button = QPushButton("Return")
    try:
        feedback.flash(button)
        button.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert feedback._active == {}
        assert feedback._tracked_buttons == set()
    finally:
        feedback.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_button_success_flash_retargets_and_restores_original_style() -> None:
    button = QPushButton("Return")
    button.setObjectName("ReturnScene")
    button.setStyleSheet("QPushButton#ReturnScene { color: white; }")
    original_style = button.styleSheet()
    feedback = ButtonSuccessFlash(duration_ms=30)

    try:
        feedback.flash(button)
        feedback.flash(button)

        assert PALETTE.success in button.styleSheet()
        assert len(feedback._active) == 1
        _wait_until(
            lambda: feedback._active == {} and button.styleSheet() == original_style
        )
        assert button.styleSheet() == original_style
        assert feedback._active == {}
    finally:
        button.deleteLater()
        feedback.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()
