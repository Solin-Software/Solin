"""Unit tests for the media bootstrap composition (engine-agnostic wiring)."""

from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtCore import QObject

from solin.bootstrap.media import MediaComposition


def _composition(tmp_path) -> MediaComposition:
    (tmp_path / "media").mkdir()
    (tmp_path / "thumb").mkdir()
    return MediaComposition(QObject(), tmp_path / "media", tmp_path / "thumb")


def test_shutdown_tears_down_obs_runtime_in_obs_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLIN_MEDIA_ENGINE", "obs")
    import solin.core.media.obs_runtime as rt_mod

    calls: list[str] = []
    monkeypatch.setattr(
        rt_mod, "obs_runtime", lambda: SimpleNamespace(shutdown=lambda: calls.append("shutdown"))
    )

    _composition(tmp_path).shutdown()

    assert calls == ["shutdown"]  # native runtime torn down deterministically


def test_shutdown_skips_obs_runtime_for_qt_engine(monkeypatch, tmp_path):
    monkeypatch.delenv("SOLIN_MEDIA_ENGINE", raising=False)
    import solin.core.media.obs_runtime as rt_mod

    calls: list[str] = []
    monkeypatch.setattr(
        rt_mod, "obs_runtime", lambda: SimpleNamespace(shutdown=lambda: calls.append("shutdown"))
    )

    _composition(tmp_path).shutdown()

    assert calls == []  # default Qt engine: no libobs runtime to tear down
