"""Unit tests for the media bootstrap composition (engine-agnostic wiring)."""

from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtCore import QObject

from solin.bootstrap.media import MediaComposition


def _composition(tmp_path) -> MediaComposition:
    (tmp_path / "media").mkdir()
    (tmp_path / "thumb").mkdir()
    return MediaComposition(QObject(), tmp_path / "media", tmp_path / "thumb")


def test_shutdown_tears_down_obs_runtime(monkeypatch, tmp_path):
    """libobs is the only engine, so its runtime is always torn down."""
    import solin.core.media.obs_runtime as rt_mod

    calls: list[str] = []
    monkeypatch.setattr(
        rt_mod, "obs_runtime", lambda: SimpleNamespace(shutdown=lambda: calls.append("shutdown"))
    )

    _composition(tmp_path).shutdown()

    assert calls == ["shutdown"]  # native runtime torn down deterministically


def test_shutdown_is_unaffected_by_the_legacy_engine_variable(monkeypatch, tmp_path):
    """SOLIN_MEDIA_ENGINE no longer selects anything.

    It used to switch between libobs and a Qt engine. Now that libobs is the only
    engine, a stale value left in an operator's environment must not resurrect a
    code path that no longer exists.
    """
    monkeypatch.setenv("SOLIN_MEDIA_ENGINE", "qt")
    import solin.core.media.obs_runtime as rt_mod

    calls: list[str] = []
    monkeypatch.setattr(
        rt_mod, "obs_runtime", lambda: SimpleNamespace(shutdown=lambda: calls.append("shutdown"))
    )

    _composition(tmp_path).shutdown()

    assert calls == ["shutdown"]
