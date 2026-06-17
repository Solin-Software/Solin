from __future__ import annotations

from PySide6.QtCore import QCoreApplication

from solin.core.remote import updates as updates_module
from solin.core.remote.updates import UpdateWorker


def _application() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


def test_update_worker_uses_injected_install_id_provider(monkeypatch) -> None:
    _application()
    captured: dict[str, object] = {}

    def fake_get_json(url: str, **kwargs: object) -> dict[str, object]:
        captured["url"] = url
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(updates_module, "get_json", fake_get_json)
    worker = UpdateWorker(lambda: "install-1")

    worker.run()

    assert captured["params"] == {
        "v": updates_module.APP_VERSION,
        "id": "install-1",
        "platform": updates_module.APP_PLATFORM,
    }
