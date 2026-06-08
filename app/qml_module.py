from __future__ import annotations

import os

from PySide6.QtCore import QUrl

QML_MODULE_URI = "Solin"


def qml_import_roots() -> list[str]:
    app_dir = os.path.dirname(__file__)
    project_root = os.path.dirname(app_dir)
    candidates = [
        os.path.join(app_dir, "qml"),
        os.path.join(project_root, "build", "qmlcache", "app", "qml"),
    ]
    return [
        path for path in candidates
        if os.path.exists(os.path.join(path, QML_MODULE_URI, "qmldir"))
    ]


def load_qml_type(widget, type_name: str) -> None:
    engine = widget.engine()
    qml_file = os.path.join(os.path.dirname(__file__), "qml", f"{type_name}.qml")
    if os.path.exists(qml_file):
        widget.setSource(QUrl.fromLocalFile(qml_file))
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
