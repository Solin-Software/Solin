from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout

from ...styles.icons import make_icon
from ._shared import _DIM, _MUTED, _SURF, _TEXT, _ToggleSwitch, _BORDER


class SettingsLayoutMixin:
    """Shared row/card builders used by SettingsWidget and future settings pages."""

    def _section_title(self, text: str, attr: str | None = None) -> QLabel:
        label = QLabel(text.upper())
        label.setStyleSheet(
            f"font-size: 11px; font-weight: 700; color: {_MUTED};"
            " background: transparent; padding: 0;"
        )
        if attr:
            setattr(self, attr, label)
        return label

    def _card(self) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setStyleSheet(
            f"background: {_SURF}; border: 1px solid {_BORDER};"
            " border-radius: 10px;"
        )
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        return frame, layout

    def _divider(self) -> QFrame:
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(
            f"background: {_BORDER}; border: none; margin-left: 14px; margin-right: 14px;"
        )
        return divider

    def _toggle_row(self, icon_svg, title, desc, checked, icon_color=_MUTED):
        row = QFrame()
        row.setStyleSheet("background: transparent; border: none;")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(14, 10, 14, 10)
        row_layout.setSpacing(12)

        icon_label = QLabel()
        icon_label.setPixmap(make_icon(icon_svg, size=18, color=icon_color).pixmap(18, 18))
        icon_label.setFixedSize(20, 20)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setStyleSheet("background: transparent; border: none;")
        row_layout.addWidget(icon_label)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        title_label = QLabel(title)
        title_label.setStyleSheet(
            f"font-size: 13px; font-weight: 500; color: {_TEXT};"
            " background: transparent; border: none;"
        )
        text_col.addWidget(title_label)

        desc_label = QLabel(desc)
        desc_label.setStyleSheet(
            f"font-size: 11px; color: {_DIM}; background: transparent; border: none;"
        )
        desc_label.setWordWrap(True)
        text_col.addWidget(desc_label)
        row_layout.addLayout(text_col, stretch=1)

        toggle = _ToggleSwitch(checked=checked)
        row_layout.addWidget(toggle)
        return row, toggle, title_label, desc_label

    def _clickable_row(
        self,
        icon_svg,
        label_text,
        value_text,
        subtitle="",
        icon_color=_MUTED,
    ):
        row = QFrame()
        row.setStyleSheet("background: transparent; border: none;")
        row.setCursor(Qt.CursorShape.PointingHandCursor)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(14, 10, 14, 10)
        row_layout.setSpacing(12)

        icon_label = QLabel()
        icon_label.setPixmap(make_icon(icon_svg, size=18, color=icon_color).pixmap(18, 18))
        icon_label.setFixedSize(20, 20)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setStyleSheet("background: transparent; border: none;")
        row_layout.addWidget(icon_label)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        label = QLabel(label_text)
        label.setStyleSheet(
            f"font-size: 11px; color: {_MUTED}; background: transparent; border: none;"
        )
        text_col.addWidget(label)

        value_label = QLabel(value_text)
        value_label.setStyleSheet(
            f"font-size: 13px; font-weight: 500; color: {_TEXT};"
            " background: transparent; border: none;"
        )
        text_col.addWidget(value_label)

        subtitle_label = None
        if subtitle:
            subtitle_label = QLabel(subtitle)
            subtitle_label.setStyleSheet(
                f"font-size: 11px; color: {_DIM}; background: transparent; border: none;"
            )
            text_col.addWidget(subtitle_label)
        row_layout.addLayout(text_col, stretch=1)

        chevron = QLabel("\u203a")
        chevron.setStyleSheet(
            f"color: {_DIM}; font-size: 18px; background: transparent; border: none;"
        )
        row_layout.addWidget(chevron)
        return row, value_label, label, subtitle_label
