from __future__ import annotations

import ast
from pathlib import Path

from tests._paths import REPO_ROOT

_REPO_ROOT = REPO_ROOT
_BROAD_EXCEPTION_NOQA = "noqa: BLE001"


def _python_sources() -> list[Path]:
    return [
        _REPO_ROOT / "main.py",
        *sorted((_REPO_ROOT / "src" / "solin").rglob("*.py")),
        *sorted((_REPO_ROOT / "scripts").rglob("*.py")),
        *sorted((_REPO_ROOT / "tools").rglob("*.py")),
    ]


def _catches_broad_exception(exception_type: ast.expr | None) -> bool:
    if exception_type is None:
        return True
    if isinstance(exception_type, ast.Name):
        return exception_type.id in {"BaseException", "Exception"}
    if isinstance(exception_type, ast.Tuple):
        return any(_catches_broad_exception(item) for item in exception_type.elts)
    return False


def _broad_exception_handlers() -> list[tuple[Path, ast.ExceptHandler, str]]:
    handlers: list[tuple[Path, ast.ExceptHandler, str]] = []
    for path in _python_sources():
        source = path.read_text(encoding="utf-8-sig")
        lines = source.splitlines()
        tree = ast.parse(source, filename=str(path))

        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and _catches_broad_exception(node.type):
                handlers.append((path, node, lines[node.lineno - 1]))
    return handlers


def test_broad_exception_handlers_do_not_silently_pass():
    offenders: list[str] = []

    for path, node, _handler_line in _broad_exception_handlers():
        relative_path = path.relative_to(_REPO_ROOT).as_posix()
        if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            offenders.append(f"{relative_path}:{node.lineno}")

    assert offenders == []


def test_broad_exception_handlers_have_local_justification():
    offenders: list[str] = []

    for path, node, handler_line in _broad_exception_handlers():
        justification = handler_line.partition(_BROAD_EXCEPTION_NOQA)[2].strip()
        if (
            _BROAD_EXCEPTION_NOQA not in handler_line
            or not justification.startswith("- ")
            or len(justification) <= 4
        ):
            relative_path = path.relative_to(_REPO_ROOT).as_posix()
            offenders.append(f"{relative_path}:{node.lineno}")

    assert offenders == [], (
        "Broad catches require an inline '# noqa: BLE001 - reason' justification: "
        f"{offenders}"
    )
