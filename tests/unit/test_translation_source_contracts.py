from __future__ import annotations

import ast
from pathlib import Path
import re


_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "solin"
_TRANSLATION_CALLS = {"tr", "translate"}
_TRANSLATION_MARKERS = _TRANSLATION_CALLS | {"QT_TRANSLATE_NOOP"}
_ANONYMOUS_FORMAT_FIELD = re.compile(r"\{(?:|\d+)\}")


def _function_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return ""


def _literal_source(node: ast.Call, function_name: str) -> str | None:
    source_index = 1 if function_name in {"translate", "QT_TRANSLATE_NOOP"} else 0
    if len(node.args) <= source_index:
        return None
    source = node.args[source_index]
    if isinstance(source, ast.Constant) and isinstance(source.value, str):
        return source.value
    return None


def test_python_translation_lookups_never_use_formatted_source_strings():
    violations: list[str] = []
    for path in _SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = _function_name(node)
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
            function_name = _function_name(node)
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
            function_name = _function_name(node)
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


def test_python_numerus_sources_use_lupdate_visible_qobject_tr_calls():
    violations: list[str] = []
    for path in _SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = _function_name(node)
            source = _literal_source(node, function_name)
            if source is None or "%n" not in source:
                continue

            is_valid_tr_call = function_name == "tr" and len(node.args) >= 3
            if is_valid_tr_call:
                continue

            relative = path.relative_to(_SOURCE_ROOT.parent.parent)
            violations.append(f"{relative}:{node.lineno}")

    assert violations == [], (
        "PySide6 lupdate only extracts Python numerus metadata from literal "
        "QObject.tr(source, disambiguation, count) calls: "
        + ", ".join(violations)
    )
