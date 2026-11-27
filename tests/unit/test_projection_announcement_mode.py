from unittest.mock import Mock

import pytest
from PySide6.QtCore import QObject, Signal

from solin.controllers.projection_playback_operations import ProjectionPlaybackOperations


class Media(QObject):
    volume_changed = Signal(float)
    position_changed = Signal(int)

    def __init__(self):
        super().__init__()
        self.volume = 0.8
        self.playback_rate = 1.0
        self.suppressed = False
        self.pause = Mock()
        self.play = Mock()
        self.seek = Mock()
        self.toggle_play_pause = Mock()

    def set_volume(self, value):
        self.volume = value
        self.volume_changed.emit(value)

    def set_playback_rate(self, value):
        self.playback_rate = value

    def set_audio_suppressed(self, value):
        self.suppressed = value


class Protection(QObject):
    lockedChanged = Signal()
    locked = False

    def request_seek(self, position):
        self.seek(position)
        return True

    def __init__(self):
        super().__init__()
        self.seek = Mock()


@pytest.fixture
def playback():
    media = Media()
    protection = Protection()
    settings = Mock()
    settings.playback_order.return_value = "off"
    settings.loop_enabled.return_value = False
    settings.volume.return_value = 0.8
    settings.speed.return_value = 1.0
    return ProjectionPlaybackOperations(media, settings, protection), media, protection


def test_announcement_lead_in_gates_transport_until_media_position(playback):
    operations, media, protection = playback
    operations.begin_announcement()
    assert media.suppressed and operations.transport_blocked and operations.seek_blocked
    operations.toggle()
    assert not operations.pause() and not operations.resume() and not operations.seek(200)
    media.position_changed.emit(3499)
    media.pause.assert_not_called()
    media.position_changed.emit(3500)
    media.pause.assert_called_once()
    assert not operations.transport_blocked and operations.seek_blocked and media.suppressed
    operations.toggle()
    media.seek.assert_called_once_with(0)
    media.play.assert_called_once()
    assert not media.suppressed and not operations.seek_blocked
    assert operations.seek(200)
    protection.seek.assert_called_once_with(200)


@pytest.mark.parametrize("success", [False, True])
def test_share_wait_blocks_commands_and_resolves(playback, success):
    operations, media, protection = playback
    operations.begin_auto_share_wait()
    operations.toggle()
    assert not operations.pause() and not operations.resume() and not operations.seek(10)
    media.toggle_play_pause.assert_not_called()
    protection.seek.assert_not_called()
    operations.resolve_auto_share_wait(success)
    assert not operations.transport_blocked and not operations.seek_blocked
    assert media.play.call_count == int(success)
    operations.resolve_auto_share_wait(success)
    assert media.play.call_count == int(success)


@pytest.mark.parametrize("success", [False, True])
def test_share_wait_defers_announcement_gate(playback, success):
    operations, media, _ = playback
    operations.begin_auto_share_wait()
    operations.begin_announcement()
    media.position_changed.emit(4000)
    media.pause.assert_not_called()
    operations.resolve_auto_share_wait(success)
    media.play.assert_called_once()
    assert operations.transport_blocked and operations.seek_blocked
    media.position_changed.emit(4000)
    media.pause.assert_called_once()


def test_reset_restores_audio_and_transport(playback):
    operations, media, _ = playback
    operations.begin_announcement()
    operations.begin_auto_share_wait()
    operations.reset_transport()
    assert not media.suppressed and not operations.transport_blocked and not operations.seek_blocked
    media.position_changed.emit(4000)
    media.pause.assert_not_called()


def test_protection_blocks_seek_but_not_toggle(playback):
    operations, media, protection = playback
    protection.locked = True
    assert not operations.seek(123)
    operations.toggle()
    protection.seek.assert_not_called()
    media.toggle_play_pause.assert_called_once()


def test_automatic_playback_indicator_tracks_queue_and_loop(playback):
    operations, _, _ = playback
    operations.commit_queue(operations.new_queue([{}, {}], order="next"))
    assert operations.has_pending_automatic_playback
    operations.commit_queue(operations.queue.select(1))
    assert not operations.has_pending_automatic_playback
    operations.set_order("random")
    assert operations.has_pending_automatic_playback
    operations.commit_queue(operations.queue.select(0))
    assert not operations.has_pending_automatic_playback
    operations.toggle_loop()
    assert operations.has_pending_automatic_playback
