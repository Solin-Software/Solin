from __future__ import annotations

from PySide6.QtCore import QCoreApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton

from solin.styles.theme import PALETTE
from solin.widgets.common.button_feedback import ButtonSuccessFlash


def test_button_success_flash_retargets_and_restores_original_style() -> None:
    button = QPushButton("Return")
    button.setObjectName("ReturnScene")
    button.setStyleSheet("QPushButton#ReturnScene { color: white; }")
    original_style = button.styleSheet()
    feedback = ButtonSuccessFlash(duration_ms=30)

    feedback.flash(button)
    feedback.flash(button)

    assert PALETTE.success in button.styleSheet()
    assert len(feedback._active) == 1
    QTest.qWait(50)
    QCoreApplication.processEvents()
    assert button.styleSheet() == original_style
    assert feedback._active == {}
    button.deleteLater()
    feedback.deleteLater()
    QCoreApplication.processEvents()
