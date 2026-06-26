from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ...styles.icons import make_icon
from .shared import (
    SETTINGS_BORDER,
    SETTINGS_DIM,
    SETTINGS_MUTED,
    SETTINGS_SURFACE,
    SETTINGS_TEXT,
    SettingsToggleSwitch,
)


StyleFactory = Callable[[], str]
ThemeBinding = Callable[[], None]


class SettingsLayoutMixin:
    """Shared row/card builders used by SettingsWidget and future settings pages."""

    def _reset_theme_bindings(self) -> None:
        self._theme_bindings: list[ThemeBinding] = []

    def _add_theme_binding(self, binding: ThemeBinding) -> None:
        if not hasattr(self, "_theme_bindings"):
            self._reset_theme_bindings()
        self._theme_bindings.append(binding)
        binding()

    def _bind_theme_style(self, widget: QWidget, style_factory: StyleFactory) -> None:
        self._add_theme_binding(lambda: widget.setStyleSheet(style_factory()))

    def _bind_theme_pixmap(
        self,
        label: QLabel,
        icon_svg: str,
        *,
        size: int,
        color,
    ) -> None:
        self._add_theme_binding(
            lambda: label.setPixmap(
                make_icon(icon_svg, size=size, color=color).pixmap(size, size)
            )
        )

    def _bind_theme_icon(
        self,
        button: QAbstractButton,
        icon_svg: str,
        *,
        size: int,
        color,
    ) -> None:
        self._add_theme_binding(
            lambda: button.setIcon(make_icon(icon_svg, size=size, color=color))
        )

    def _apply_settings_theme_bindings(self) -> None:
        live_bindings: list[ThemeBinding] = []
        for binding in getattr(self, "_theme_bindings", ()):
            try:
                binding()
            except RuntimeError:
                continue
            live_bindings.append(binding)
        self._theme_bindings = live_bindings

    def _section_title(self, text: str, attr: str | None = None) -> QLabel:
        label = QLabel(text.upper())
        self._bind_theme_style(
            label,
            lambda: (
                f"font-size: 11px; font-weight: 700; color: {SETTINGS_MUTED};"
                " background: transparent; padding: 0;"
            ),
        )
        if attr:
            setattr(self, attr, label)
        return label

    def _card(self) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        self._bind_theme_style(
            frame,
            lambda: (
                f"background: {SETTINGS_SURFACE}; border: 1px solid {SETTINGS_BORDER};"
                " border-radius: 10px;"
            ),
        )
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        return frame, layout

    def _divider(self) -> QFrame:
        divider = QFrame()
        divider.setFixedHeight(1)
        self._bind_theme_style(
            divider,
            lambda: (
                f"background: {SETTINGS_BORDER}; border: none;"
                " margin-left: 14px; margin-right: 14px;"
            ),
        )
        return divider

    def _toggle_row(self, icon_svg, title, desc, checked, icon_color=SETTINGS_MUTED):
        row = QFrame()
        row.setStyleSheet("background: transparent; border: none;")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(14, 10, 14, 10)
        row_layout.setSpacing(12)

        icon_label = QLabel()
        self._bind_theme_pixmap(icon_label, icon_svg, size=18, color=icon_color)
        icon_label.setFixedSize(20, 20)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setStyleSheet("background: transparent; border: none;")
        row_layout.addWidget(icon_label)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        title_label = QLabel(title)
        self._bind_theme_style(
            title_label,
            lambda: (
                f"font-size: 13px; font-weight: 500; color: {SETTINGS_TEXT};"
                " background: transparent; border: none;"
            ),
        )
        text_col.addWidget(title_label)

        desc_label = QLabel(desc)
        self._bind_theme_style(
            desc_label,
            lambda: (
                f"font-size: 11px; color: {SETTINGS_DIM};"
                " background: transparent; border: none;"
            ),
        )
        desc_label.setWordWrap(True)
        text_col.addWidget(desc_label)
        row_layout.addLayout(text_col, stretch=1)

        toggle = SettingsToggleSwitch(checked=checked)
        row_layout.addWidget(toggle)
        return row, toggle, title_label, desc_label

    def _clickable_row(
        self,
        icon_svg,
        label_text,
        value_text,
        subtitle="",
        icon_color=SETTINGS_MUTED,
    ):
        row = QFrame()
        row.setStyleSheet("background: transparent; border: none;")
        row.setCursor(Qt.CursorShape.PointingHandCursor)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(14, 10, 14, 10)
        row_layout.setSpacing(12)

        icon_label = QLabel()
        self._bind_theme_pixmap(icon_label, icon_svg, size=18, color=icon_color)
        icon_label.setFixedSize(20, 20)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setStyleSheet("background: transparent; border: none;")
        row_layout.addWidget(icon_label)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        label = QLabel(label_text)
        self._bind_theme_style(
            label,
            lambda: (
                f"font-size: 11px; color: {SETTINGS_MUTED};"
                " background: transparent; border: none;"
            ),
        )
        text_col.addWidget(label)

        value_label = QLabel(value_text)
        self._bind_theme_style(
            value_label,
            lambda: (
                f"font-size: 13px; font-weight: 500; color: {SETTINGS_TEXT};"
                " background: transparent; border: none;"
            ),
        )
        text_col.addWidget(value_label)

        subtitle_label = None
        if subtitle:
            subtitle_label = QLabel(subtitle)
            self._bind_theme_style(
                subtitle_label,
                lambda: (
                    f"font-size: 11px; color: {SETTINGS_DIM};"
                    " background: transparent; border: none;"
                ),
            )
            text_col.addWidget(subtitle_label)
        row_layout.addLayout(text_col, stretch=1)

        chevron = QLabel("\u203a")
        self._bind_theme_style(
            chevron,
            lambda: (
                f"color: {SETTINGS_DIM}; font-size: 18px;"
                " background: transparent; border: none;"
            ),
        )
        row_layout.addWidget(chevron)
        return row, value_label, label, subtitle_label
