from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def find_qmlcachegen() -> str:
    tool = shutil.which("pyside6-qmlcachegen")
    if tool:
        return tool

    suffix = ".exe" if sys.platform.startswith("win") else ""
    sibling = Path(sys.executable).with_name(f"pyside6-qmlcachegen{suffix}")
    if sibling.exists():
        return str(sibling)

    raise SystemExit(
        "pyside6-qmlcachegen was not found. Install PySide6 in the active Python environment."
    )


QML_MODULE_URI = "Solin"
QML_CACHE_MANIFEST = "qml-manifest.json"
DEFAULT_QT_QML_MODULES = [
    "QtQml",
    "QtQuick",
    "QtQuick/Controls",
    "QtQuick/Controls/Basic",
    "QtQuick/Effects",
    "QtQuick/Layouts",
    "QtQuick/Templates",
    "QtMultimedia",
]
MACOS_QT_RUNTIME_FRAMEWORKS = [
    "QtCore.framework",
    "QtDBus.framework",
    "QtGui.framework",
    "QtNetwork.framework",
    "QtMultimedia.framework",
    "QtMultimediaQuick.framework",
    "QtOpenGL.framework",
    "QtQml.framework",
    "QtQmlMeta.framework",
    "QtQmlModels.framework",
    "QtQmlWorkerScript.framework",
    "QtQuick.framework",
    "QtQuickControls2.framework",
    "QtQuickControls2Basic.framework",
    "QtQuickControls2BasicStyleImpl.framework",
    "QtQuickControls2Impl.framework",
    "QtQuickEffects.framework",
    "QtQuickLayouts.framework",
    "QtQuickTemplates2.framework",
    "QtShaderTools.framework",
]


def obfuscated_qml_name(qml_file: Path) -> str:
    digest = hashlib.sha256(qml_file.name.encode("utf-8")).hexdigest()[:10]
    return f"q_{digest}.qml"


_TRANSLATOR_PRAGMA_RE = re.compile(r"(?m)^\s*pragma\s+Translator\s*:")


def qml_source_with_translation_context(qml_file: Path) -> str:
    source = qml_file.read_text(encoding="utf-8")
    if _TRANSLATOR_PRAGMA_RE.search(source):
        return source

    context = json.dumps(qml_file.stem, ensure_ascii=False)
    return f"pragma Translator: {context}\n{source}"


def compile_qml_cache(source_dir: Path, output_dir: Path) -> list[Path]:
    if not source_dir.is_dir():
        raise SystemExit(f"QML source directory does not exist: {source_dir}")

    qml_files = sorted(source_dir.glob("*.qml"))
    if not qml_files:
        raise SystemExit(f"No .qml files found in: {source_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)

    for old_file in output_dir.iterdir():
        if old_file.is_file() and old_file.suffix in {".qml", ".qmlc"}:
            old_file.unlink()
        elif old_file.is_file() and old_file.name.endswith(".qmlc.aotstats"):
            old_file.unlink()

    qml_map = {}
    for qml_file in qml_files:
        staged_name = obfuscated_qml_name(qml_file)
        qml_map[qml_file.stem] = staged_name
        (output_dir / staged_name).write_text(
            qml_source_with_translation_context(qml_file),
            encoding="utf-8",
        )

    qmldir = output_dir / "qmldir"
    qmldir.write_text(
        f"module {QML_MODULE_URI}\n"
        + "\n".join(f"{type_name} 1.0 {staged_name}" for type_name, staged_name in qml_map.items())
        + "\n",
        encoding="utf-8",
    )
    (output_dir / QML_CACHE_MANIFEST).write_text(
        json.dumps(
            {
                "schema": 1,
                "sources": {
                    qml_file.name: hashlib.sha256(qml_file.read_bytes()).hexdigest()
                    for qml_file in qml_files
                },
            },
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    qmlcachegen = find_qmlcachegen()
    for staged_qml in sorted(output_dir.glob("*.qml")):
        subprocess.run(
            [qmlcachegen, "--only-bytecode", str(staged_qml)],
            check=True,
            cwd=output_dir,
        )

    for stats in output_dir.glob("*.qmlc.aotstats"):
        stats.unlink()

    compiled = sorted(output_dir.glob("*.qmlc"))
    missing = []
    for staged_name in qml_map.values():
        compiled_name = f"{staged_name}c"
        if not (output_dir / compiled_name).exists():
            missing.append(compiled_name)
    if missing:
        raise SystemExit("Missing compiled QML cache files: " + ", ".join(missing))

    return compiled


def find_pyside6_dir() -> Path:
    try:
        import PySide6
    except ImportError as exc:
        raise SystemExit("PySide6 is not installed in the active Python environment.") from exc

    return Path(PySide6.__file__).resolve().parent


def find_qt_qml_runtime_dir() -> Path:
    pyside6_dir = find_pyside6_dir()
    candidates = [
        pyside6_dir / "qml",
        pyside6_dir / "Qt" / "qml",
    ]

    try:
        from PySide6.QtCore import QLibraryInfo

        qml_imports_path = QLibraryInfo.path(QLibraryInfo.LibraryPath.QmlImportsPath)
        prefix_path = QLibraryInfo.path(QLibraryInfo.LibraryPath.PrefixPath)
        if qml_imports_path:
            candidates.append(Path(qml_imports_path))
        if prefix_path:
            candidates.append(Path(prefix_path) / "qml")
    except Exception:  # noqa: BLE001 - Qt runtime metadata boundary
        log.debug("Could not read QLibraryInfo QML import paths", exc_info=True)

    seen = set()
    checked = []
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        checked.append(str(candidate))
        if (
            candidate.is_dir()
            and (candidate / "QtQml" / "qmldir").is_file()
            and (candidate / "QtQuick" / "qmldir").is_file()
        ):
            return candidate

    raise SystemExit(
        "PySide6 QML runtime directory was not found. Checked: "
        + "; ".join(checked)
    )


def qt_library_dir_candidates() -> list[Path]:
    pyside6_dir = find_pyside6_dir()
    candidates = [
        pyside6_dir / "lib",
        pyside6_dir / "Qt" / "lib",
    ]

    try:
        from PySide6.QtCore import QLibraryInfo

        libraries_path = QLibraryInfo.path(QLibraryInfo.LibraryPath.LibrariesPath)
        prefix_path = QLibraryInfo.path(QLibraryInfo.LibraryPath.PrefixPath)
        if libraries_path:
            candidates.append(Path(libraries_path))
        if prefix_path:
            candidates.append(Path(prefix_path) / "lib")
    except Exception:  # noqa: BLE001 - Qt runtime metadata boundary
        log.debug("Could not read QLibraryInfo library paths", exc_info=True)

    seen = set()
    unique_candidates = []
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        unique_candidates.append(candidate)

    return unique_candidates


def find_qt_library_dir(required_patterns: list[str] | None = None) -> Path:
    checked = []
    for candidate in qt_library_dir_candidates():
        checked.append(str(candidate))
        if candidate.is_dir():
            if required_patterns and not any(any(candidate.glob(pattern)) for pattern in required_patterns):
                continue
            return candidate

    raise SystemExit("PySide6 Qt library directory was not found. Checked: " + "; ".join(checked))


def _sort_qml_modules(module_names: list[str]) -> list[str]:
    return sorted(set(module_names), key=lambda name: (len(name.split("/")), name))


def _copy_qml_module_root(module_source: Path, module_target: Path) -> None:
    if module_target.exists():
        shutil.rmtree(module_target)
    module_target.mkdir(parents=True)

    for entry in module_source.iterdir():
        if entry.is_dir():
            continue
        shutil.copy2(entry, module_target / entry.name)


def stage_qt_qml_runtime(output_dir: Path, module_names: list[str]) -> list[Path]:
    source_root = find_qt_qml_runtime_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    staged = []
    for module_name in _sort_qml_modules(module_names):
        module_source = source_root.joinpath(*module_name.split("/"))
        module_target = output_dir.joinpath(*module_name.split("/"))
        if not module_source.is_dir():
            raise SystemExit(f"Required Qt QML module was not found: {module_source}")
        _copy_qml_module_root(module_source, module_target)
        staged.append(module_target)

    return staged


def _macos_framework_binary(framework: Path) -> Path | None:
    library_name = framework.name.removesuffix(".framework")
    candidates = [
        framework / "Versions" / "A" / library_name,
        framework / library_name,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def stage_macos_qt_quick_frameworks(output_dir: Path) -> list[Path]:
    required_patterns = MACOS_QT_RUNTIME_FRAMEWORKS
    optional_patterns = []
    source_root = find_qt_library_dir(required_patterns)
    output_dir.mkdir(parents=True, exist_ok=True)

    for old_framework in output_dir.glob("*.framework"):
        shutil.rmtree(old_framework)
    for old_library in output_dir.glob("Qt*"):
        if old_library.is_file() or old_library.is_symlink():
            old_library.unlink()

    staged = []
    missing_required = []
    staged_names = set()
    for pattern in required_patterns + optional_patterns:
        matches = sorted(source_root.glob(pattern))
        if not matches and pattern in required_patterns:
            missing_required.append(pattern)
        for framework in matches:
            if framework.name in staged_names:
                continue
            staged_names.add(framework.name)
            framework_binary = _macos_framework_binary(framework)
            if framework_binary is None:
                if pattern in required_patterns:
                    missing_required.append(f"{pattern}/Versions/A/{framework.stem}")
                continue
            target = output_dir / framework.stem
            shutil.copy2(framework_binary, target)
            staged.append(target)

    if missing_required:
        raise SystemExit(
            "Required macOS Qt framework(s) were not found in "
            f"{source_root}: " + ", ".join(missing_required)
        )

    if sys.platform == "darwin" and not staged:
        raise SystemExit("No Qt Quick runtime libraries were found in the PySide6 Qt library directory.")

    return staged


def stage_windows_qt_quick_libraries(output_dir: Path) -> list[Path]:
    source_root = find_pyside6_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    for old_dll in output_dir.glob("*.dll"):
        old_dll.unlink()

    patterns = [
        "opengl32sw.dll",
        "Qt6Multimedia.dll",
        "Qt6MultimediaQuick.dll",
        "Qt6QuickControls2*.dll",
        "Qt6QuickEffects.dll",
        "Qt6QuickLayouts.dll",
        "Qt6QuickTemplates2.dll",
    ]

    staged = []
    for pattern in patterns:
        for dll in sorted(source_root.glob(pattern)):
            target = output_dir / dll.name
            shutil.copy2(dll, target)
            staged.append(target)

    staged_names = {path.name for path in staged}
    if "opengl32sw.dll" not in staged_names:
        raise SystemExit(
            "Required Qt software OpenGL fallback was not found in the PySide6 directory."
        )
    if sys.platform.startswith("win") and len(staged) == 1:
        raise SystemExit("No Qt Quick runtime DLLs were found in the PySide6 directory.")

    return staged


def linux_shared_library_dependencies(binary: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["readelf", "-d", str(binary)],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise SystemExit(
            "readelf was not found. Install binutils before staging Linux Qt libraries."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Could not inspect Linux shared library dependencies: {binary}") from exc

    return re.findall(r"Shared library: \[([^]]+)]", result.stdout)


def stage_linux_qt_quick_libraries(output_dir: Path, qml_runtime_dir: Path) -> list[Path]:
    source_root = find_qt_library_dir(["libQt6Core.so*"])
    output_dir.mkdir(parents=True, exist_ok=True)

    for old_library in output_dir.glob("libQt6*.so*"):
        old_library.unlink()

    pending = sorted(qml_runtime_dir.rglob("*.so"))
    inspected: set[Path] = set()
    staged: dict[str, Path] = {}

    while pending:
        binary = pending.pop()
        binary = binary.resolve()
        if binary in inspected:
            continue
        inspected.add(binary)

        for dependency in linux_shared_library_dependencies(binary):
            if not dependency.startswith("libQt6") or ".so" not in dependency:
                continue
            if dependency in staged:
                continue

            source = source_root / dependency
            if not source.is_file():
                raise SystemExit(
                    f"Required Linux Qt runtime library was not found: {source} "
                    f"(needed by {binary})"
                )

            target = output_dir / dependency
            shutil.copy2(source, target)
            staged[dependency] = target
            pending.append(source)

    if sys.platform.startswith("linux") and not staged:
        raise SystemExit(
            f"No Linux Qt runtime libraries were required by QML plugins in {qml_runtime_dir}."
        )

    return [staged[name] for name in sorted(staged)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate PySide6 QML bytecode cache files.")
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--qt-qml-output-dir", type=Path)
    parser.add_argument("--qt-library-output-dir", type=Path)
    parser.add_argument("--qt-framework-output-dir", type=Path)
    parser.add_argument(
        "--qt-qml-module",
        action="append",
        default=DEFAULT_QT_QML_MODULES,
        help="Qt QML runtime module to stage. Can be passed more than once.",
    )
    args = parser.parse_args()

    compiled = compile_qml_cache(args.source_dir.resolve(), args.output_dir.resolve())
    print(f"Generated {len(compiled)} compiled QML cache file(s) in {args.output_dir}")
    for path in compiled:
        print(f"  {path.name}")

    if args.qt_qml_output_dir:
        staged = stage_qt_qml_runtime(
            args.qt_qml_output_dir.resolve(),
            args.qt_qml_module,
        )
        print(f"Staged {len(staged)} Qt QML runtime module(s) in {args.qt_qml_output_dir}")
        for path in staged:
            print(f"  {path.relative_to(args.qt_qml_output_dir.resolve())}")

    if args.qt_library_output_dir:
        if sys.platform.startswith("linux"):
            if not args.qt_qml_output_dir:
                raise SystemExit(
                    "--qt-qml-output-dir is required with --qt-library-output-dir on Linux."
                )
            staged = stage_linux_qt_quick_libraries(
                args.qt_library_output_dir.resolve(),
                args.qt_qml_output_dir.resolve(),
            )
            artifact_kind = "Linux Qt runtime library file(s)"
        else:
            staged = stage_windows_qt_quick_libraries(args.qt_library_output_dir.resolve())
            artifact_kind = "Qt Quick runtime DLL(s)"
        print(f"Staged {len(staged)} {artifact_kind} in {args.qt_library_output_dir}")
        for path in staged:
            print(f"  {path.name}")

    if args.qt_framework_output_dir:
        staged = stage_macos_qt_quick_frameworks(args.qt_framework_output_dir.resolve())
        print(f"Staged {len(staged)} Qt runtime library file(s) in {args.qt_framework_output_dir}")
        for path in staged:
            print(f"  {path.name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
