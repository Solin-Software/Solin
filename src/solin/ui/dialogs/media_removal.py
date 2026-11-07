"""Shared confirmation UI for structured media lists."""

from enum import Enum, auto

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QMessageBox, QWidget

from solin.core.foundation import constants


class ContainerRemovalChoice(Enum):
    CANCEL = auto()
    KEEP_MEDIA = auto()
    DELETE_MEDIA = auto()


def confirm_additional_media_removal(parent: QWidget | None, name: str, count: int) -> bool:
    return QMessageBox.question(
        parent,
        QCoreApplication.translate("MediaRemoval", "Delete additional media"),
        QCoreApplication.translate(
            "MediaRemoval", 'Delete additional media from "{name}"?\nMedia items: {count}',
        ).replace("{count}", str(count)).replace("{name}", name),
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        QMessageBox.StandardButton.Cancel,
    ) == QMessageBox.StandardButton.Yes


def warn_playing_media_removal(parent: QWidget | None, titles: str) -> None:
    QMessageBox.warning(
        parent,
        QCoreApplication.translate("MediaRemoval", "Media is playing"),
        QCoreApplication.translate(
            "MediaRemoval",
            'Cannot remove "{title}" while it is currently playing.\n'
            'Stop the projection and try again.',
        ).replace("{title}", titles),
    )


def confirm_container_removal(
    parent: QWidget | None, name: str, *, subsection: bool,
) -> ContainerRemovalChoice:
    title = (
        QCoreApplication.translate("_PlaylistEditView", "Delete subsection")
        if subsection else QCoreApplication.translate("_PlaylistEditView", "Delete section")
    )
    if not constants.CONFIRM_CONTAINER_MEDIA_DELETION:
        message = (
            QCoreApplication.translate(
                "MediaRemoval", 'Delete subsection "{name}"?\nItems inside will be kept.',
            ) if subsection else QCoreApplication.translate(
                "PlaylistEditView", 'Delete section "{name}"?\nItems inside will be kept.',
            )
        ).replace("{name}", name)
        reply = QMessageBox.question(
            parent,
            title,
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        return (
            ContainerRemovalChoice.KEEP_MEDIA
            if reply == QMessageBox.StandardButton.Yes else ContainerRemovalChoice.CANCEL
        )
    dialog = QMessageBox(parent)
    dialog.setWindowTitle(title)
    dialog.setIcon(QMessageBox.Icon.Question)
    dialog.setText(QCoreApplication.translate(
        "MediaRemoval", 'Delete "{name}" and its media?',
    ).replace("{name}", name))
    remove = dialog.addButton(
        QCoreApplication.translate("MediaRemoval", "Delete media"),
        QMessageBox.ButtonRole.DestructiveRole,
    )
    keep = dialog.addButton(
        QCoreApplication.translate("MediaRemoval", "Keep media"),
        QMessageBox.ButtonRole.ActionRole,
    )
    cancel = dialog.addButton(QMessageBox.StandardButton.Cancel)
    dialog.setDefaultButton(cancel)
    dialog.setEscapeButton(cancel)
    dialog.exec()
    if dialog.clickedButton() is remove:
        return ContainerRemovalChoice.DELETE_MEDIA
    if dialog.clickedButton() is keep:
        return ContainerRemovalChoice.KEEP_MEDIA
    return ContainerRemovalChoice.CANCEL
