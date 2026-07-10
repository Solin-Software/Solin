from __future__ import annotations

from ...styles.icons import (
    ICON_AUTO_DOWNLOAD,
    ICON_MUSIC,
    ICON_NAV_MEETINGS,
    ICON_PLAY_PAUSE,
    ICON_SHIELD,
    ICON_SONG_ANNOUNCEMENT,
)


class MediaSectionMixin:
    """Builds and manages media playback/download settings."""

    def _build_media_card(self):
        card, lay = self._card()

        saved_dl = self._media_settings.auto_download_on_play()
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

        saved_meetings = self._media_settings.meetings_auto_download()
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

        saved_announce = self._media_settings.sjjm_announce_mode()
        row3, self._sjjm_announce_toggle, self._sjjm_announce_label, self._sjjm_announce_desc = \
            self._toggle_row(
                ICON_SONG_ANNOUNCEMENT,
                self.tr("Song Announcement Mode"),
                self.tr("Song starts muted for title display. Press play to start."),
                checked=saved_announce,
            )
        self._sjjm_announce_toggle.toggled.connect(self._on_sjjm_announce_toggled)
        lay.addWidget(row3)

        lay.addWidget(self._divider())

        saved_background_song = self._background_song_settings.is_enabled()
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

        saved_start_paused = self._media_settings.start_videos_paused()
        row5, self._start_paused_toggle, self._start_paused_label, self._start_paused_desc = \
            self._toggle_row(
                ICON_PLAY_PAUSE,
                self.tr("Start videos paused"),
                self.tr("Videos open paused so you can start them manually."),
                checked=saved_start_paused,
            )
        self._start_paused_toggle.toggled.connect(self._on_start_paused_toggled)
        lay.addWidget(row5)

        lay.addWidget(self._divider())

        row6, self._playback_protection_toggle, \
            self._playback_protection_label, self._playback_protection_desc = \
            self._toggle_row(
                ICON_SHIELD,
                self.tr("Playback protection"),
                self.tr(
                    "Prevents media changes and seeking while audio or video is playing. "
                    "Pause first to make changes."
                ),
                checked=self._playback_protection.enabled,
            )
        self._playback_protection_toggle.toggled.connect(
            self._playback_protection.set_enabled
        )
        self._playback_protection.enabledChanged.connect(
            self._sync_playback_protection_toggle
        )
        lay.addWidget(row6)
        return card

    def _on_auto_download_toggled(self, checked):
        self._media_settings.set_auto_download_on_play(checked)

    def get_auto_download_on_play(self):
        return self._media_settings.auto_download_on_play()

    def _on_meetings_dl_toggled(self, checked):
        self._media_settings.set_meetings_auto_download(checked)
        self.meetings_auto_download_toggled.emit(bool(checked))

    def get_meetings_auto_download(self):
        return self._media_settings.meetings_auto_download()

    def _on_sjjm_announce_toggled(self, checked):
        self._media_settings.set_sjjm_announce_mode(checked)

    def get_sjjm_announce_mode(self):
        return self._media_settings.sjjm_announce_mode()

    def _background_song_description(self) -> str:
        if not self._meeting_schedule_configured():
            return self.tr("Configure the meeting day/time before automatic playback can start.")
        return self.tr("Plays audio songs before configured meetings and fades out before start.")

    def _sync_background_song_desc(self) -> None:
        if hasattr(self, "_background_song_desc"):
            self._background_song_desc.setText(self._background_song_description())

    def _on_background_song_toggled(self, checked):
        self._background_song_settings.set_enabled(checked)
        self._sync_background_song_desc()
        self.background_song_toggled.emit(bool(checked))

    def get_background_song_enabled(self):
        return self._background_song_settings.is_enabled()

    def _on_start_paused_toggled(self, checked):
        self._media_settings.set_start_videos_paused(checked)

    def get_start_videos_paused(self):
        return self._media_settings.start_videos_paused()

    def get_playback_protection_enabled(self):
        return self._playback_protection.enabled

    def _sync_playback_protection_toggle(self):
        self._playback_protection_toggle.set_checked(
            self._playback_protection.enabled,
            animate=False,
        )
