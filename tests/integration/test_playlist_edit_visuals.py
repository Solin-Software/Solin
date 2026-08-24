from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication

from solin.core.meetings.colors import hsl_to_hex
from solin.ui.qml.playlist.visuals import (
    PLAYLIST_ICON_SVGS,
    UNSECTIONED_CARD_BACKGROUND,
    PlaylistIconProvider,
    PlaylistThumbnailProvider,
    playlist_media_badge,
    round_playlist_pixmap,
)
from solin.ui.thumbnail_images import image_source_aspect_ratio

_APP = QApplication.instance() or QApplication([])


def test_playlist_edit_visual_providers_are_available():
    assert PlaylistThumbnailProvider is not None
    assert PlaylistIconProvider is not None
    assert round_playlist_pixmap is not None


def test_playlist_edit_visual_helpers_stay_stable():
    assert hsl_to_hex(0, 100, 50) == "#ff0000"
    assert playlist_media_badge("image") == "Image"
    assert playlist_media_badge("audio") == "Audio"
    assert playlist_media_badge("video") == "Video"
    assert UNSECTIONED_CARD_BACKGROUND == "transparent"


def test_playlist_icons_stay_embedded_in_python():
    expected = {
        "grip",
        "more",
        "play_all",
        "play",
        "pause",
        "shuffle",
        "back",
        "plus",
        "export",
        "folder_link",
        "refresh",
        "close",
        "save",
        "cloud",
        "section",
        "marker",
        "media_trim",
        "chevron_down",
        "chevron_up",
        "edit",
        "trash",
        "palette",
        "media_image",
        "media_audio",
        "media_video",
    }

    assert set(PLAYLIST_ICON_SVGS) == expected
    assert all(svg.startswith("<svg") for svg in PLAYLIST_ICON_SVGS.values())


def test_meeting_restore_button_visibility_tooltip_and_action_are_wired():
    source = Path("src/solin/qml/MeetingDetailView.qml").read_text(encoding="utf-8")

    assert "visible: root.hasController && controller.canonicalRestoreAvailable" in source
    assert '"Restore official meeting content"' in source
    assert "controller.restoreCanonicalContent()" in source


def test_whole_meeting_transitions_use_a_blocking_indeterminate_progress_bar():
    source = Path("src/solin/qml/MeetingDetailView.qml").read_text(encoding="utf-8")

    assert "visible: root.hasController && controller.syncBusy" in source
    assert "controller.syncBusyMessage" in source
    assert "indeterminate: true" in source


def test_media_preparation_feedback_stays_local_to_its_tree_row():
    source = Path("src/solin/qml/PlaylistTreeView.qml").read_text(encoding="utf-8")

    assert "running: mediaRoot.operationActive" in source
    assert "mediaRoot.operationProgress" in source
    assert "root.playlistController.cancelOperation(mediaRoot.operationId)" in source
    assert "root.playlistController.retryOperation(mediaRoot.operationId)" in source


def test_shared_tree_keeps_shell_theme_contract_and_native_drag_feedback():
    source = Path("src/solin/qml/PlaylistTreeView.qml").read_text(encoding="utf-8")

    assert "property color previewBg:" in source
    assert "property color previewBorder:" in source
    assert 'objectName: "dragOverlay"' in source
    assert 'objectName: "dragPlaceholder"' in source
    assert "draggable.beginVisualDrag()" in source


def test_drag_grips_and_overflow_buttons_share_secondary_control_colors():
    source = Path("src/solin/qml/PlaylistTreeView.qml").read_text(encoding="utf-8")

    assert "component DragGrip: Image" in source
    assert "root.secondaryControl))" in source
    assert "root.secondaryControlHover" in source
    assert source.count("colorHex: root.iconHex(root.secondaryControl)") == 2
    assert source.count("hoverColorHex: root.iconHex(root.secondaryControlHover)") == 2


def test_thumbnail_provider_can_return_uncropped_image_for_framing_editor():
    portrait = QPixmap(100, 200)
    portrait.fill(QColor("red"))
    provider = PlaylistThumbnailProvider({"portrait": portrait})

    fitted = provider.requestPixmap("portrait/0/fit", None, QSize(200, 100))
    cropped = provider.requestPixmap("portrait/0", None, QSize(200, 100))

    assert fitted.size() == QSize(50, 100)
    assert cropped.size() == QSize(200, 100)


def test_framing_uses_original_aspect_instead_of_scaled_thumbnail_rounding(
    tmp_path,
):
    portrait = QPixmap(90, 160)
    portrait.fill(QColor("red"))
    provider = PlaylistThumbnailProvider({"portrait": portrait})
    source_path = tmp_path / "portrait.png"
    assert portrait.save(str(source_path), "PNG")

    fitted = provider.requestPixmap("portrait/0/fit", None, QSize(200, 113))

    assert fitted.size() == QSize(63, 113)
    assert fitted.width() / fitted.height() < 9 / 16
    assert image_source_aspect_ratio(pixmap=portrait) == 9 / 16
    assert image_source_aspect_ratio(path=str(source_path)) == 9 / 16
