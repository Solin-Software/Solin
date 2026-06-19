from solin.controllers.media_playback_notification_controller import (
    MediaPlaybackNotificationController,
)


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def disconnect(self, slot):
        self.slots.remove(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


class _MediaController:
    def __init__(self):
        self.current_url = "https://cdn.example/media/My%20Song.mp4?download=1"
        self.error_occurred = _Signal()


class _Notifications:
    def __init__(self):
        self.errors = []

    def error(self, message, **kwargs):
        self.errors.append((message, kwargs))


def test_media_playback_error_notifies_and_stops_projection():
    media = _MediaController()
    notifications = _Notifications()
    stops = []
    controller = MediaPlaybackNotificationController(
        notifications,
        media,
        current_title=lambda: "Current playback",
        stop_projection=lambda: stops.append("stop"),
    )

    controller.start()
    controller.start()
    media.error_occurred.emit("I/O error")

    assert len(media.error_occurred.slots) == 1
    assert notifications.errors == [
        (
            "Could not play Current playback.\nI/O error",
            {
                "title": "Playback failed",
                "dedupe_key": (
                    "media-playback:"
                    "https://cdn.example/media/My%20Song.mp4?download=1:I/O error"
                ),
            },
        )
    ]
    assert stops == ["stop"]

    controller.stop()
    assert media.error_occurred.slots == []
