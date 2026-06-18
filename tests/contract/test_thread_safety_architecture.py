from __future__ import annotations

import ast

from tests._paths import REPO_ROOT


_GUI_TYPES = {
    "QApplication",
    "QDialog",
    "QGuiApplication",
    "QLabel",
    "QPainter",
    "QPixmap",
    "QWidget",
}
_GUI_METHODS = {
    "activateWindow",
    "hide",
    "raise_",
    "setPixmap",
    "setText",
    "setVisible",
    "show",
}


def _is_thread_constructor(call: ast.Call) -> bool:
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "Thread"
        or isinstance(func, ast.Name)
        and func.id == "Thread"
    )


def _worker_functions(tree: ast.AST) -> set[ast.FunctionDef]:
    parents = {
        child: node
        for node in ast.walk(tree)
        for child in ast.iter_child_nodes(node)
    }
    workers: set[ast.FunctionDef] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            is_worker_class = "Worker" in node.name or "Thread" in node.name
            if is_worker_class:
                workers.update(
                    child
                    for child in node.body
                    if isinstance(child, ast.FunctionDef) and child.name == "run"
                )

        if not isinstance(node, ast.Call) or not _is_thread_constructor(node):
            continue
        target = next(
            (keyword.value for keyword in node.keywords if keyword.arg == "target"),
            None,
        )
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
            if target.value.id != "self":
                continue
            scope = parents.get(node)
            while scope is not None and not isinstance(scope, ast.ClassDef):
                scope = parents.get(scope)
            workers.update(
                child
                for child in getattr(scope, "body", [])
                if isinstance(child, ast.FunctionDef) and child.name == target.attr
            )
        elif isinstance(target, ast.Name):
            scope = parents.get(node)
            while scope is not None and not isinstance(
                scope,
                (ast.FunctionDef, ast.Module),
            ):
                scope = parents.get(scope)
            workers.update(
                child
                for child in getattr(scope, "body", [])
                if isinstance(child, ast.FunctionDef) and child.name == target.id
            )

    return workers


def test_worker_entrypoints_do_not_touch_gui_resources():
    worker_count = 0
    violations: list[str] = []
    for path in (REPO_ROOT / "src" / "solin").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for function in _worker_functions(tree):
            worker_count += 1
            for node in ast.walk(function):
                if isinstance(node, ast.Name) and node.id in _GUI_TYPES:
                    violations.append(f"{path}:{node.lineno}: {node.id}")
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _GUI_METHODS
                ):
                    violations.append(f"{path}:{node.lineno}: {node.func.attr}()")

    assert worker_count >= 20
    assert violations == [], "GUI access from worker entrypoints:\n" + "\n".join(violations)
