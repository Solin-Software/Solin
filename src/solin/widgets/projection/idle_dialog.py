from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from solin.styles.icons import ICON_SET_AS_IDLE, make_icon
from solin.styles.theme import PALETTE, qss_rgba


class SetAsIdleConfirmDialog(QDialog):
    """Confirmation dialog for setting the current media as idle screen."""

    def __init__(self, title: str, pixmap: "QPixmap | None" = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("")
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setModal(True)
        self._confirmed = False
        self._parent_ref = parent
        self._build_ui(title, pixmap)

    def _build_ui(self, title: str, pixmap):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        card = QFrame()
        card.setObjectName("IdleConfirmCard")
        card.setStyleSheet(
            f"QFrame#IdleConfirmCard {{"
            f"  background: {PALETTE.surface_hover_strong};"
            f"  border: 1px solid {PALETTE.border};"
            f"  border-radius: 12px;"
            f"}}"
            "QWidget { background: transparent; }"
            "QLabel  { background: transparent; }"
        )
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(20, 20, 20, 20)
        card_lay.setSpacing(14)

        header = QWidget()
        header_lay = QHBoxLayout(header)
        header_lay.setContentsMargins(0, 0, 0, 0)
        header_lay.setSpacing(12)

        thumb_box = QFrame()
        thumb_box.setFixedSize(52, 52)
        thumb_box.setStyleSheet(
            f"QFrame {{ background: {PALETTE.bg0}; border: 1px solid {PALETTE.border_muted};"
            f"         border-radius: 8px; }}"
        )
        thumb_lay = QVBoxLayout(thumb_box)
        thumb_lay.setContentsMargins(0, 0, 0, 0)
        thumb_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb_img = QLabel()
        thumb_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb_img.setFixedSize(52, 52)
        if pixmap and not pixmap.isNull():
            scaled = pixmap.scaled(
                50,
                50,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            thumb_img.setPixmap(scaled)
        else:
            thumb_img.setPixmap(make_icon(ICON_SET_AS_IDLE, 22, PALETTE.text_muted).pixmap(22, 22))
        thumb_lay.addWidget(thumb_img)

        text_col = QWidget()
        text_lay = QVBoxLayout(text_col)
        text_lay.setContentsMargins(0, 0, 0, 0)
        text_lay.setSpacing(4)

        lbl_action = QLabel(self.tr("Set as Idle Screen?"))
        lbl_action.setStyleSheet(
            f"color: {PALETTE.text_primary}; font-size: 14px; font-weight: 700;"
        )

        lbl_title = QLabel(title)
        lbl_title.setStyleSheet(f"color: {PALETTE.text_muted}; font-size: 12px;")
        lbl_title.setWordWrap(True)
        lbl_title.setMaximumWidth(220)

        text_lay.addWidget(lbl_action)
        text_lay.addWidget(lbl_title)
        text_lay.addStretch()

        header_lay.addWidget(thumb_box)
        header_lay.addWidget(text_col, stretch=1)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background: {PALETTE.border_muted}; max-height: 1px; border: none;")
        sep.setFixedHeight(1)

        lbl_desc = QLabel(
            self.tr(
                "This media will be shown as the projection screen background "
                "when no content is being displayed."
            )
        )
        lbl_desc.setWordWrap(True)
        lbl_desc.setStyleSheet(f"color: {PALETTE.text_faint}; font-size: 11px;")
        lbl_desc.setMaximumWidth(320)

        btn_row = QWidget()
        btn_row_lay = QHBoxLayout(btn_row)
        btn_row_lay.setContentsMargins(0, 0, 0, 0)
        btn_row_lay.setSpacing(8)

        self._cancel_btn = QPushButton(self.tr("Cancel"))
        self._cancel_btn.setFixedHeight(32)
        self._cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel_btn.setStyleSheet(
            f"QPushButton {{"
            f"  background: transparent;"
            f"  color: {PALETTE.text_muted};"
            f"  border: 1px solid {PALETTE.border};"
            f"  border-radius: 6px;"
            f"  font-size: 12px;"
            f"  font-weight: 600;"
            f"  padding: 0 16px;"
            f"}}"
            f"QPushButton:hover  {{ background: {qss_rgba(PALETTE.text_faint, 0.15)}; color: {PALETTE.text_secondary}; }}"
            f"QPushButton:pressed{{ background: {qss_rgba(PALETTE.text_faint, 0.28)}; }}"
        )
        self._cancel_btn.clicked.connect(self.reject)

        self._confirm_btn = QPushButton(self.tr("Set as Idle"))
        self._confirm_btn.setFixedHeight(32)
        self._confirm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._confirm_btn.setStyleSheet(
            f"QPushButton {{"
            f"  background: {PALETTE.success};"
            f"  color: {PALETTE.text_on_accent};"
            f"  border: 1px solid {PALETTE.success_pressed};"
            f"  border-radius: 6px;"
            f"  font-size: 12px;"
            f"  font-weight: 600;"
            f"  padding: 0 16px;"
            f"}}"
            f"QPushButton:hover  {{ background: {PALETTE.success_hover}; }}"
            f"QPushButton:pressed{{ background: {PALETTE.success_pressed}; }}"
        )
        self._confirm_btn.setDefault(True)
        self._confirm_btn.clicked.connect(self._on_confirm)

        btn_row_lay.addStretch()
        btn_row_lay.addWidget(self._cancel_btn)
        btn_row_lay.addWidget(self._confirm_btn)

        card_lay.addWidget(header)
        card_lay.addWidget(sep)
        card_lay.addWidget(lbl_desc)
        card_lay.addWidget(btn_row)

        outer.addWidget(card)
        self.setFixedWidth(360)

    def showEvent(self, event):
        super().showEvent(event)
        ref = self._parent_ref
        if ref is None:
            return
        top = ref if isinstance(ref, QDialog) else ref.window()
        if top is None:
            return
        geo = top.frameGeometry()
        mine = self.frameGeometry()
        cx = geo.x() + (geo.width() - mine.width()) // 2
        cy = geo.y() + (geo.height() - mine.height()) // 2
        self.move(cx, cy)

    def _on_confirm(self):
        self._confirmed = True
        self.accept()

    def confirmed(self) -> bool:
        return self._confirmed

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
        else:
            super().keyPressEvent(event)


def confirm_set_as_idle(
    title: str,
    *,
    pixmap: QPixmap | None = None,
    parent=None,
) -> bool:
    """Show the shared idle-screen confirmation and return its explicit choice."""

    dialog = SetAsIdleConfirmDialog(title, pixmap=pixmap, parent=parent)
    dialog.exec()
    return dialog.confirmed()
