from __future__ import annotations

from PySide6.QtCore import QCoreApplication

from solin.core.remote.notifications import NotificationWorker


def _application() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


class _NotificationSettings:
    def __init__(self, seen: set[str] | None = None) -> None:
        self._seen = set(seen or set())
        self.marked: list[str] = []

    def seen_ids(self) -> set[str]:
        return set(self._seen)

    def mark_seen(self, notification_id: str) -> None:
        self.marked.append(notification_id)
        self._seen.add(notification_id)


def test_notification_worker_uses_injected_settings_store() -> None:
    _application()
    settings = _NotificationSettings()
    worker = NotificationWorker("T", settings)

    notifications = worker._process(
        {
            "notifications": [
                {
                    "id": "notice-1",
                    "type": "info",
                    "content": {
                        "T": {"title": "Titulo", "detail": "Detalhe"},
                    },
                }
            ]
        }
    )

    assert [notification.id for notification in notifications] == ["notice-1"]
    assert settings.marked == ["notice-1"]


def test_notification_worker_respects_injected_seen_ids() -> None:
    _application()
    settings = _NotificationSettings({"notice-1"})
    worker = NotificationWorker("T", settings)

    notifications = worker._process(
        {
            "notifications": [
                {
                    "id": "notice-1",
                    "type": "info",
                    "content": {
                        "T": {"title": "Titulo", "detail": "Detalhe"},
                    },
                }
            ]
        }
    )

    assert notifications == []
    assert settings.marked == []
