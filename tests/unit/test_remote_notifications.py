from __future__ import annotations

from PySide6.QtCore import QCoreApplication

from solin.core.remote import notifications as notifications_module
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
    worker = NotificationWorker("T", settings, lambda: "install-1")

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
    worker = NotificationWorker("T", settings, lambda: "install-1")

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


def test_notification_worker_uses_injected_install_id_provider(monkeypatch) -> None:
    _application()
    settings = _NotificationSettings()
    captured: dict[str, object] = {}

    def fake_get_json(url: str, **kwargs: object) -> dict[str, object]:
        captured["url"] = url
        captured.update(kwargs)
        return {"notifications": []}

    monkeypatch.setattr(notifications_module, "get_json", fake_get_json)
    worker = NotificationWorker("T", settings, lambda: "install-1")

    worker.run()

    assert captured["params"] == {
        "id": "install-1",
        "v": notifications_module.APP_VERSION,
        "platform": notifications_module.APP_PLATFORM,
    }
