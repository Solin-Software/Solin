from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QObject, QVariantAnimation, Slot
from PySide6.QtWidgets import QPushButton
from shiboken6 import getCppPointer

from solin.styles.theme import PALETTE, qss_rgba


class ButtonSuccessFlash(QObject):
    """Render short, reusable success feedback on an individual button."""

    def __init__(self, parent: QObject | None = None, *, duration_ms: int = 360) -> None:
        super().__init__(parent)
        self._duration_ms = duration_ms
        self._active: dict[int, tuple[QVariantAnimation, str]] = {}
        self._tracked_buttons: set[int] = set()

    def flash(self, button: QPushButton) -> None:
        button_id = getCppPointer(button)[0]
        previous = self._active.pop(button_id, None)
        if previous is not None:
            previous_animation, previous_style = previous
            previous_animation.stop()
            previous_animation.deleteLater()
            try:
                button.setStyleSheet(previous_style)
            except RuntimeError:
                return

        original_style = button.styleSheet()
        if button_id not in self._tracked_buttons:
            self._tracked_buttons.add(button_id)
            button.destroyed.connect(self._button_destroyed)
        object_name = button.objectName()
        selector = f"QPushButton#{object_name}" if object_name else "QPushButton"
        animation = QVariantAnimation(self)
        animation.setDuration(self._duration_ms)
        animation.setStartValue(1.0)
        animation.setEndValue(0.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)

        def apply(value: object) -> None:
            amount = float(value)
            background_alpha = int(18 + (56 * amount))
            border_alpha = int(70 + (120 * amount))
            hover_alpha = int(30 + (70 * amount))
            pressed_alpha = int(42 + (82 * amount))
            border = qss_rgba(PALETTE.success, border_alpha / 255)
            try:
                button.setStyleSheet(
                    original_style + f"{selector} {{"
                    f"background:{qss_rgba(PALETTE.success, background_alpha / 255)};"
                    f"border-color:{border};color:{PALETTE.success};"
                    "}" + f"{selector}:hover {{"
                    f"background:{qss_rgba(PALETTE.success, hover_alpha / 255)};"
                    f"border-color:{border};color:{PALETTE.success};"
                    "}" + f"{selector}:pressed {{"
                    f"background:{qss_rgba(PALETTE.success, pressed_alpha / 255)};"
                    f"border-color:{border};color:{PALETTE.text_secondary};"
                    "}"
                )
            except RuntimeError:
                pass

        def cleanup() -> None:
            current = self._active.get(button_id)
            if current is not None and current[0] is animation:
                self._active.pop(button_id, None)
                try:
                    button.setStyleSheet(original_style)
                except RuntimeError:
                    pass
            animation.deleteLater()

        animation.valueChanged.connect(apply)
        animation.finished.connect(cleanup)
        self._active[button_id] = (animation, original_style)
        apply(1.0)
        animation.start()

    @Slot(QObject)
    def _button_destroyed(self, button: QObject) -> None:
        # destroyed() may supply a different Python wrapper for the same native
        # object. The slot's QObject receiver also disconnects on its destruction.
        button_id = getCppPointer(button)[0]
        self._tracked_buttons.discard(button_id)
        current = self._active.pop(button_id, None)
        if current is None:
            return
        animation, _original_style = current
        animation.stop()
        animation.deleteLater()
