from __future__ import annotations

import ast
from pathlib import Path
import re
import xml.etree.ElementTree as ET

import pytest


_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "solin"
_TRANSLATION_CALLS = {"tr", "translate"}
_TRANSLATION_MARKERS = _TRANSLATION_CALLS | {"QT_TRANSLATE_NOOP"}
_ANONYMOUS_FORMAT_FIELD = re.compile(r"\{(?:|\d+)\}")

_SCENE_WORKFLOW_QML = (
    "ScenesCameraDialog.qml",
    "ScenesWorkspace.qml",
    "ScenesReferenceDialog.qml",
    "ScenesRenameDialog.qml",
    "ScenesSourceSelector.qml",
)


@pytest.mark.parametrize(
    "catalog",
    sorted((_SOURCE_ROOT / "resources" / "translations").glob("solin_*.ts")),
    ids=lambda path: path.stem,
)
def test_scene_source_workflow_messages_have_completed_translations(catalog: Path) -> None:
    # These stream-credential labels predate the scene-source workflow and are
    # tracked separately from connection reuse, layer naming, and configuration.
    stream_credential_sources = {
        "Stream user name",
        "Stream password",
        "Remove saved stream credentials",
        "Only if the camera asks for one",
    }
    qml_sources = {
        ast.literal_eval(match.group(1))
        for filename in _SCENE_WORKFLOW_QML
        for match in re.finditer(
            r'qsTr\(\s*("(?:[^"\\]|\\.)*")',
            (_SOURCE_ROOT / "qml" / filename).read_text(encoding="utf-8"),
        )
    } - stream_credential_sources
    bridge_tree = ast.parse(
        (_SOURCE_ROOT / "ui" / "qml" / "scenes_bridge.py").read_text(encoding="utf-8")
    )
    bridge_sources = {
        node.args[0].value
        for node in ast.walk(bridge_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "tr"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    bridge_sources.add("A source with this name already exists in this scene.")
    contexts = {node.findtext("name"): node for node in ET.parse(catalog).findall("context")}
    for context, sources in {
        "ScenesEditorView": qml_sources,
        "ScenesBridge": bridge_sources,
    }.items():
        messages = {
            node.findtext("source"): node.find("translation")
            for node in contexts[context].findall("message")
        }
        for source in sorted(sources):
            translation = messages.get(source)
            assert translation is not None, (catalog.name, context, source)
            assert translation.get("type") not in {"unfinished", "vanished", "obsolete"}, (
                catalog.name,
                context,
                source,
            )
            assert "".join(translation.itertext()).strip(), (catalog.name, context, source)
            assert sorted(re.findall(r"%[1-9]\d*|%n", source)) == sorted(
                re.findall(r"%[1-9]\d*|%n", "".join(translation.itertext()))
            ), (catalog.name, context, source)
    keep_active = next(
        (
            node.find("translation")
            for node in contexts["ScenesEditorView"].findall("message")
            if node.findtext("source") == "Keep camera active"
        ),
        None,
    )
    assert keep_active is None or keep_active.get("type") in {"vanished", "obsolete"}


@pytest.mark.parametrize("catalog", sorted((_SOURCE_ROOT / "resources" / "translations").glob("solin_*.ts")), ids=lambda path: path.stem)
def test_camera_mode_and_health_messages_have_completed_translations(catalog: Path) -> None:
    required = {
        "ScenesBridge": {
            "Camera capabilities are unavailable. You can try automatic mode or refresh the cameras.",
            "Waiting for camera frames…",
            "The camera is sending a different resolution than selected. Choose a supported mode or refresh the cameras.",
            "No frames received from the camera. Check its connection, privacy permissions, or whether another application is using it.",
            "The camera could not apply the selected mode. Choose another resolution, frame rate or format.",
            "The camera capture could not be checked. Refresh the cameras or reconnect the device.",
        },
        "ScenesEditorView": {
            "Unavailable device", "Automatic", "Unavailable", "Resolution", "Frame rate",
            "Custom frame rate", "e.g. 29.97 or 30000/1001", "Advanced", "Show advanced settings",
            "Saved camera mode is unavailable. Refresh the cameras or choose another mode.",
            "This frame rate is not supported at the selected resolution and format.",
            "Custom resolution", "Width", "Height", "Check", "Checking supported camera modes…",
            "This resolution is outside the camera's supported limits or steps.",
            "Supported modes could not be retrieved for this resolution. Refresh the cameras or choose another resolution.",
        },
    }
    contexts = {node.findtext("name"): node for node in ET.parse(catalog).findall("context")}
    for context, sources in required.items():
        messages = {node.findtext("source"): node.find("translation")
                    for node in contexts[context].findall("message")}
        for source in sources:
            translation = messages.get(source)
            assert translation is not None, (catalog.name, context, source)
            assert translation.get("type") not in {"unfinished", "vanished", "obsolete"}
            assert "".join(translation.itertext()).strip(), (catalog.name, context, source)
    assert "%1 (not verified)" not in {
        node.findtext("source") for node in contexts["ScenesBridge"].findall("message")
    }


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
