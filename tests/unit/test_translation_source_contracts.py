from __future__ import annotations

import ast
from pathlib import Path
import re


_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "solin"
_TRANSLATION_CALLS = {"tr", "translate"}
_TRANSLATION_MARKERS = _TRANSLATION_CALLS | {"QT_TRANSLATE_NOOP"}
_ANONYMOUS_FORMAT_FIELD = re.compile(r"\{(?:|\d+)\}")


def test_python_translation_lookups_never_use_formatted_source_strings():
    violations: list[str] = []
    for path in _SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id
                if isinstance(node.func, ast.Name)
                else ""
            )
            if function_name not in _TRANSLATION_CALLS:
                continue
            if any(isinstance(argument, ast.JoinedStr) for argument in node.args):
                relative = path.relative_to(_SOURCE_ROOT.parent.parent)
                violations.append(f"{relative}:{node.lineno}")

    assert violations == [], (
        "Translation source strings must be literals with placeholders; "
        "format only after lookup: " + ", ".join(violations)
    )


def test_python_translation_sources_use_named_format_fields():
    violations: list[str] = []
    for path in _SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id
                if isinstance(node.func, ast.Name)
                else ""
            )
            if function_name not in _TRANSLATION_MARKERS:
                continue
            if any(
                isinstance(argument, ast.Constant)
                and isinstance(argument.value, str)
                and _ANONYMOUS_FORMAT_FIELD.search(argument.value)
                for argument in node.args
            ):
                relative = path.relative_to(_SOURCE_ROOT.parent.parent)
                violations.append(f"{relative}:{node.lineno}")

    assert violations == [], (
        "Translation sources must use named format fields so translators can "
        "reorder them safely: " + ", ".join(violations)
    )


def test_python_neutral_plural_sources_use_qt_numerus():
    violations: list[str] = []
    for path in _SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id
                if isinstance(node.func, ast.Name)
                else ""
            )
            if function_name not in _TRANSLATION_MARKERS:
                continue
            if any(
                isinstance(argument, ast.Constant)
                and isinstance(argument.value, str)
                and "(s)" in argument.value
                and "%n" not in argument.value
                for argument in node.args
            ):
                relative = path.relative_to(_SOURCE_ROOT.parent.parent)
                violations.append(f"{relative}:{node.lineno}")

    assert violations == [], (
        "Neutral '(s)' translation sources must use Qt numerus (%n): "
        + ", ".join(violations)
    )
