from __future__ import annotations

from types import SimpleNamespace

from solin.ui import macos_layer


def test_corner_radius_skips_non_cocoa_qt_backend(monkeypatch) -> None:
    monkeypatch.setattr(macos_layer.sys, "platform", "darwin")
    monkeypatch.setattr(
        macos_layer,
        "QGuiApplication",
        SimpleNamespace(instance=lambda: SimpleNamespace(platformName=lambda: "offscreen")),
    )

    class Widget:
        def winId(self):
            raise AssertionError("offscreen Qt must not cross into the Cocoa bridge")

    assert macos_layer.apply_corner_radius(Widget(), 20) is False
