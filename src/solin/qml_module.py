from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl

QML_MODULE_URI = "Solin"


def qml_import_roots() -> list[str]:
    package_dir = Path(__file__).resolve().parent
    project_root = package_dir.parents[1]
    candidates = [
        package_dir / "qml",
        project_root / "build" / "qmlcache" / "solin" / "qml",
    ]
    return [
        str(path) for path in candidates
        if (path / QML_MODULE_URI / "qmldir").exists()
    ]


def load_qml_type(widget, type_name: str) -> None:
    engine = widget.engine()
    qml_file = Path(__file__).resolve().parent / "qml" / f"{type_name}.qml"
    if qml_file.exists():
        widget.setSource(QUrl.fromLocalFile(str(qml_file)))
        return

    import_roots = qml_import_roots()
    if import_roots:
        for import_root in reversed(import_roots):
            if import_root not in engine.importPathList():
                engine.addImportPath(import_root)
        widget.loadFromModule(QML_MODULE_URI, type_name)
        return

    raise RuntimeError(
        f'QML module "{QML_MODULE_URI}" was not found and no dev QML source '
        f"exists for {type_name}."
    )
