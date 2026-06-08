from __future__ import annotations

from ...core.foundation.settings_keys import SettingsKey
from ...styles.icons import (
    ICON_AUTO_DOWNLOAD,
    ICON_MUSIC,
    ICON_NAV_MEETINGS,
    ICON_PLAY_PAUSE,
)


class MediaSectionMixin:
    """Builds and manages media playback/download settings."""

    def _build_media_card(self):
        card, lay = self._card()

        saved_dl = self._prefs.value(SettingsKey.AUTO_DOWNLOAD_ON_PLAY, True, bool)
        row1, self._auto_dl_toggle, self._auto_dl_label, self._auto_dl_desc = \
            self._toggle_row(
                ICON_AUTO_DOWNLOAD,
                self.tr("Auto-download on play"),
                self.tr("Downloads the playing media for offline use."),
                checked=saved_dl,
            )
        self._auto_dl_toggle.toggled.connect(self._on_auto_download_toggled)
        lay.addWidget(row1)

        lay.addWidget(self._divider())

        saved_meetings = self._prefs.value(SettingsKey.MEETINGS_AUTO_DOWNLOAD, False, bool)
        row2, self._meetings_dl_toggle, self._meetings_dl_label, self._meetings_dl_desc = \
            self._toggle_row(
                ICON_NAV_MEETINGS,
                self.tr("Auto-download weekly study"),
                self.tr("Downloads this week\u2019s and next week\u2019s meeting media."),
                checked=saved_meetings,
            )
        self._meetings_dl_toggle.toggled.connect(self._on_meetings_dl_toggled)
        lay.addWidget(row2)

        lay.addWidget(self._divider())

        saved_announce = self._prefs.value(SettingsKey.SJJM_ANNOUNCE_MODE, False, bool)
        row3, self._sjjm_announce_toggle, self._sjjm_announce_label, self._sjjm_announce_desc = \
            self._toggle_row(
                ICON_MUSIC,
                self.tr("Song Announcement Mode"),
                self.tr("Song starts muted for title display. Press play to start."),
                checked=saved_announce,
            )
        self._sjjm_announce_toggle.toggled.connect(self._on_sjjm_announce_toggled)
        lay.addWidget(row3)

        lay.addWidget(self._divider())

        saved_background_song = self._prefs.value(
            SettingsKey.BACKGROUND_SONG_ENABLED, False, bool
        )
        row4, self._background_song_toggle, self._background_song_label, \
            self._background_song_desc = self._toggle_row(
                ICON_MUSIC,
                self.tr("Automatic background song"),
                self._background_song_description(),
                checked=saved_background_song,
            )
        self._background_song_toggle.toggled.connect(self._on_background_song_toggled)
        lay.addWidget(row4)

        lay.addWidget(self._divider())

        saved_start_paused = self._prefs.value(SettingsKey.START_VIDEOS_PAUSED, False, bool)
        row5, self._start_paused_toggle, self._start_paused_label, self._start_paused_desc = \
            self._toggle_row(
                ICON_PLAY_PAUSE,
                self.tr("Start videos paused"),
                self.tr("Videos open paused so you can start them manually."),
                checked=saved_start_paused,
            )
        self._start_paused_toggle.toggled.connect(self._on_start_paused_toggled)
        lay.addWidget(row5)
        return card

    def _on_auto_download_toggled(self, checked):
        self._prefs.setValue(SettingsKey.AUTO_DOWNLOAD_ON_PLAY, checked)

    def get_auto_download_on_play(self):
        return self._prefs.value(SettingsKey.AUTO_DOWNLOAD_ON_PLAY, True, bool)

    def _on_meetings_dl_toggled(self, checked):
        self._prefs.setValue(SettingsKey.MEETINGS_AUTO_DOWNLOAD, checked)

    def get_meetings_auto_download(self):
        return self._prefs.value(SettingsKey.MEETINGS_AUTO_DOWNLOAD, False, bool)

    def _on_sjjm_announce_toggled(self, checked):
        self._prefs.setValue(SettingsKey.SJJM_ANNOUNCE_MODE, checked)

    def get_sjjm_announce_mode(self):
        return self._prefs.value(SettingsKey.SJJM_ANNOUNCE_MODE, False, bool)

    def _background_song_description(self) -> str:
        if not self._meeting_schedule_configured():
            return self.tr("Configure the meeting day/time before automatic playback can start.")
        return self.tr("Plays audio songs before configured meetings and fades out before start.")

    def _sync_background_song_desc(self) -> None:
        if hasattr(self, "_background_song_desc"):
            self._background_song_desc.setText(self._background_song_description())

    def _on_background_song_toggled(self, checked):
        self._prefs.setValue(SettingsKey.BACKGROUND_SONG_ENABLED, checked)
        self._sync_background_song_desc()
        self.background_song_toggled.emit(bool(checked))

    def get_background_song_enabled(self):
        return self._prefs.value(SettingsKey.BACKGROUND_SONG_ENABLED, False, bool)

    def _on_start_paused_toggled(self, checked):
        self._prefs.setValue(SettingsKey.START_VIDEOS_PAUSED, checked)

    def get_start_videos_paused(self):
        return self._prefs.value(SettingsKey.START_VIDEOS_PAUSED, False, bool)
