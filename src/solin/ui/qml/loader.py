from __future__ import annotations

from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QResource, Signal
from PySide6.QtQml import QQmlComponent, QQmlEngine, QQmlIncubator
from PySide6.QtQuickWidgets import QQuickWidget

QML_MODULE_URI = "Solin"
QML_SHELL_MODULE_URI = "Solin.Shell"
_QML_RESOURCE_ARCHIVE = "solin-qml.rcc"
_QML_RESOURCE_PREFIX = ":/qt/qml"


class QmlLoadState(StrEnum):
    PENDING = "pending"
    LOADING = "loading"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


@cache
def _register_qml_archive(archive: Path) -> None:
    if not QResource.registerResource(str(archive)):
        raise RuntimeError(f"Could not register Solin QML resource archive: {archive}")
    for module_uri in (QML_MODULE_URI, QML_SHELL_MODULE_URI):
        module_path = module_uri.replace(".", "/")
        if not QResource(f"{_QML_RESOURCE_PREFIX}/{module_path}/qmldir").isValid():
            QResource.unregisterResource(str(archive))
            raise RuntimeError(f"Missing QML module {module_uri} in resource archive: {archive}")


def _configure_import_root(engine: QQmlEngine, qml_root: Path) -> None:
    archive = qml_root / _QML_RESOURCE_ARCHIVE
    has_source = (qml_root / QML_MODULE_URI / "qmldir").is_file()
    if archive.exists():
        if has_source:
            raise RuntimeError(f"Both QML sources and a resource archive exist in: {qml_root}")
        _register_qml_archive(archive)
        return
    if not has_source:
        raise RuntimeError(f"No Solin QML module sources or resource archive in: {qml_root}")
    import_root = qml_root.as_posix()
    if import_root not in engine.importPathList():
        engine.addImportPath(import_root)


class _LoadIncubator(QQmlIncubator):
    def __init__(self, owner: QmlLoadHandle) -> None:
        super().__init__(QQmlIncubator.IncubationMode.Asynchronous)
        self._owner = owner

    def statusChanged(self, status: QQmlIncubator.Status) -> None:  # noqa: N802 - Qt override
        self._owner._incubator_status_changed(status)


class QmlLoadHandle(QObject):
    """Cancelable QML compilation/incubation installed into one stable host."""

    completed = Signal()
    failed = Signal(str)
    state_changed = Signal(str)

    def __init__(
        self,
        widget: QQuickWidget,
        module_uri: str,
        type_name: str,
        *,
        asynchronous: bool,
    ) -> None:
        super().__init__(widget)
        self._widget = widget
        self._module_uri = module_uri
        self._type_name = type_name
        self._asynchronous = asynchronous
        self._state = QmlLoadState.PENDING
        self._failure_message: str | None = None
        self._component: QQmlComponent | None = None
        self._incubator: _LoadIncubator | None = None

    @property
    def state(self) -> QmlLoadState:
        return self._state

    @property
    def failure_message(self) -> str | None:
        return self._failure_message

    @property
    def is_terminal(self) -> bool:
        return self._state in {
            QmlLoadState.READY,
            QmlLoadState.FAILED,
            QmlLoadState.CANCELLED,
        }

    def start(self) -> None:
        if self._state is not QmlLoadState.PENDING:
            return
        self._set_state(QmlLoadState.LOADING)
        try:
            qml_root = Path(__file__).resolve().parents[2] / "qml"
            _configure_import_root(self._widget.engine(), qml_root)
            if self._asynchronous:
                self._begin_asynchronous_load()
            else:
                self._load_synchronously()
        except Exception as error:  # noqa: BLE001 - QML loader boundary
            self._fail(str(error))

    def _load_synchronously(self) -> None:
        self._widget.loadFromModule(self._module_uri, self._type_name)
        if self._widget.status() is not QQuickWidget.Status.Ready:
            errors = "; ".join(str(error) for error in self._widget.errors())
            self._fail(errors or f"Could not load {self._module_uri}.{self._type_name}")
            return
        self._set_state(QmlLoadState.READY)
        self.completed.emit()

    def cancel(self) -> None:
        if self.is_terminal:
            return
        if self._incubator is not None:
            self._incubator.clear()
        self._component = None
        self._incubator = None
        self._set_state(QmlLoadState.CANCELLED)

    def _begin_asynchronous_load(self) -> None:
        component = QQmlComponent(
            self._widget.engine(),
            self._module_uri,
            self._type_name,
            QQmlComponent.CompilationMode.Asynchronous,
            self,
        )
        self._component = component
        component.statusChanged.connect(self._component_status_changed)
        self._component_status_changed(component.status())

    def _component_status_changed(self, status: QQmlComponent.Status) -> None:
        if self._state is not QmlLoadState.LOADING or self._component is None:
            return
        if status is QQmlComponent.Status.Error:
            self._fail(self._component_error_text())
            return
        if status is not QQmlComponent.Status.Ready or self._incubator is not None:
            return
        self._incubator = _LoadIncubator(self)
        self._component.create(self._incubator, self._widget.rootContext())

    def _incubator_status_changed(self, status: QQmlIncubator.Status) -> None:
        if self._state is not QmlLoadState.LOADING or self._incubator is None:
            return
        if status is QQmlIncubator.Status.Error:
            errors = "; ".join(str(error) for error in self._incubator.errors())
            self._fail(errors or "QML incubation failed")
            return
        if status is not QQmlIncubator.Status.Ready or self._component is None:
            return
        root = self._incubator.object()
        if root is None:
            self._fail("QML incubation completed without a root object")
            return
        self._widget.setContent(self._component.url(), self._component, root)
        self._set_state(QmlLoadState.READY)
        self.completed.emit()

    def _component_error_text(self) -> str:
        if self._component is None:
            return "QML component failed to load"
        errors: list[Any] = self._component.errors()
        return "; ".join(str(error) for error in errors) or "QML component failed to load"

    def _fail(self, message: str) -> None:
        self._failure_message = message
        self._set_state(QmlLoadState.FAILED)
        self.failed.emit(message)

    def _set_state(self, state: QmlLoadState) -> None:
        if self._state is state:
            return
        self._state = state
        self.state_changed.emit(state.value)


__all__ = [
    "QML_MODULE_URI",
    "QML_SHELL_MODULE_URI",
    "QmlLoadHandle",
    "QmlLoadState",
]
