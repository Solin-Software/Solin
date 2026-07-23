from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtQml import QQmlComponent, QQmlIncubator

QML_MODULE_URI = "Solin"
QML_CACHE_MANIFEST = "qml-manifest.json"


class QmlLoadState(StrEnum):
    PENDING = "pending"
    LOADING = "loading"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class QmlTypeLocation:
    source_url: QUrl | None
    import_root: str | None


def _package_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def qml_import_roots() -> list[str]:
    package_dir = _package_dir()
    project_root = package_dir.parents[1]
    candidates = [
        package_dir / "qml",
        project_root / "build" / "qmlcache" / "solin" / "qml",
    ]
    return [str(path) for path in candidates if _qml_module_is_usable(path)]


def _qml_module_is_usable(import_root: Path) -> bool:
    module_dir = import_root / QML_MODULE_URI
    if not (module_dir / "qmldir").is_file():
        return False
    source_dir = _package_dir() / "qml"
    if not tuple(source_dir.glob("*.qml")):
        return _compiled_qml_module_is_complete(module_dir)
    return qml_cache_matches_sources(source_dir, module_dir)


def _compiled_qml_module_is_complete(module_dir: Path) -> bool:
    try:
        manifest = json.loads(
            (module_dir / QML_CACHE_MANIFEST).read_text(encoding="utf-8")
        )
        qmldir_lines = (module_dir / "qmldir").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    sources = manifest.get("sources") if isinstance(manifest, dict) else None
    if not isinstance(sources, dict) or not sources:
        return False
    staged_sources = {
        tokens[2]
        for line in qmldir_lines
        if len(tokens := line.split()) >= 3 and tokens[2].endswith(".qml")
    }
    compiled_names = {f"{name}c" for name in staged_sources}
    return len(compiled_names) == len(sources) and all(
        (module_dir / name).is_file() for name in compiled_names
    )


def qml_cache_matches_sources(source_dir: Path, module_dir: Path) -> bool:
    manifest_path = module_dir / QML_CACHE_MANIFEST
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    sources = manifest.get("sources") if isinstance(manifest, dict) else None
    if not isinstance(sources, dict):
        return False
    qml_files = sorted(source_dir.glob("*.qml"))
    if set(sources) != {path.name for path in qml_files}:
        return False
    for qml_file in qml_files:
        expected = sources.get(qml_file.name)
        if not isinstance(expected, str):
            return False
        try:
            digest = hashlib.sha256(qml_file.read_bytes()).hexdigest()
        except OSError:
            return False
        if digest != expected:
            return False
    return True


def resolve_qml_type(type_name: str) -> QmlTypeLocation:
    import_roots = qml_import_roots()
    if import_roots:
        return QmlTypeLocation(source_url=None, import_root=import_roots[0])
    qml_file = _package_dir() / "qml" / f"{type_name}.qml"
    if qml_file.exists():
        return QmlTypeLocation(
            source_url=QUrl.fromLocalFile(str(qml_file)),
            import_root=None,
        )
    raise RuntimeError(
        f'QML module "{QML_MODULE_URI}" was not found and no dev QML source '
        f"exists for {type_name}."
    )


def load_qml_type(widget, type_name: str) -> None:
    engine = widget.engine()
    location = resolve_qml_type(type_name)
    if location.source_url is not None:
        widget.setSource(location.source_url)
        return

    if location.import_root is not None:
        if location.import_root not in engine.importPathList():
            engine.addImportPath(location.import_root)
        widget.loadFromModule(QML_MODULE_URI, type_name)
        return

    raise AssertionError("Resolved QML location has no source or module")


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

    def __init__(self, widget, type_name: str, *, asynchronous: bool) -> None:
        super().__init__(widget)
        self._widget = widget
        self._type_name = type_name
        self._asynchronous = asynchronous
        self._state = QmlLoadState.PENDING
        self._component: QQmlComponent | None = None
        self._incubator: _LoadIncubator | None = None

    @property
    def state(self) -> QmlLoadState:
        return self._state

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
        if not self._asynchronous:
            try:
                load_qml_type(self._widget, self._type_name)
            except Exception as error:  # noqa: BLE001 - QML loader boundary
                self._fail(str(error))
                return
            self._set_state(QmlLoadState.READY)
            self.completed.emit()
            return
        try:
            self._begin_asynchronous_load()
        except Exception as error:  # noqa: BLE001 - QML loader boundary
            self._fail(str(error))

    def cancel(self) -> None:
        if self.is_terminal:
            return
        if self._incubator is not None:
            self._incubator.clear()
        self._component = None
        self._incubator = None
        self._set_state(QmlLoadState.CANCELLED)

    def _begin_asynchronous_load(self) -> None:
        engine = self._widget.engine()
        location = resolve_qml_type(self._type_name)
        mode = QQmlComponent.CompilationMode.Asynchronous
        if location.source_url is not None:
            component = QQmlComponent(engine, location.source_url, mode, self)
        else:
            if location.import_root is None:
                raise AssertionError("Resolved QML location has no module root")
            if location.import_root not in engine.importPathList():
                engine.addImportPath(location.import_root)
            component = QQmlComponent(
                engine,
                QML_MODULE_URI,
                self._type_name,
                mode,
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
        self._set_state(QmlLoadState.FAILED)
        self.failed.emit(message)

    def _set_state(self, state: QmlLoadState) -> None:
        if self._state is state:
            return
        self._state = state
        self.state_changed.emit(state.value)


__all__ = [
    "QML_CACHE_MANIFEST",
    "QML_MODULE_URI",
    "QmlLoadHandle",
    "QmlLoadState",
    "load_qml_type",
    "qml_cache_matches_sources",
    "qml_import_roots",
    "resolve_qml_type",
]
