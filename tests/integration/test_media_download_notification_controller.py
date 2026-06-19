from solin.controllers.media_download_notification_controller import (
    MediaDownloadNotificationController,
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


class _CacheManager:
    def __init__(self):
        self.prefetch_error = _Signal()


class _MediaController:
    def __init__(self):
        self.playback_download_failed = _Signal()


class _Notifications:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, message, **kwargs):
        self.errors.append((message, kwargs))

    def warning(self, message, **kwargs):
        self.warnings.append((message, kwargs))


def test_media_download_error_is_reported_once_with_readable_filename():
    cache = _CacheManager()
    notifications = _Notifications()
    controller = MediaDownloadNotificationController(
        notifications,
        cache_manager=cache,
    )

    controller.start()
    controller.start()
    cache.prefetch_error.emit(
        "https://cdn.example/media/My%20Song.mp4?download=1",
        "network unavailable",
    )

    assert len(cache.prefetch_error.slots) == 1
    assert notifications.errors == [
        (
            "Could not download My Song.mp4.\nnetwork unavailable",
            {
                "title": "Download failed",
                "dedupe_key": (
                    "media-prefetch:"
                    "https://cdn.example/media/My%20Song.mp4?download=1"
                ),
            },
        )
    ]

    controller.stop()
    assert cache.prefetch_error.slots == []


def test_playback_download_error_warns_that_streaming_continues():
    cache = _CacheManager()
    media = _MediaController()
    notifications = _Notifications()
    controller = MediaDownloadNotificationController(
        notifications,
        cache_manager=cache,
        media_controller=media,
    )

    controller.start()
    controller.start()
    media.playback_download_failed.emit(
        "https://cdn.example/media/My%20Song.mp4?download=1",
        "[Errno 28] No space left on device",
        True,
    )

    assert len(media.playback_download_failed.slots) == 1
    assert notifications.warnings == [
        (
            "Could not save My Song.mp4 locally because there is not enough disk space.\n"
            "Playback will continue by streaming.\n"
            "[Errno 28] No space left on device",
            {
                "title": "Local copy failed",
                "dedupe_key": (
                    "media-playback-download:"
                    "https://cdn.example/media/My%20Song.mp4?download=1:"
                    "[Errno 28] No space left on device"
                ),
            },
        )
    ]

    controller.stop()
    assert media.playback_download_failed.slots == []
