from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QCloseEvent, QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from solin.core.storage.binary_files import write_bytes_atomic
from solin.styles.icons import ICON_CLOUD_DOWNLOAD, ICON_SHIELD, make_icon
from solin.styles.theme import PALETTE
from solin.ui.qr_generation import QrGenerationSessionFactory


def _fingerprint_display_text(value: str) -> str:
    """Keep canonical byte groups while adding safe line-break opportunities."""

    octets = value.split(":")
    if len(octets) < 2 or any(len(octet) != 2 for octet in octets):
        return value
    return "  ".join(":".join(octets[index : index + 4]) for index in range(0, len(octets), 4))


@dataclass(frozen=True, slots=True)
class RemoteControlSetupPresentation:
    access_url: str
    setup_url: str
    verification_code: str
    fingerprint_sha256: str
    certificate_der: bytes

    @property
    def ready(self) -> bool:
        return bool(
            self.access_url.startswith("https://")
            and self.setup_url.startswith("https://")
            and self.verification_code
            and self.fingerprint_sha256
            and self.certificate_der
        )


class RemoteControlSetupDialog(QDialog):
    """Focused onboarding flow for adding one remote-control device."""

    def __init__(
        self,
        presentation: RemoteControlSetupPresentation,
        qr_factory: QrGenerationSessionFactory,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if not presentation.ready:
            raise ValueError("Remote-control setup information is incomplete")
        self._presentation = presentation
        self._qr_session = qr_factory.create(parent=self)
        self._qr_session.ready.connect(self._show_qr)
        self._qr_session.failed.connect(self._show_qr_error)

        self.setObjectName("RemoteControlSetupDialog")
        self.setWindowTitle(self.tr("Set up a device"))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setMinimumSize(560, 520)
        self.setMaximumWidth(680)
        self._build_ui()
        self._apply_theme()
        self.adjustSize()
        screen = parent.screen() if parent is not None else QGuiApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.resize(
                min(620, max(560, available.width() - 48)),
                min(620, max(520, available.height() - 80)),
            )
        self._qr_session.start(presentation.setup_url)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 24, 26, 22)
        root.setSpacing(18)

        header = QHBoxLayout()
        header.setSpacing(11)
        icon = QLabel()
        icon.setFixedSize(28, 28)
        icon.setPixmap(make_icon(ICON_SHIELD, 24, PALETTE.accent).pixmap(24, 24))
        copy = QVBoxLayout()
        copy.setSpacing(2)
        title = QLabel(self.tr("Set up Solin Remote"))
        title.setObjectName("SetupTitle")
        detail = QLabel(self.tr("Scan once to open the secure setup on your phone or tablet."))
        detail.setObjectName("SetupSubtitle")
        detail.setWordWrap(True)
        copy.addWidget(title)
        copy.addWidget(detail)
        header.addWidget(icon, alignment=Qt.AlignmentFlag.AlignTop)
        header.addLayout(copy, 1)
        root.addLayout(header)

        scroll = QScrollArea()
        scroll.setObjectName("SetupScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(14)

        connection = QFrame()
        connection.setObjectName("SetupConnectionCard")
        connection_layout = QHBoxLayout(connection)
        connection_layout.setContentsMargins(18, 18, 18, 18)
        connection_layout.setSpacing(20)

        self._qr_label = QLabel(self.tr("Generating QR code…"))
        self._qr_label.setObjectName("SetupQr")
        self._qr_label.setFixedSize(188, 188)
        self._qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr_label.setWordWrap(True)
        connection_layout.addWidget(self._qr_label)

        instructions = QVBoxLayout()
        instructions.setSpacing(10)
        step = QLabel(self.tr("1  Scan the QR code"))
        step.setObjectName("SetupStepTitle")
        instructions.addWidget(step)

        explanation = QLabel(
            self.tr(
                "Keep Solin open and connect both devices to the same local network. "
                "The guide explains the certificate and app installation."
            )
        )
        explanation.setObjectName("SetupBody")
        explanation.setWordWrap(True)
        instructions.addWidget(explanation)

        url_label = QLabel(self._presentation.setup_url)
        url_label.setObjectName("SetupUrl")
        url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        url_label.setWordWrap(True)
        instructions.addWidget(url_label)

        copy_url = QPushButton(self.tr("Copy setup address"))
        copy_url.clicked.connect(self._copy_setup_url)
        copy_url.setCursor(Qt.CursorShape.PointingHandCursor)
        instructions.addWidget(copy_url)
        instructions.addStretch()
        connection_layout.addLayout(instructions, 1)
        body_layout.addWidget(connection)

        first_opening = QLabel(
            self.tr(
                "On the first opening, the browser may show a privacy warning. Confirm that "
                "the local address matches, choose Advanced or Show Details, and continue only "
                "to this address. The warning disappears after the certificate is trusted."
            )
        )
        first_opening.setObjectName("SetupFirstOpeningNotice")
        first_opening.setWordWrap(True)
        body_layout.addWidget(first_opening)

        verification = QFrame()
        verification.setObjectName("SetupVerificationCard")
        verification_layout = QVBoxLayout(verification)
        verification_layout.setContentsMargins(16, 14, 16, 14)
        verification_layout.setSpacing(7)
        verification_caption = QLabel(self.tr("VERIFICATION CODE"))
        verification_caption.setObjectName("SetupCaption")
        verification_layout.addWidget(verification_caption)
        code = QLabel(self._presentation.verification_code)
        code.setObjectName("SetupCode")
        code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        verification_layout.addWidget(code)
        compare = QLabel(
            self.tr("Compare this code on the device before installing the certificate.")
        )
        compare.setObjectName("SetupBody")
        compare.setWordWrap(True)
        verification_layout.addWidget(compare)

        advanced_toggle = QToolButton()
        advanced_toggle.setObjectName("SetupAdvancedToggle")
        advanced_toggle.setText(self.tr("Show full SHA-256 fingerprint"))
        advanced_toggle.setCheckable(True)
        advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
        advanced_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        advanced_toggle.toggled.connect(self._toggle_fingerprint)
        verification_layout.addWidget(advanced_toggle)

        self._fingerprint = QLabel(
            _fingerprint_display_text(self._presentation.fingerprint_sha256)
        )
        self._fingerprint.setObjectName("SetupFingerprint")
        self._fingerprint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._fingerprint.setWordWrap(True)
        self._fingerprint.setMinimumWidth(0)
        self._fingerprint.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self._fingerprint.hide()
        verification_layout.addWidget(self._fingerprint)
        body_layout.addWidget(verification)
        body_layout.addStretch()
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        footer = QHBoxLayout()
        footer.setSpacing(9)
        save_certificate = QPushButton(self.tr("Save certificate…"))
        save_certificate.setIcon(make_icon(ICON_CLOUD_DOWNLOAD, 15, PALETTE.text_secondary))
        save_certificate.setIconSize(QSize(15, 15))
        save_certificate.clicked.connect(self._save_certificate)
        save_certificate.setCursor(Qt.CursorShape.PointingHandCursor)
        footer.addWidget(save_certificate)
        footer.addStretch()
        done = QPushButton(self.tr("Done"))
        done.setObjectName("SetupPrimaryButton")
        done.clicked.connect(self.accept)
        done.setDefault(True)
        done.setCursor(Qt.CursorShape.PointingHandCursor)
        footer.addWidget(done)
        root.addLayout(footer)

    def _apply_theme(self) -> None:
        self.setStyleSheet(
            f"""
            QDialog#RemoteControlSetupDialog {{
                background: {PALETTE.surface_card}; color: {PALETTE.text_primary};
            }}
            QScrollArea#SetupScroll {{ border: none; background: transparent; }}
            QScrollArea#SetupScroll > QWidget > QWidget {{ background: transparent; }}
            QScrollBar:vertical {{
                width: 8px; margin: 0; background: transparent;
            }}
            QScrollBar::handle:vertical {{
                min-height: 32px; border-radius: 4px; background: {PALETTE.border};
            }}
            QScrollBar::handle:vertical:hover {{ background: {PALETTE.border_strong}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
            QLabel {{ background: transparent; color: {PALETTE.text_secondary}; }}
            QLabel#SetupTitle {{ color: {PALETTE.text_primary}; font-size: 18px; font-weight: 700; }}
            QLabel#SetupSubtitle, QLabel#SetupBody {{ color: {PALETTE.text_muted}; font-size: 11px; }}
            QFrame#SetupConnectionCard, QFrame#SetupVerificationCard {{
                background: {PALETTE.surface_alt}; border: 1px solid {PALETTE.border_muted};
                border-radius: 12px;
            }}
            QLabel#SetupQr {{
                background: {PALETTE.white}; color: {PALETTE.text_dim};
                border: 1px solid {PALETTE.border}; border-radius: 10px; padding: 2px;
            }}
            QLabel#SetupStepTitle {{ color: {PALETTE.text_primary}; font-size: 13px; font-weight: 700; }}
            QLabel#SetupUrl, QLabel#SetupFingerprint {{
                color: {PALETTE.text_muted}; font-family: monospace; font-size: 10px;
            }}
            QLabel#SetupFirstOpeningNotice {{
                background: {PALETTE.warning_surface}; color: {PALETTE.warning_text};
                border: 1px solid {PALETTE.warning_border}; border-radius: 8px;
                padding: 8px 10px; font-size: 10px;
            }}
            QLabel#SetupCaption {{ color: {PALETTE.text_dim}; font-size: 9px; font-weight: 700; }}
            QLabel#SetupCode {{
                color: {PALETTE.text_primary}; font-family: monospace;
                font-size: 18px; font-weight: 700; letter-spacing: 1px;
            }}
            QPushButton {{
                min-height: 32px; padding: 0 12px; color: {PALETTE.text_secondary};
                background: {PALETTE.surface}; border: 1px solid {PALETTE.border};
                border-radius: 8px; font-weight: 600;
            }}
            QPushButton:hover {{ border-color: {PALETTE.accent_alt}; color: {PALETTE.text_primary}; }}
            QPushButton#SetupPrimaryButton {{
                background: {PALETTE.accent}; color: {PALETTE.text_on_accent};
                border-color: {PALETTE.accent}; min-width: 86px;
            }}
            QPushButton#SetupPrimaryButton:hover {{ background: {PALETTE.accent_hover}; }}
            QToolButton#SetupAdvancedToggle {{
                padding: 3px 0; color: {PALETTE.text_muted}; background: transparent;
                border: none; font-size: 10px; text-align: left;
            }}
            """
        )

    def _show_qr(self, png_data: bytes) -> None:
        pixmap = QPixmap()
        if not pixmap.loadFromData(png_data, "PNG"):
            self._show_qr_error()
            return
        self._qr_label.setText("")
        self._qr_label.setPixmap(
            pixmap.scaled(
                180,
                180,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
        )

    def _show_qr_error(self) -> None:
        self._qr_label.setPixmap(QPixmap())
        self._qr_label.setText(self.tr("Could not generate the QR code. Copy the address instead."))

    def _copy_setup_url(self) -> None:
        QGuiApplication.clipboard().setText(self._presentation.setup_url)

    def _toggle_fingerprint(self, visible: bool) -> None:
        toggle = self.sender()
        if isinstance(toggle, QToolButton):
            toggle.setArrowType(Qt.ArrowType.DownArrow if visible else Qt.ArrowType.RightArrow)
        self._fingerprint.setVisible(visible)
        self._fingerprint.updateGeometry()

    def _save_certificate(self) -> None:
        path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            self.tr("Save Solin certificate"),
            "solin-remote-root.cer",
            self.tr("Certificate files (*.cer)"),
        )
        if not path:
            return
        try:
            write_bytes_atomic(path, self._presentation.certificate_der, mode=0o644)
        except OSError:
            QMessageBox.critical(
                self,
                self.tr("Could not save certificate"),
                self.tr("Choose another location and try again."),
            )

    def closeEvent(self, event: QCloseEvent) -> None:
        self._qr_session.close()
        super().closeEvent(event)

    def done(self, result: int) -> None:
        self._qr_session.close()
        super().done(result)
