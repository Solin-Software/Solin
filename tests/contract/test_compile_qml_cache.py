from __future__ import annotations

import pytest

from scripts import compile_qml_cache


def test_qml_source_preserves_original_translation_context(tmp_path):
    qml_file = tmp_path / "AdvancedTimerPage.qml"
    qml_file.write_text(
        'import QtQuick\nText { text: qsTr("Clock face") }\n',
        encoding="utf-8",
    )

    staged = compile_qml_cache.qml_source_with_translation_context(qml_file)

    assert staged.startswith('pragma Translator: "AdvancedTimerPage"\n')
    assert 'qsTr("Clock face")' in staged


def test_qml_source_keeps_explicit_translation_context(tmp_path):
    qml_file = tmp_path / "TimerView.qml"
    source = (
        'pragma Translator: "SharedTimer"\n'
        'import QtQuick\n'
        'Text { text: qsTr("Timer") }\n'
    )
    qml_file.write_text(source, encoding="utf-8")

    assert compile_qml_cache.qml_source_with_translation_context(qml_file) == source


def test_find_qt_library_dir_skips_candidates_without_required_frameworks(tmp_path, monkeypatch):
    empty_lib = tmp_path / "PySide6" / "lib"
    framework_lib = tmp_path / "PySide6" / "Qt" / "lib"
    empty_lib.mkdir(parents=True)
    (framework_lib / "QtQuickLayouts.framework").mkdir(parents=True)

    monkeypatch.setattr(
        compile_qml_cache,
        "qt_library_dir_candidates",
        lambda: [empty_lib, framework_lib],
    )

    assert compile_qml_cache.find_qt_library_dir(["QtQuickLayouts.framework"]) == framework_lib


def test_find_qt_library_dir_reports_missing_required_frameworks(tmp_path, monkeypatch):
    empty_lib = tmp_path / "PySide6" / "lib"
    empty_lib.mkdir(parents=True)

    monkeypatch.setattr(
        compile_qml_cache,
        "qt_library_dir_candidates",
        lambda: [empty_lib],
    )

    with pytest.raises(SystemExit, match="PySide6 Qt library directory was not found"):
        compile_qml_cache.find_qt_library_dir(["QtQuickLayouts.framework"])


def test_stage_qt_qml_runtime_copies_selected_modules_without_unused_submodules(tmp_path, monkeypatch):
    source_root = tmp_path / "PySide6" / "qml"
    output_root = tmp_path / "out"
    module_files = {
        "QtQml": ["qmldir", "libqmlplugin.dylib"],
        "QtQml/StateMachine": ["qmldir", "libqtqmlstatemachineplugin.dylib"],
        "QtQml/XmlListModel": ["qmldir", "libqmlxmllistmodelplugin.dylib"],
        "QtQuick": ["qmldir", "libqtquick2plugin.dylib"],
        "QtQuick/Controls": ["qmldir", "libqtquickcontrols2plugin.dylib"],
        "QtQuick/Controls/Basic": ["qmldir", "libqtquickcontrols2basicstyleplugin.dylib"],
        "QtQuick/Controls/Material": ["qmldir", "libqtquickcontrols2materialstyleplugin.dylib"],
        "QtQuick/Effects": ["qmldir", "libeffectsplugin.dylib"],
        "QtQuick/Layouts": ["qmldir", "libqquicklayoutsplugin.dylib"],
        "QtQuick/Templates": ["qmldir", "libqtquicktemplates2plugin.dylib"],
    }
    for module_name, filenames in module_files.items():
        module_dir = source_root.joinpath(*module_name.split("/"))
        module_dir.mkdir(parents=True)
        for filename in filenames:
            (module_dir / filename).write_text(filename, encoding="utf-8")

    monkeypatch.setattr(compile_qml_cache, "find_qt_qml_runtime_dir", lambda: source_root)

    staged = compile_qml_cache.stage_qt_qml_runtime(
        output_root,
        compile_qml_cache.DEFAULT_QT_QML_MODULES,
    )

    assert {path.relative_to(output_root).as_posix() for path in staged} == set(
        compile_qml_cache.DEFAULT_QT_QML_MODULES
    )
    assert (output_root / "QtQml" / "libqmlplugin.dylib").exists()
    assert (output_root / "QtQuick" / "libqtquick2plugin.dylib").exists()
    assert (output_root / "QtQuick" / "Controls" / "libqtquickcontrols2plugin.dylib").exists()
    assert (
        output_root
        / "QtQuick"
        / "Controls"
        / "Basic"
        / "libqtquickcontrols2basicstyleplugin.dylib"
    ).exists()
    assert (output_root / "QtQuick" / "Effects" / "libeffectsplugin.dylib").exists()
    assert (output_root / "QtQuick" / "Layouts" / "libqquicklayoutsplugin.dylib").exists()
    assert (output_root / "QtQuick" / "Templates" / "libqtquicktemplates2plugin.dylib").exists()
    # QtMultimedia is deliberately NOT staged: nothing in Solin imports it any more.
    assert not (output_root / "QtMultimedia").exists()
    assert not (output_root / "QtQml" / "StateMachine").exists()
    assert not (output_root / "QtQml" / "XmlListModel").exists()
    assert not (output_root / "QtQuick" / "Controls" / "Material").exists()


def test_stage_macos_qt_quick_frameworks_stages_flat_runtime_libraries(tmp_path, monkeypatch):
    source_root = tmp_path / "PySide6" / "Qt" / "lib"
    output_root = tmp_path / "out"
    required = compile_qml_cache.MACOS_QT_RUNTIME_FRAMEWORKS
    for name in required + [
        "NotQt.framework",
        "QtPdf.framework",
        "QtQuickControls2Material.framework",
        "QtQuickControls2Universal.framework",
    ]:
        framework = source_root / name
        binary = framework / "Versions" / "A" / name.removesuffix(".framework")
        binary.parent.mkdir(parents=True)
        binary.write_text(name, encoding="utf-8")

    monkeypatch.setattr(compile_qml_cache, "find_qt_library_dir", lambda _patterns: source_root)

    staged = compile_qml_cache.stage_macos_qt_quick_frameworks(output_root)

    staged_names = {path.name for path in staged}
    assert {name.removesuffix(".framework") for name in required} == staged_names
    assert (output_root / "QtQml").read_text(encoding="utf-8") == "QtQml.framework"
    assert (output_root / "QtOpenGL").read_text(encoding="utf-8") == "QtOpenGL.framework"
    assert (output_root / "QtQmlMeta").read_text(encoding="utf-8") == "QtQmlMeta.framework"
    assert not (output_root / "NotQt.framework").exists()
    assert not (output_root / "QtPdf.framework").exists()
    assert not (output_root / "QtQuickControls2Material").exists()
    assert not (output_root / "QtQuickControls2Universal").exists()
    assert not any(output_root.glob("*.framework"))


def test_stage_macos_qt_quick_frameworks_reports_missing_required_dependency(tmp_path, monkeypatch):
    source_root = tmp_path / "PySide6" / "Qt" / "lib"
    (source_root / "QtCore.framework").mkdir(parents=True)

    monkeypatch.setattr(compile_qml_cache, "find_qt_library_dir", lambda _patterns: source_root)

    with pytest.raises(SystemExit, match="Required macOS Qt framework"):
        compile_qml_cache.stage_macos_qt_quick_frameworks(tmp_path / "out")


def test_stage_windows_qt_quick_libraries_includes_required_runtime(
    tmp_path,
    monkeypatch,
):
    source_root = tmp_path / "PySide6"
    source_root.mkdir()
    for name in (
        "Qt6Multimedia.dll",
        "Qt6MultimediaQuick.dll",
        "Qt6QuickEffects.dll",
        "Qt6QuickLayouts.dll",
    ):
        (source_root / name).write_text(name, encoding="utf-8")
    monkeypatch.setattr(compile_qml_cache, "find_pyside6_dir", lambda: source_root)

    staged = compile_qml_cache.stage_windows_qt_quick_libraries(tmp_path / "out")

    # The Multimedia DLLs exist in the source dir but must NOT be staged:
    # dropping QtMultimedia is the point, so this asserts they are filtered out.
    assert {path.name for path in staged} == {
        "Qt6QuickEffects.dll",
        "Qt6QuickLayouts.dll",
    }


def test_stage_linux_qt_quick_libraries_copies_only_transitive_qt_dependencies(
    tmp_path,
    monkeypatch,
):
    source_root = tmp_path / "PySide6" / "Qt" / "lib"
    qml_root = tmp_path / "qml"
    output_root = tmp_path / "out"
    source_root.mkdir(parents=True)
    qml_root.mkdir()

    plugin = qml_root / "libqtquickcontrols2plugin.so"
    controls = source_root / "libQt6QuickControls2Impl.so.6"
    templates = source_root / "libQt6QuickTemplates2.so.6"
    unused = source_root / "libQt6Pdf.so.6"
    for binary in (plugin, controls, templates, unused):
        binary.write_text(binary.name, encoding="utf-8")

    dependencies = {
        plugin.resolve(): ["libQt6QuickControls2Impl.so.6", "libc.so.6"],
        controls.resolve(): ["libQt6QuickTemplates2.so.6", "libQt6Core.so.6"],
        templates.resolve(): [],
    }
    (source_root / "libQt6Core.so.6").write_text("core", encoding="utf-8")
    dependencies[(source_root / "libQt6Core.so.6").resolve()] = []

    monkeypatch.setattr(
        compile_qml_cache,
        "find_qt_library_dir",
        lambda _patterns: source_root,
    )
    monkeypatch.setattr(
        compile_qml_cache,
        "linux_shared_library_dependencies",
        lambda binary: dependencies[binary.resolve()],
    )

    staged = compile_qml_cache.stage_linux_qt_quick_libraries(output_root, qml_root)

    assert {path.name for path in staged} == {
        "libQt6Core.so.6",
        "libQt6QuickControls2Impl.so.6",
        "libQt6QuickTemplates2.so.6",
    }
    assert not (output_root / "libQt6Pdf.so.6").exists()
