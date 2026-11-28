"""Qualify packaged QML modules through the application's shared host."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence


def _verify() -> dict[str, str]:
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtQuickWidgets import QQuickWidget
    from PySide6.QtWidgets import QApplication

    from solin.ui.qml.host import configure_qml_host
    from solin.ui.qml.loader import QML_MODULE_URI, QML_SHELL_MODULE_URI, QmlLoadState
    from solin.ui.qml.quick_toolbar import QUICK_TOOLBAR_ICON_SVGS, QuickToolbarBridge
    from solin.ui.qml.svg_icons import SvgIconProvider

    app = QApplication.instance() or QApplication(["solin-qml-verification"])
    bridge = QuickToolbarBridge()
    widgets: list[QQuickWidget] = []
    sources: dict[str, str] = {}
    try:
        for name, module, deferred in (
            ("QuickAccessToolbar", QML_SHELL_MODULE_URI, False),
            ("TimerToggle", QML_MODULE_URI, True),
        ):
            widget = QQuickWidget()
            widgets.append(widget)
            options = {}
            if name == "QuickAccessToolbar":
                options = {
                    "context_properties": {"bridge": bridge},
                    "image_providers": {
                        "icons": SvgIconProvider(QUICK_TOOLBAR_ICON_SVGS, default_icon="monitor"),
                    },
                    "mouse_tracking": True,
                }
            handle = configure_qml_host(
                widget, type_name=name, module_uri=module, clear_color="transparent",
                defer_load=deferred, **options,
            )
            errors: list[str] = []
            handle.failed.connect(errors.append)
            if deferred:
                loop = QEventLoop()
                timer = QTimer()
                timer.setSingleShot(True)
                timer.timeout.connect(loop.quit)
                handle.completed.connect(loop.quit)
                handle.failed.connect(loop.quit)
                handle.start()
                if not handle.is_terminal:
                    timer.start(10000)
                    loop.exec()
                timer.stop()
            if handle.state != QmlLoadState.READY or widget.rootObject() is None:
                details = errors + [error.toString() for error in widget.errors()]
                raise RuntimeError(
                    f"{module}.{name} failed to load (state: {handle.state}): "
                    f"{'; '.join(details) or 'no QML error reported'}"
                )
            source = widget.source().toString()
            expected = f"qrc:/qt/qml/{module.replace('.', '/')}/{name}.qml"
            if source != expected:
                raise RuntimeError(f"{module}.{name} loaded from {source!r}, expected {expected!r}")
            sources[name] = source
            if name == "TimerToggle":
                root = widget.rootObject()
                if not root.setProperty("checked", True) or root.property("checked") is not True:
                    raise RuntimeError("TimerToggle did not expose its checked property")
        app.processEvents()
        return sources
    finally:
        for widget in widgets:
            widget.close()
            widget.deleteLater()
        app.processEvents()


def main(arguments: Sequence[str]) -> int:
    result: dict[str, object] = {"schema": "solin.qml-runtime.v1", "ok": False}
    exit_code = 1
    try:
        if list(arguments) != ["--verify-qml-runtime"]:
            raise ValueError("Usage: --verify-qml-runtime")
        result.update(ok=True, sources=_verify())
        exit_code = 0
    except Exception as error:  # noqa: BLE001 - qualification process boundary
        result["error"] = type(error).__name__
        print(f"QML runtime qualification failed: {error}", file=sys.stderr, flush=True)
        if isinstance(error, ValueError):
            exit_code = 2
    print(json.dumps(result, separators=(",", ":")), flush=True)
    return exit_code
