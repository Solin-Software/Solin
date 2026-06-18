from __future__ import annotations

import ast
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tests._paths import REPO_ROOT

PROJECT_ROOT = REPO_ROOT


@dataclass(frozen=True)
class Codebase:
    root: Path
    package: str
    modules: frozenset[tuple[str, ...]]

    @classmethod
    def discover(cls, root: Path) -> Codebase | None:
        if not root.is_dir():
            return None

        package = root.name
        modules: set[tuple[str, ...]] = set()
        for path in root.rglob("*.py"):
            relative = path.relative_to(root)
            parts = relative.with_suffix("").parts
            if parts[-1] == "__init__":
                parts = parts[:-1]
            modules.add((package, *parts))
        return cls(root=root, package=package, modules=frozenset(modules))

    def python_files(self, directory: Path | None = None):
        yield from sorted((directory or self.root).rglob("*.py"))

    def module_for(self, path: Path) -> tuple[str, ...]:
        parts = path.relative_to(self.root).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        return (self.package, *parts)


CODEBASES = tuple(
    codebase
    for root in (PROJECT_ROOT / "src" / "solin",)
    if (codebase := Codebase.discover(root)) is not None
)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _display(path: Path, node: ast.AST) -> str:
    relative = path.relative_to(PROJECT_ROOT)
    source = ast.get_source_segment(path.read_text(encoding="utf-8"), node)
    statement = " ".join((source or type(node).__name__).split())
    return f"{relative}:{node.lineno}: {statement}"


def _resolve_from(
    codebase: Codebase,
    importer: tuple[str, ...],
    is_package: bool,
    node: ast.ImportFrom,
) -> tuple[str, ...]:
    if node.level == 0:
        return tuple((node.module or "").split("."))

    package = importer if is_package else importer[:-1]
    keep = len(package) - (node.level - 1)
    prefix = package[: max(keep, 0)]
    return (*prefix, *(node.module or "").split("."))


def _dependency_targets(
    codebase: Codebase,
    path: Path,
    node: ast.Import | ast.ImportFrom,
) -> list[tuple[tuple[str, ...], str | None]]:
    if isinstance(node, ast.Import):
        return [(tuple(alias.name.split(".")), None) for alias in node.names]

    importer = codebase.module_for(path)
    base = _resolve_from(codebase, importer, path.name == "__init__.py", node)
    targets: list[tuple[tuple[str, ...], str | None]] = []
    for alias in node.names:
        candidate = (*base, *alias.name.split("."))
        target = candidate if candidate in codebase.modules else base
        targets.append((target, alias.name))
    return targets


def _imports(path: Path):
    yield from (
        node
        for node in ast.walk(_tree(path))
        if isinstance(node, (ast.Import, ast.ImportFrom))
    )


def test_application_source_layout_is_discoverable():
    assert CODEBASES, (
        "No application source root found. Expected src/solin/ "
        "relative to the repository root."
    )


def test_generated_output_directories_are_not_versioned():
    result = subprocess.run(
        ["git", "ls-files", "build", "dist"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == ""


def test_package_initializers_do_not_reexport_concrete_symbols():
    violations: list[str] = []

    for codebase in CODEBASES:
        for path in codebase.root.rglob("__init__.py"):
            for node in _imports(path):
                if not isinstance(node, ast.ImportFrom):
                    continue
                base = _resolve_from(
                    codebase,
                    codebase.module_for(path),
                    is_package=True,
                    node=node,
                )
                if not base or base[0] != codebase.package or base not in codebase.modules:
                    continue

                concrete_names = [
                    alias.name
                    for alias in node.names
                    if alias.name == "*"
                    or (*base, *alias.name.split(".")) not in codebase.modules
                ]
                if concrete_names:
                    violations.append(
                        f"{_display(path, node)} [concrete: {', '.join(concrete_names)}]"
                    )

    assert not violations, (
        "Package __init__.py files must not import or re-export concrete application "
        "symbols. Import from the defining module instead:\n" + "\n".join(violations)
    )


def test_top_level_packages_do_not_import_each_others_private_members():
    violations: list[str] = []

    for codebase in CODEBASES:
        for path in codebase.python_files():
            relative_parts = path.relative_to(codebase.root).parts
            source_package = relative_parts[0] if len(relative_parts) > 1 else None
            if source_package is None:
                continue

            for node in _imports(path):
                for target, imported_name in _dependency_targets(codebase, path, node):
                    if len(target) < 2 or target[0] != codebase.package:
                        continue
                    target_package = target[1]
                    if target_package == source_package:
                        continue
                    private_module = any(part.startswith("_") for part in target[2:])
                    private_member = bool(
                        imported_name
                        and any(
                            part.startswith("_")
                            for part in imported_name.split(".")
                        )
                    )
                    if private_module or private_member:
                        violations.append(_display(path, node))

    assert not violations, (
        "Top-level application packages must not import private modules or members "
        "from one another. Promote a public API or move the shared abstraction:\n"
        + "\n".join(sorted(set(violations)))
    )


def test_clean_architecture_layer_dependencies():
    violations: list[str] = []
    rules = {
        "domain": frozenset({"PySide6", "infrastructure", "presentation"}),
        "application": frozenset({"PySide6", "presentation"}),
    }

    for codebase in CODEBASES:
        for layer, forbidden in rules.items():
            layer_root = codebase.root / layer
            if not layer_root.is_dir():
                continue

            for path in codebase.python_files(layer_root):
                for node in _imports(path):
                    for target, _ in _dependency_targets(codebase, path, node):
                        dependency = (
                            target[1]
                            if len(target) > 1 and target[0] == codebase.package
                            else target[0] if target else ""
                        )
                        if dependency in forbidden:
                            violations.append(
                                f"{_display(path, node)} "
                                f"[{layer} -> {dependency}]"
                            )

    assert not violations, (
        "Clean Architecture dependency violations detected. Domain must remain "
        "independent of PySide6, infrastructure, and presentation; application "
        "must remain independent of PySide6 and presentation:\n"
        + "\n".join(sorted(set(violations)))
    )


def test_core_translation_rendering_stays_in_i18n_or_rendering_adapters():
    core_root = PROJECT_ROOT / "src" / "solin" / "core"
    allowed_prefixes = {
        (PROJECT_ROOT / "src" / "solin" / "core" / "i18n").resolve(),
    }
    allowed_files = {
        (core_root / "rendering" / "timer_report_pdf.py").resolve(),
    }
    violations: list[str] = []

    for path in sorted(core_root.rglob("*.py")):
        resolved = path.resolve()
        if resolved in allowed_files or any(
            resolved.is_relative_to(prefix) for prefix in allowed_prefixes
        ):
            continue

        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if not isinstance(function, ast.Attribute):
                continue
            is_qt_translate = (
                function.attr == "translate"
                and isinstance(function.value, ast.Name)
                and function.value.id in {"QCoreApplication", "QApplication"}
            )
            is_self_tr = (
                function.attr == "tr"
                and isinstance(function.value, ast.Name)
                and function.value.id == "self"
            )
            if is_qt_translate or is_self_tr:
                violations.append(_display(path, node))

    assert not violations, (
        "Core services must emit source data/statuses and let presentation/i18n "
        "adapters render translations:\n" + "\n".join(violations)
    )


def test_playlist_widgets_use_watched_folder_playlist_store_boundary():
    playlist_widget_root = PROJECT_ROOT / "src" / "solin" / "widgets" / "playlist"
    forbidden_module = (
        "solin",
        "core",
        "ingest",
        "watched_folder",
    )
    raw_helpers = {
        "WatchedFolderSyncThread",
        "get_pending_files",
        "load_manifest_playlist",
        "local_file_availability_signature",
        "remove_item_from_manifest",
        "save_manifest_playlist",
        "scan_root",
    }
    violations: list[str] = []

    for path in sorted(playlist_widget_root.rglob("*.py")):
        for node in _imports(path):
            if not isinstance(node, ast.ImportFrom):
                continue
            base = _resolve_from(
                CODEBASES[0],
                CODEBASES[0].module_for(path),
                path.name == "__init__.py",
                node,
            )
            if base != forbidden_module:
                continue
            imported = {alias.name for alias in node.names}
            blocked = sorted(raw_helpers.intersection(imported))
            if blocked:
                violations.append(f"{_display(path, node)} [{', '.join(blocked)}]")

    assert violations == [], (
        "Playlist widgets must use WatchedFolderPlaylistStore instead of raw "
        "watched-folder manifest/sync helpers:\n" + "\n".join(violations)
    )


def test_meeting_widgets_use_watched_folder_file_store_boundary():
    meeting_widget_root = PROJECT_ROOT / "src" / "solin" / "widgets" / "meetings"
    forbidden_module = (
        "solin",
        "core",
        "ingest",
        "watched_folder",
    )
    raw_helpers = {
        "local_file_availability_signature",
        "meeting_folder_source_needs_processing",
        "scan_meeting_folder_sources",
    }
    violations: list[str] = []

    for path in sorted(meeting_widget_root.rglob("*.py")):
        for node in _imports(path):
            if not isinstance(node, ast.ImportFrom):
                continue
            base = _resolve_from(
                CODEBASES[0],
                CODEBASES[0].module_for(path),
                path.name == "__init__.py",
                node,
            )
            if base != forbidden_module:
                continue
            imported = {alias.name for alias in node.names}
            blocked = sorted(raw_helpers.intersection(imported))
            if blocked:
                violations.append(f"{_display(path, node)} [{', '.join(blocked)}]")

    assert violations == [], (
        "Meeting widgets/controllers must use WatchedFolderFileStore instead of "
        "raw watched-folder filesystem helpers:\n" + "\n".join(violations)
    )


def test_ui_workflows_do_not_construct_jwpub_import_threads_directly():
    workflow_roots = (
        PROJECT_ROOT / "src" / "solin" / "controllers",
        PROJECT_ROOT / "src" / "solin" / "widgets",
    )
    violations: list[str] = []

    for workflow_root in workflow_roots:
        for path in sorted(workflow_root.rglob("*.py")):
            for node in _imports(path):
                if not isinstance(node, ast.ImportFrom):
                    continue
                imported = {alias.name for alias in node.names}
                if "JwpubImportThread" in imported:
                    violations.append(_display(path, node))

    assert violations == [], (
        "UI workflows must receive JWPUB import worker factories from composition "
        "instead of constructing concrete threads:\n" + "\n".join(violations)
    )


def test_widgets_do_not_import_clip_http_fetcher():
    widget_root = PROJECT_ROOT / "src" / "solin" / "widgets"
    violations: list[str] = []

    for path in sorted(widget_root.rglob("*.py")):
        for node in _imports(path):
            if not isinstance(node, ast.ImportFrom):
                continue
            if any(alias.name == "fetch_clips" for alias in node.names):
                violations.append(_display(path, node))

    assert violations == [], (
        "Widgets must receive clip-loading workers from composition instead of "
        "importing the HTTP fetcher:\n" + "\n".join(violations)
    )


def test_concrete_http_clients_stay_in_network_adapter():
    source_root = PROJECT_ROOT / "src" / "solin"
    allowed = {
        (source_root / "core" / "network" / "http.py").resolve(),
    }
    fragments = ("requests.get(", "urlopen(", "curl_cffi")
    violations: list[str] = []

    for path in sorted(source_root.rglob("*.py")):
        if path.resolve() in allowed:
            continue
        source = path.read_text(encoding="utf-8")
        for fragment in fragments:
            if fragment in source:
                violations.append(
                    f"{path.relative_to(PROJECT_ROOT)} contains {fragment}"
                )

    assert violations == [], (
        "Concrete HTTP clients must stay behind solin.core.network.http adapters:\n"
        + "\n".join(violations)
    )


def test_widgets_do_not_import_cache_scanner():
    widget_root = PROJECT_ROOT / "src" / "solin" / "widgets"
    violations: list[str] = []

    for path in sorted(widget_root.rglob("*.py")):
        for node in _imports(path):
            if not isinstance(node, ast.ImportFrom):
                continue
            if any(alias.name == "scan_cached_media_items" for alias in node.names):
                violations.append(_display(path, node))

    assert violations == [], (
        "Widgets must receive cache scan sessions from composition instead of "
        "calling filesystem scanners directly:\n" + "\n".join(violations)
    )


def test_ui_workflows_do_not_import_document_conversion_adapters():
    workflow_roots = (
        PROJECT_ROOT / "src" / "solin" / "controllers",
        PROJECT_ROOT / "src" / "solin" / "widgets",
    )
    forbidden_modules = {"core.rendering.pdf", "core.rendering.libreoffice"}
    violations: list[str] = []

    for workflow_root in workflow_roots:
        for path in sorted(workflow_root.rglob("*.py")):
            for node in _imports(path):
                if not isinstance(node, ast.ImportFrom):
                    continue
                module = node.module or ""
                if any(module.endswith(blocked) for blocked in forbidden_modules):
                    violations.append(_display(path, node))

    assert violations == [], (
        "UI workflows must receive document conversion services from composition "
        "instead of importing cache and worker adapters directly:\n"
        + "\n".join(violations)
    )


def test_remote_services_controller_receives_concrete_adapters_from_composition():
    path = PROJECT_ROOT / "src" / "solin" / "controllers" / "remote_services_controller.py"
    violations: list[str] = []

    for node in _imports(path):
        for target, _ in _dependency_targets(CODEBASES[0], path, node):
            if len(target) < 2 or target[0] != "solin":
                continue
            imports_widget = target[1] == "widgets"
            imports_ui_dialog = len(target) > 2 and target[1:3] == ("ui", "dialogs")
            imports_remote_service = len(target) > 2 and target[1:3] == ("core", "remote")
            if imports_widget or imports_ui_dialog or imports_remote_service:
                violations.append(_display(path, node))

    assert violations == [], (
        "RemoteServicesController must receive remote services, notification "
        "queues, and update dialogs from composition instead of importing "
        "concrete adapters:\n" + "\n".join(violations)
    )


def test_qml_presentation_adapters_live_under_ui_qml():
    legacy_paths = (
        PROJECT_ROOT / "src" / "solin" / "qml_module.py",
        PROJECT_ROOT / "src" / "solin" / "quick_toolbar_bridge.py",
        PROJECT_ROOT / "src" / "solin" / "widgets" / "jw_media_catalog_bridge.py",
        PROJECT_ROOT / "src" / "solin" / "widgets" / "jw_songs_bridge.py",
        PROJECT_ROOT / "src" / "solin" / "widgets" / "playlist" / "edit_bridge.py",
        PROJECT_ROOT / "src" / "solin" / "widgets" / "playlist" / "edit_model.py",
        PROJECT_ROOT / "src" / "solin" / "widgets" / "playlist" / "edit_visuals.py",
        PROJECT_ROOT / "src" / "solin" / "widgets" / "timer_bridge.py",
    )
    expected_paths = (
        PROJECT_ROOT / "src" / "solin" / "ui" / "qml" / "loader.py",
        PROJECT_ROOT / "src" / "solin" / "ui" / "qml" / "jw_media_catalog.py",
        PROJECT_ROOT / "src" / "solin" / "ui" / "qml" / "jw_songs.py",
        PROJECT_ROOT / "src" / "solin" / "ui" / "qml" / "media_library.py",
        PROJECT_ROOT / "src" / "solin" / "ui" / "qml" / "meeting_detail.py",
        PROJECT_ROOT / "src" / "solin" / "ui" / "qml" / "playlist" / "bridge.py",
        PROJECT_ROOT / "src" / "solin" / "ui" / "qml" / "playlist" / "model.py",
        PROJECT_ROOT / "src" / "solin" / "ui" / "qml" / "playlist" / "visuals.py",
        PROJECT_ROOT / "src" / "solin" / "ui" / "qml" / "quick_toolbar.py",
        PROJECT_ROOT / "src" / "solin" / "ui" / "qml" / "timer_bridge.py",
        PROJECT_ROOT / "src" / "solin" / "ui" / "qml" / "timer_icons.py",
        PROJECT_ROOT / "src" / "solin" / "ui" / "qml" / "timer_output.py",
    )
    violations: list[str] = []

    for path in legacy_paths:
        if path.exists():
            violations.append(f"{path.relative_to(PROJECT_ROOT)} still exists")

    media_library_widget = (
        PROJECT_ROOT / "src" / "solin" / "widgets" / "media_library_widget.py"
    )
    widget_source = media_library_widget.read_text(encoding="utf-8")
    for fragment in (
        "class MediaLibraryBridge",
        "class MediaLibraryIconProvider",
        "class MediaLibraryModel",
    ):
        if fragment in widget_source:
            violations.append(
                f"{media_library_widget.relative_to(PROJECT_ROOT)} contains {fragment}"
            )

    timer_window = PROJECT_ROOT / "src" / "solin" / "projection" / "timer_window.py"
    if "class ClockRenderBridge" in timer_window.read_text(encoding="utf-8"):
        violations.append(
            f"{timer_window.relative_to(PROJECT_ROOT)} contains class ClockRenderBridge"
        )

    meetings_widget = (
        PROJECT_ROOT / "src" / "solin" / "widgets" / "meetings" / "widget.py"
    )
    meetings_source = meetings_widget.read_text(encoding="utf-8")
    for fragment in (
        "QQuickWidget",
        "addImageProvider",
        "setContextProperty",
        "load_qml_type",
        "rootObject",
    ):
        if fragment in meetings_source:
            violations.append(
                f"{meetings_widget.relative_to(PROJECT_ROOT)} contains {fragment}"
            )

    forbidden_modules = {
        ("solin", "qml_module"),
        ("solin", "quick_toolbar_bridge"),
        ("solin", "widgets", "jw_media_catalog_bridge"),
        ("solin", "widgets", "jw_songs_bridge"),
        ("solin", "widgets", "playlist", "edit_bridge"),
        ("solin", "widgets", "playlist", "edit_model"),
        ("solin", "widgets", "playlist", "edit_visuals"),
        ("solin", "widgets", "timer_bridge"),
    }
    for codebase in CODEBASES:
        for path in codebase.python_files():
            for node in _imports(path):
                for target, _ in _dependency_targets(codebase, path, node):
                    if target in forbidden_modules:
                        violations.append(_display(path, node))

    missing = [
        str(path.relative_to(PROJECT_ROOT))
        for path in expected_paths
        if not path.exists()
    ]
    assert not missing, "Missing UI QML adapter modules:\n" + "\n".join(missing)
    assert violations == [], (
        "QML presentation adapters must live under solin.ui.qml instead of the "
        "package root:\n" + "\n".join(violations)
    )


def test_widgets_do_not_own_jw_catalog_thumbnail_download_workers():
    widget_root = PROJECT_ROOT / "src" / "solin" / "widgets"
    forbidden_names = {
        "ensure_thumbnail_cached",
        "QRunnable",
        "QThreadPool",
    }
    violations: list[str] = []

    for path in sorted(widget_root.rglob("*.py")):
        for node in _imports(path):
            imported = {alias.name for alias in node.names}
            blocked = sorted(imported & forbidden_names)
            if blocked:
                violations.append(f"{_display(path, node)} [{', '.join(blocked)}]")

    assert violations == [], (
        "Widgets must delegate JW catalog thumbnail HTTP/cache workers to the "
        "injected session:\n" + "\n".join(violations)
    )


def test_wifi_widget_does_not_own_qr_generation_workers():
    path = PROJECT_ROOT / "src" / "solin" / "widgets" / "wifi_receive_widget.py"
    forbidden_imports = {"QThread", "qrcode"}
    violations: list[str] = []

    for node in _imports(path):
        imported = (
            {alias.name for alias in node.names}
            if isinstance(node, ast.Import)
            else {alias.name for alias in node.names}
        )
        blocked = sorted(imported & forbidden_imports)
        if blocked:
            violations.append(f"{_display(path, node)} [{', '.join(blocked)}]")

    source = path.read_text(encoding="utf-8")
    for fragment in ("_QrWorker", "moveToThread("):
        if fragment in source:
            violations.append(f"{path.relative_to(PROJECT_ROOT)}: {fragment}")

    assert violations == [], (
        "Wi-Fi widgets must receive QR generation sessions from composition "
        "instead of owning QR workers or QThreads:\n" + "\n".join(violations)
    )


def test_qr_code_generation_policy_has_no_framework_dependencies():
    path = PROJECT_ROOT / "src" / "solin" / "core" / "ingest" / "qr_codes.py"
    violations: list[str] = []

    for node in _imports(path):
        roots = (
            [alias.name.split(".", 1)[0] for alias in node.names]
            if isinstance(node, ast.Import)
            else [(node.module or "").split(".", 1)[0]]
        )
        if any(root in {"PySide6", "solin"} for root in roots):
            violations.append(_display(path, node))

    assert violations == []


def test_wifi_upload_policy_has_no_framework_dependencies():
    path = PROJECT_ROOT / "src" / "solin" / "core" / "ingest" / "wifi_uploads.py"
    violations: list[str] = []

    for node in _imports(path):
        roots = (
            [alias.name.split(".", 1)[0] for alias in node.names]
            if isinstance(node, ast.Import)
            else [(node.module or "").split(".", 1)[0]]
        )
        if "PySide6" in roots:
            violations.append(_display(path, node))

    assert violations == []


def test_watched_folder_source_policies_have_no_framework_dependencies():
    paths = (
        PROJECT_ROOT / "src" / "solin" / "core" / "ingest" / "local_files.py",
        PROJECT_ROOT
        / "src"
        / "solin"
        / "core"
        / "ingest"
        / "meeting_folder_sources.py",
    )
    violations: list[str] = []

    for path in paths:
        for node in _imports(path):
            roots = (
                [alias.name.split(".", 1)[0] for alias in node.names]
                if isinstance(node, ast.Import)
                else [(node.module or "").split(".", 1)[0]]
            )
            if "PySide6" in roots:
                violations.append(_display(path, node))

    assert violations == []


def test_shell_composition_controllers_do_not_store_main_window():
    controller_root = PROJECT_ROOT / "src" / "solin" / "controllers"
    controller_files = (
        "ipc_controller.py",
        "lazy_page_controller.py",
        "language_controller.py",
        "live_integration_controller.py",
        "main_window_bootstrap_controller.py",
        "main_window_ui_controller.py",
        "media_projection_controller.py",
        "navigation_controller.py",
        "open_media_controller.py",
        "playlist_import_controller.py",
        "profile_switch_controller.py",
        "projection_integration_controller.py",
        "projection_stop_controller.py",
        "projection_window_controller.py",
        "remote_services_controller.py",
        "shutdown_controller.py",
        "signal_connection_controller.py",
        "timer_theme_controller.py",
        "wifi_playlist_controller.py",
        "window_state_controller.py",
    )
    violations: list[str] = []

    for filename in controller_files:
        path = controller_root / filename
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Attribute) and node.attr == "_window":
                violations.append(_display(path, node))

    assert violations == []


def test_meeting_domain_has_no_framework_or_application_dependencies():
    meeting_root = PROJECT_ROOT / "src" / "solin" / "core" / "meetings"
    domain_files = (
        "catalog_placement.py",
        "media_nodes.py",
        "models.py",
        "schedule.py",
        "section_meta.py",
        "tree_builder.py",
        "tree_editing.py",
        "tree_merger.py",
        "tree_types.py",
    )
    forbidden_relative = {
        "linked_folder_sync",
        "memorial",
        "publications",
        "schedule_settings",
        "tree_store",
    }
    violations: list[str] = []

    for filename in domain_files:
        path = meeting_root / filename
        for node in _imports(path):
            roots = (
                [alias.name.split(".", 1)[0] for alias in node.names]
                if isinstance(node, ast.Import)
                else [(node.module or "").split(".", 1)[0]]
            )
            relative_root = (
                (node.module or "").split(".", 1)[0]
                if isinstance(node, ast.ImportFrom) and node.level
                else ""
            )
            if (
                any(root in {"PySide6", "solin"} for root in roots)
                or relative_root in forbidden_relative
            ):
                violations.append(_display(path, node))

    assert violations == []


def test_meeting_linked_folder_sync_receives_schedule_resolver():
    path = (
        PROJECT_ROOT
        / "src"
        / "solin"
        / "core"
        / "meetings"
        / "linked_folder_sync.py"
    )
    source = path.read_text(encoding="utf-8")

    assert "MeetingScheduleSettingsStore" not in source
    assert "schedule_settings" not in source
    assert "weekday_for_pub_type" in source


def test_meeting_folder_import_policy_lives_outside_controller():
    controller = (
        PROJECT_ROOT
        / "src"
        / "solin"
        / "widgets"
        / "meetings"
        / "tree_controller.py"
    )
    policy = (
        PROJECT_ROOT
        / "src"
        / "solin"
        / "core"
        / "meetings"
        / "meeting_folder_imports.py"
    )
    controller_source = controller.read_text(encoding="utf-8")
    policy_source = policy.read_text(encoding="utf-8")

    for fragment in (
        "def _meeting_folder_source_supported",
        "def _meeting_folder_record_for_source",
        "def _same_local_source",
        "def _forget_meeting_folder_records_for_path",
    ):
        assert fragment not in controller_source
    assert "PySide6" not in policy_source


def test_timer_domain_has_no_framework_or_application_dependencies():
    timer_root = PROJECT_ROOT / "src" / "solin" / "core" / "timer"
    domain_files = (
        "models.py",
        "part_titles.py",
        "render.py",
        "schedule_factory.py",
        "state_machine.py",
    )
    forbidden_relative = {"engine", "store", "i18n"}
    violations: list[str] = []

    for filename in domain_files:
        path = timer_root / filename
        for node in _imports(path):
            roots = (
                [alias.name.split(".", 1)[0] for alias in node.names]
                if isinstance(node, ast.Import)
                else [(node.module or "").split(".", 1)[0]]
            )
            relative_root = (
                (node.module or "").split(".", 1)[0]
                if isinstance(node, ast.ImportFrom) and node.level
                else ""
            )
            if (
                any(root in {"PySide6", "solin"} for root in roots)
                or relative_root in forbidden_relative
            ):
                violations.append(_display(path, node))

    assert violations == []


def test_profile_domain_has_no_framework_or_application_dependencies():
    path = PROJECT_ROOT / "src" / "solin" / "core" / "profiles" / "models.py"
    violations: list[str] = []

    for node in _imports(path):
        roots = (
            [alias.name.split(".", 1)[0] for alias in node.names]
            if isinstance(node, ast.Import)
            else [(node.module or "").split(".", 1)[0]]
        )
        if any(root in {"PySide6", "solin"} for root in roots):
            violations.append(_display(path, node))

    assert violations == []


def test_profile_infrastructure_receives_global_settings_from_composition():
    path = PROJECT_ROOT / "src" / "solin" / "core" / "profiles" / "infrastructure.py"
    source = path.read_text(encoding="utf-8")

    assert "GlobalSettingsStore.create" not in source
    assert "global_settings: GlobalSettingsStore" in source
    assert "QSettingsProfilePreferences(global_settings)" in source


def test_native_visual_adapters_live_in_presentation_ui():
    core_titlebar_path = (
        PROJECT_ROOT / "src" / "solin" / "core" / "ui" / "titlebar.py"
    )
    core_macos_layer_path = (
        PROJECT_ROOT / "src" / "solin" / "core" / "ui" / "macos_layer.py"
    )
    ui_titlebar_path = PROJECT_ROOT / "src" / "solin" / "ui" / "titlebar.py"
    ui_macos_layer_path = PROJECT_ROOT / "src" / "solin" / "ui" / "macos_layer.py"
    consumers = (
        PROJECT_ROOT / "src" / "solin" / "bootstrap" / "application.py",
        PROJECT_ROOT / "src" / "solin" / "controllers" / "window_state_controller.py",
        PROJECT_ROOT / "src" / "solin" / "widgets" / "quick_access_toolbar.py",
    )

    assert not core_titlebar_path.exists()
    assert not core_macos_layer_path.exists()
    assert ui_titlebar_path.is_file()
    assert ui_macos_layer_path.is_file()
    for path in consumers:
        source = path.read_text(encoding="utf-8")
        assert "core.ui.titlebar" not in source
        assert "core.ui.macos_layer" not in source


def test_presentation_helpers_live_in_ui_package():
    core_helpers_path = PROJECT_ROOT / "src" / "solin" / "core" / "ui" / "helpers.py"
    core_fonts_path = PROJECT_ROOT / "src" / "solin" / "core" / "ui" / "fonts.py"
    ui_helpers_path = PROJECT_ROOT / "src" / "solin" / "ui" / "helpers.py"
    ui_fonts_path = PROJECT_ROOT / "src" / "solin" / "ui" / "fonts.py"
    consumers = (
        PROJECT_ROOT / "src" / "solin" / "projection",
        PROJECT_ROOT / "src" / "solin" / "widgets",
        PROJECT_ROOT / "src" / "solin" / "ui",
    )
    violations: list[str] = []

    assert not core_helpers_path.exists()
    assert not core_fonts_path.exists()
    assert ui_helpers_path.is_file()
    assert ui_fonts_path.is_file()
    for root in consumers:
        for path in sorted(root.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            if "core.ui.helpers" in source or "core.ui.fonts" in source:
                violations.append(str(path.relative_to(PROJECT_ROOT)))

    assert violations == []


def test_screen_manager_lives_in_presentation_ui():
    core_path = PROJECT_ROOT / "src" / "solin" / "core" / "ui" / "screens.py"
    ui_path = PROJECT_ROOT / "src" / "solin" / "ui" / "screens.py"
    consumers = (
        PROJECT_ROOT / "src" / "solin" / "controllers",
        PROJECT_ROOT / "src" / "solin" / "widgets",
        PROJECT_ROOT / "src" / "solin" / "main_window.py",
    )
    violations: list[str] = []

    assert not core_path.exists()
    assert ui_path.is_file()
    for target in consumers:
        paths = [target] if target.is_file() else sorted(target.rglob("*.py"))
        for path in paths:
            source = path.read_text(encoding="utf-8")
            if "core.ui.screens" in source:
                violations.append(str(path.relative_to(PROJECT_ROOT)))

    assert violations == []


def test_monitor_allocation_lives_with_projection_core():
    old_path = PROJECT_ROOT / "src" / "solin" / "core" / "ui" / "monitor_allocation.py"
    new_path = (
        PROJECT_ROOT
        / "src"
        / "solin"
        / "core"
        / "projection"
        / "monitor_allocation.py"
    )
    violations: list[str] = []

    assert not old_path.exists()
    assert new_path.is_file()
    for path in sorted((PROJECT_ROOT / "src" / "solin").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "core.ui.monitor_allocation" in source:
            violations.append(str(path.relative_to(PROJECT_ROOT)))

    assert violations == []


def test_core_ui_package_has_no_python_modules():
    core_ui_root = PROJECT_ROOT / "src" / "solin" / "core" / "ui"
    violations = [
        str(path.relative_to(PROJECT_ROOT))
        for path in sorted(core_ui_root.rglob("*.py"))
    ]
    import_violations: list[str] = []

    for path in sorted((PROJECT_ROOT / "src" / "solin").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "core.ui." in source or "core.ui import" in source:
            import_violations.append(str(path.relative_to(PROJECT_ROOT)))

    assert violations == []
    assert import_violations == []


def test_media_and_playlist_item_domains_have_no_framework_dependencies():
    domain_files = (
        PROJECT_ROOT / "src" / "solin" / "core" / "media" / "duration.py",
        PROJECT_ROOT / "src" / "solin" / "core" / "media" / "formats.py",
        PROJECT_ROOT / "src" / "solin" / "core" / "playlists" / "items.py",
        PROJECT_ROOT
        / "src"
        / "solin"
        / "core"
        / "playlists"
        / "media_reference.py",
    )
    violations: list[str] = []

    for path in domain_files:
        for node in _imports(path):
            imported_module = (
                node.module or ""
                if isinstance(node, ast.ImportFrom)
                else ""
            )
            roots = (
                [alias.name.split(".", 1)[0] for alias in node.names]
                if isinstance(node, ast.Import)
                else [(node.module or "").split(".", 1)[0]]
            )
            if (
                "PySide6" in roots
                or imported_module == "widgets"
                or imported_module.startswith("solin.widgets")
            ):
                violations.append(_display(path, node))

    assert violations == []


def test_jw_identifiers_have_no_framework_or_network_dependencies():
    path = PROJECT_ROOT / "src" / "solin" / "core" / "jw" / "identifiers.py"
    violations: list[str] = []

    for node in _imports(path):
        roots = (
            [alias.name.split(".", 1)[0] for alias in node.names]
            if isinstance(node, ast.Import)
            else [(node.module or "").split(".", 1)[0]]
        )
        if any(root in {"PySide6", "solin"} for root in roots):
            violations.append(_display(path, node))

    assert violations == []


def test_yeartext_content_policy_has_no_framework_dependencies():
    path = PROJECT_ROOT / "src" / "solin" / "core" / "jw" / "yeartext_content.py"
    violations: list[str] = []

    for node in _imports(path):
        roots = (
            [alias.name.split(".", 1)[0] for alias in node.names]
            if isinstance(node, ast.Import)
            else [(node.module or "").split(".", 1)[0]]
        )
        if "PySide6" in roots:
            violations.append(_display(path, node))

    assert violations == []


def test_jw_song_media_policy_has_no_framework_dependencies():
    path = PROJECT_ROOT / "src" / "solin" / "core" / "jw" / "song_media.py"
    violations: list[str] = []

    for node in _imports(path):
        roots = (
            [alias.name.split(".", 1)[0] for alias in node.names]
            if isinstance(node, ast.Import)
            else [(node.module or "").split(".", 1)[0]]
        )
        if "PySide6" in roots:
            violations.append(_display(path, node))

    assert violations == []


def test_jw_catalog_policy_has_no_qt_adapter_dependencies():
    path = PROJECT_ROOT / "src" / "solin" / "core" / "jw" / "catalog.py"
    violations: list[str] = []

    for node in _imports(path):
        roots = (
            [alias.name.split(".", 1)[0] for alias in node.names]
            if isinstance(node, ast.Import)
            else [(node.module or "").split(".", 1)[0]]
        )
        if "PySide6" in roots:
            violations.append(_display(path, node))

    source = path.read_text(encoding="utf-8")
    for fragment in (
        "JWMediaCatalogService",
        "QObject",
        "QRunnable",
        "QThreadPool",
        "Signal",
    ):
        if fragment in source:
            violations.append(f"{path.relative_to(PROJECT_ROOT)} contains {fragment}")

    assert violations == []


def test_jwpub_import_service_has_no_qt_adapter_dependencies():
    path = PROJECT_ROOT / "src" / "solin" / "core" / "jw" / "jwpub_import.py"
    violations: list[str] = []

    for node in _imports(path):
        roots = (
            [alias.name.split(".", 1)[0] for alias in node.names]
            if isinstance(node, ast.Import)
            else [(node.module or "").split(".", 1)[0]]
        )
        if "PySide6" in roots:
            violations.append(_display(path, node))

    source = path.read_text(encoding="utf-8")
    for fragment in ("JwpubImportThread", "QThread", "Signal"):
        if fragment in source:
            violations.append(f"{path.relative_to(PROJECT_ROOT)} contains {fragment}")

    assert violations == []


def test_publication_workflows_use_media_resolver_service():
    paths = (
        PROJECT_ROOT / "src" / "solin" / "core" / "jw" / "jwpub_import.py",
        PROJECT_ROOT / "src" / "solin" / "core" / "meetings" / "publications.py",
    )
    forbidden_imports = {
        "fetch_pub_media_json",
        "resolve_publication_video_link",
        "select_pub_media_file",
    }
    violations: list[str] = []

    for path in paths:
        for node in _imports(path):
            if not isinstance(node, ast.ImportFrom):
                continue
            imported = {alias.name for alias in node.names}
            blocked = sorted(imported & forbidden_imports)
            if blocked:
                violations.append(f"{_display(path, node)} [{', '.join(blocked)}]")

    assert violations == []


def test_memorial_calendar_has_no_framework_dependencies():
    path = PROJECT_ROOT / "src" / "solin" / "core" / "meetings" / "memorial_calendar.py"
    violations: list[str] = []

    for node in _imports(path):
        roots = (
            [alias.name.split(".", 1)[0] for alias in node.names]
            if isinstance(node, ast.Import)
            else [(node.module or "").split(".", 1)[0]]
        )
        if "PySide6" in roots:
            violations.append(_display(path, node))

    assert violations == []


def test_meeting_week_calculations_have_no_framework_dependencies():
    path = PROJECT_ROOT / "src" / "solin" / "core" / "meetings" / "meeting_weeks.py"
    violations: list[str] = []

    for node in _imports(path):
        roots = (
            [alias.name.split(".", 1)[0] for alias in node.names]
            if isinstance(node, ast.Import)
            else [(node.module or "").split(".", 1)[0]]
        )
        if "PySide6" in roots:
            violations.append(_display(path, node))

    assert violations == []


def test_jwpub_cache_storage_has_no_framework_dependencies():
    path = PROJECT_ROOT / "src" / "solin" / "core" / "meetings" / "jwpub_cache.py"
    violations: list[str] = []

    for node in _imports(path):
        roots = (
            [alias.name.split(".", 1)[0] for alias in node.names]
            if isinstance(node, ast.Import)
            else [(node.module or "").split(".", 1)[0]]
        )
        if "PySide6" in roots:
            violations.append(_display(path, node))

    source = path.read_text(encoding="utf-8")
    for fragment in ("QObject", "QThread", "Signal"):
        if fragment in source:
            violations.append(f"{path.relative_to(PROJECT_ROOT)} contains {fragment}")

    assert violations == []


def test_meeting_publication_content_has_no_framework_dependencies():
    path = (
        PROJECT_ROOT
        / "src"
        / "solin"
        / "core"
        / "meetings"
        / "publication_content.py"
    )
    violations: list[str] = []

    for node in _imports(path):
        roots = (
            [alias.name.split(".", 1)[0] for alias in node.names]
            if isinstance(node, ast.Import)
            else [(node.module or "").split(".", 1)[0]]
        )
        if "PySide6" in roots:
            violations.append(_display(path, node))

    source = path.read_text(encoding="utf-8")
    for fragment in ("QObject", "QThread", "Signal"):
        if fragment in source:
            violations.append(f"{path.relative_to(PROJECT_ROOT)} contains {fragment}")

    assert violations == []


def test_memorial_content_has_no_framework_dependencies():
    path = PROJECT_ROOT / "src" / "solin" / "core" / "meetings" / "memorial_content.py"
    violations: list[str] = []

    for node in _imports(path):
        roots = (
            [alias.name.split(".", 1)[0] for alias in node.names]
            if isinstance(node, ast.Import)
            else [(node.module or "").split(".", 1)[0]]
        )
        if "PySide6" in roots:
            violations.append(_display(path, node))

    source = path.read_text(encoding="utf-8")
    for fragment in ("QObject", "QThread", "Signal"):
        if fragment in source:
            violations.append(f"{path.relative_to(PROJECT_ROOT)} contains {fragment}")

    assert violations == []


def test_memorial_publication_client_has_no_framework_dependencies():
    path = PROJECT_ROOT / "src" / "solin" / "core" / "jw" / "memorial_publication.py"
    violations: list[str] = []

    for node in _imports(path):
        roots = (
            [alias.name.split(".", 1)[0] for alias in node.names]
            if isinstance(node, ast.Import)
            else [(node.module or "").split(".", 1)[0]]
        )
        if "PySide6" in roots:
            violations.append(_display(path, node))

    source = path.read_text(encoding="utf-8")
    for fragment in ("QObject", "QThread", "Signal"):
        if fragment in source:
            violations.append(f"{path.relative_to(PROJECT_ROOT)} contains {fragment}")

    assert violations == []


def test_meeting_publication_worker_is_split_from_service_facade():
    facade = PROJECT_ROOT / "src" / "solin" / "core" / "meetings" / "publications.py"
    worker = (
        PROJECT_ROOT
        / "src"
        / "solin"
        / "core"
        / "meetings"
        / "publication_worker.py"
    )

    facade_source = facade.read_text(encoding="utf-8")
    worker_source = worker.read_text(encoding="utf-8")

    assert "class JwpubWorker" not in facade_source
    assert "_get_jwpub_info" not in facade_source
    assert "stream_get" not in facade_source
    assert "class JwpubWorker" in worker_source


def test_meeting_publication_worker_uses_jw_archive_client_boundary():
    path = (
        PROJECT_ROOT
        / "src"
        / "solin"
        / "core"
        / "meetings"
        / "publication_worker.py"
    )
    source = path.read_text(encoding="utf-8")

    assert "stream_get" not in source
    assert "HttpError" not in source
    assert "JwpubMediaRequest" not in source
    assert "PublicationMediaResolver" not in source
    assert "_get_jwpub_info" not in source
    assert "download_jwpub_archive" in source
    assert "resolve_jwpub_archive" in source


def test_publication_archive_client_has_no_framework_dependencies():
    path = PROJECT_ROOT / "src" / "solin" / "core" / "jw" / "publication_archive.py"
    violations: list[str] = []

    for node in _imports(path):
        roots = (
            [alias.name.split(".", 1)[0] for alias in node.names]
            if isinstance(node, ast.Import)
            else [(node.module or "").split(".", 1)[0]]
        )
        if "PySide6" in roots:
            violations.append(_display(path, node))

    source = path.read_text(encoding="utf-8")
    for fragment in ("QObject", "QThread", "Signal"):
        if fragment in source:
            violations.append(f"{path.relative_to(PROJECT_ROOT)} contains {fragment}")

    assert violations == []


def test_memorial_worker_is_split_from_service_facade():
    facade = PROJECT_ROOT / "src" / "solin" / "core" / "meetings" / "memorial.py"
    worker = (
        PROJECT_ROOT
        / "src"
        / "solin"
        / "core"
        / "meetings"
        / "memorial_worker.py"
    )

    facade_source = facade.read_text(encoding="utf-8")
    worker_source = worker.read_text(encoding="utf-8")

    assert "class MemorialWorker" not in facade_source
    assert "resolve_memorial_jwpub" not in facade_source
    assert "download_memorial_bytes" not in facade_source
    assert "extract_memorial_jwpub" not in facade_source
    assert "class MemorialWorker" in worker_source


def test_zoom_state_value_objects_have_no_framework_dependencies():
    path = (
        PROJECT_ROOT
        / "src"
        / "solin"
        / "core"
        / "integrations"
        / "automation"
        / "zoom"
        / "state.py"
    )
    violations: list[str] = []

    for node in _imports(path):
        roots = (
            [alias.name.split(".", 1)[0] for alias in node.names]
            if isinstance(node, ast.Import)
            else [(node.module or "").split(".", 1)[0]]
        )
        if any(root in {"PySide6", "solin"} for root in roots):
            violations.append(_display(path, node))

    assert violations == []


def test_zoom_service_uses_native_adapter_for_win32_calls():
    path = (
        PROJECT_ROOT
        / "src"
        / "solin"
        / "core"
        / "integrations"
        / "automation"
        / "zoom"
        / "service.py"
    )
    source = path.read_text(encoding="utf-8")

    assert "import ctypes" not in source
    assert "ctypes." not in source
    assert "CoInitializeEx" not in source
    assert "FindWindowW(" not in source
    assert "initialize_com_for_current_thread" in source
    assert "share_selection_dialog_open" in source


def test_obs_protocol_has_no_framework_network_or_thread_dependencies():
    path = (
        PROJECT_ROOT
        / "src"
        / "solin"
        / "core"
        / "integrations"
        / "automation"
        / "obs_protocol.py"
    )
    forbidden_roots = {
        "PySide6",
        "solin",
        "threading",
        "time",
        "websocket",
    }
    violations: list[str] = []

    for node in _imports(path):
        roots = (
            [alias.name.split(".", 1)[0] for alias in node.names]
            if isinstance(node, ast.Import)
            else [(node.module or "").split(".", 1)[0]]
        )
        for root in roots:
            if root in forbidden_roots:
                violations.append(_display(path, node))

    source = path.read_text(encoding="utf-8")
    for fragment in ("QObject", "QThread", "Signal", "create_connection"):
        if fragment in source:
            violations.append(f"{path.relative_to(PROJECT_ROOT)} contains {fragment}")

    assert violations == []


def test_ndi_runtime_path_policy_has_no_framework_or_native_loader_dependencies():
    path = PROJECT_ROOT / "src" / "solin" / "core" / "integrations" / "ndi_runtime_paths.py"
    forbidden_roots = {
        "PySide6",
        "ctypes",
        "os",
        "solin",
        "sys",
        "threading",
        "time",
    }
    violations: list[str] = []

    for node in _imports(path):
        roots = (
            [alias.name.split(".", 1)[0] for alias in node.names]
            if isinstance(node, ast.Import)
            else [(node.module or "").split(".", 1)[0]]
        )
        for root in roots:
            if root in forbidden_roots:
                violations.append(_display(path, node))

    assert violations == []


def test_camera_option_model_has_no_framework_dependencies():
    path = PROJECT_ROOT / "src" / "solin" / "core" / "integrations" / "camera_options.py"
    violations: list[str] = []

    for node in _imports(path):
        roots = (
            [alias.name.split(".", 1)[0] for alias in node.names]
            if isinstance(node, ast.Import)
            else [(node.module or "").split(".", 1)[0]]
        )
        if any(root in {"PySide6", "solin"} for root in roots):
            violations.append(_display(path, node))

    assert violations == []


def test_camera_option_consumers_import_model_from_defining_module():
    source_root = PROJECT_ROOT / "src" / "solin"
    forbidden = "core.integrations.camera import CameraOption"
    violations: list[str] = []

    for path in (
        source_root / "controllers" / "live_integration_controller.py",
        source_root / "widgets" / "quick_access_toolbar.py",
        source_root / "widgets" / "camera_popup.py",
    ):
        if forbidden in path.read_text(encoding="utf-8"):
            violations.append(str(path.relative_to(PROJECT_ROOT)))

    camera_service_source = (
        source_root / "core" / "integrations" / "camera.py"
    ).read_text(encoding="utf-8")
    assert "class CameraOption" not in camera_service_source
    assert "class CameraBackend" not in camera_service_source
    assert violations == []


def test_remote_policies_have_no_framework_or_network_dependencies():
    paths = (
        PROJECT_ROOT / "src" / "solin" / "core" / "remote" / "notification_policy.py",
        PROJECT_ROOT / "src" / "solin" / "core" / "remote" / "update_policy.py",
        PROJECT_ROOT / "src" / "solin" / "core" / "remote" / "urls.py",
    )
    violations: list[str] = []

    for path in paths:
        for node in _imports(path):
            roots = (
                [alias.name.split(".", 1)[0] for alias in node.names]
                if isinstance(node, ast.Import)
                else [(node.module or "").split(".", 1)[0]]
            )
            if any(root in {"PySide6", "solin"} for root in roots):
                violations.append(_display(path, node))

    assert violations == []


def test_concrete_media_services_are_constructed_only_in_bootstrap():
    source_root = PROJECT_ROOT / "src" / "solin"
    composition_path = source_root / "bootstrap" / "media.py"
    concrete_names = {
        "MediaCacheManager",
        "MediaController",
        "MediaInfoQueue",
        "MediaInfoService",
        "SongDownloader",
    }
    violations: list[str] = []

    for path in sorted(source_root.rglob("*.py")):
        if path == composition_path:
            continue
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            called_name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            if called_name in concrete_names:
                violations.append(_display(path, node))

    assert violations == [], (
        "Concrete media services must be created only by bootstrap/media.py. "
        "Consumers must receive instances or explicit factories:\n"
        + "\n".join(violations)
    )


def test_media_cache_and_playback_do_not_import_concrete_downloader():
    media_root = PROJECT_ROOT / "src" / "solin" / "core" / "media"
    violations: list[str] = []

    for filename in ("cache.py", "playback.py"):
        path = media_root / filename
        for node in _imports(path):
            imported_module = (
                node.module or ""
                if isinstance(node, ast.ImportFrom)
                else ""
            )
            imported_names = {alias.name for alias in node.names}
            if imported_module.endswith("downloader") or "SongDownloader" in imported_names:
                violations.append(_display(path, node))

    assert violations == []


def test_background_song_service_receives_schedule_source_protocol():
    path = PROJECT_ROOT / "src" / "solin" / "core" / "jw" / "background_song_service.py"
    source = path.read_text(encoding="utf-8")

    assert "MeetingScheduleSettingsStore" not in source
    assert "schedule_settings" not in source
    assert "MeetingScheduleSource" in source
    assert "_schedule_source" in source


def test_jw_language_service_receives_media_language_settings_store():
    path = PROJECT_ROOT / "src" / "solin" / "core" / "jw" / "languages.py"
    source = path.read_text(encoding="utf-8")

    assert "ProfileSettings" not in source
    assert ".for_profile_settings" not in source
    assert "activate_settings" in source
    assert "_media_language_settings" in source


def test_remote_notification_service_receives_notification_settings_store():
    path = PROJECT_ROOT / "src" / "solin" / "core" / "remote" / "notifications.py"
    source = path.read_text(encoding="utf-8")

    assert "ProfileSettings" not in source
    assert ".for_profile_settings" not in source
    assert "_notification_settings" in source


def test_install_identity_receives_installation_settings_store():
    path = PROJECT_ROOT / "src" / "solin" / "core" / "foundation" / "identity.py"
    source = path.read_text(encoding="utf-8")

    assert "InstallationSettingsStore" not in source
    assert "InstallationSettingsStore.create" not in source
    assert "class InstallationIdentitySettings(Protocol)" in source
    assert "get_install_id(settings" in source


def test_remote_workers_receive_install_id_provider():
    for path in (
        PROJECT_ROOT / "src" / "solin" / "core" / "remote" / "notifications.py",
        PROJECT_ROOT / "src" / "solin" / "core" / "remote" / "updates.py",
    ):
        source = path.read_text(encoding="utf-8")

        assert "get_install_id" not in source
        assert "install_id_provider" in source
        assert "_install_id_provider" in source


def test_patch_installer_receives_installation_settings_store():
    path = PROJECT_ROOT / "src" / "solin" / "core" / "remote" / "patch_installer.py"
    source = path.read_text(encoding="utf-8")

    assert "InstallationSettingsStore" not in source
    assert "InstallationSettingsStore.create" not in source
    assert "class PatchCleanupSettings(Protocol)" in source
    assert "cleanup_pending_patch(settings" in source
    assert "save_pending_patch_cleanup(" in source
    assert "settings: PatchCleanupSettings" in source


def test_main_window_receives_profile_settings_from_bootstrap_composition():
    path = PROJECT_ROOT / "src" / "solin" / "main_window.py"
    source = path.read_text(encoding="utf-8")

    assert ".for_profile_settings" not in source
    assert "NotificationSettingsStore" not in source
    assert "WindowGeometrySettingsStore" not in source
    assert "MainWindowProfileSettings" in source


def test_main_window_receives_long_lived_service_factories_from_bootstrap():
    path = PROJECT_ROOT / "src" / "solin" / "main_window.py"
    source = path.read_text(encoding="utf-8")

    for concrete_service in (
        "AutoKeyDispatcher",
        "OBSWebSocketService",
        "NDIReceiverService",
        "CameraService",
        "ZoomService",
        "BackgroundSongService",
        "YeartextService",
        "JwpubService",
        "MemorialService",
    ):
        assert concrete_service not in source
    assert "MainWindowServiceFactories" in source


def test_playlist_widgets_do_not_construct_playlist_repository():
    playlist_widget_root = PROJECT_ROOT / "src" / "solin" / "widgets" / "playlist"
    violations: list[str] = []

    for path in sorted(playlist_widget_root.rglob("*.py")):
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            if (
                node.func.attr == "from_paths"
                and isinstance(owner, ast.Name)
                and owner.id == "PlaylistRepository"
            ):
                violations.append(_display(path, node))

    assert violations == [], (
        "Playlist widgets must receive repository instances from composition:\n"
        + "\n".join(violations)
    )


def test_settings_widgets_do_not_construct_yeartext_service():
    settings_widget_root = PROJECT_ROOT / "src" / "solin" / "widgets" / "settings"
    violations: list[str] = []

    for path in sorted(settings_widget_root.rglob("*.py")):
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            called_name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            if called_name == "YeartextService":
                violations.append(_display(path, node))

    assert violations == [], (
        "Settings widgets must receive yearly text service factories from composition:\n"
        + "\n".join(violations)
    )


def test_meeting_widgets_do_not_construct_meeting_services():
    meetings_widget_root = PROJECT_ROOT / "src" / "solin" / "widgets" / "meetings"
    forbidden = {"JwpubService", "MemorialService"}
    violations: list[str] = []

    for path in sorted(meetings_widget_root.rglob("*.py")):
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            called_name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            if called_name in forbidden:
                violations.append(_display(path, node))

    assert violations == [], (
        "Meeting widgets must receive meeting services from composition:\n"
        + "\n".join(violations)
    )


def test_widgets_do_not_construct_jw_catalog_service():
    widget_root = PROJECT_ROOT / "src" / "solin" / "widgets"
    violations: list[str] = []

    for path in sorted(widget_root.rglob("*.py")):
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            called_name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            if called_name == "JWMediaCatalogService":
                violations.append(_display(path, node))

    assert violations == [], (
        "Widgets must receive JW catalog service factories from composition:\n"
        + "\n".join(violations)
    )


def test_update_dialog_has_no_network_persistence_or_process_adapters():
    path = PROJECT_ROOT / "src" / "solin" / "ui" / "dialogs" / "update.py"
    forbidden_modules = {
        "subprocess",
        "PySide6.QtNetwork",
        "solin.core.foundation.settings_store",
    }
    violations: list[str] = []

    for node in _imports(path):
        modules = (
            [alias.name for alias in node.names]
            if isinstance(node, ast.Import)
            else [node.module or ""]
        )
        if any(module in forbidden_modules for module in modules):
            violations.append(_display(path, node))

    assert violations == []


def test_projection_integration_controller_receives_auto_share_actions():
    path = PROJECT_ROOT / "src" / "solin" / "controllers" / "projection_integration_controller.py"
    source = path.read_text(encoding="utf-8")

    assert "core.integrations.automation.screen_share" not in source
    assert "execute_start_share" not in source
    assert "execute_stop_share" not in source
    assert "start_auto_share" in source
    assert "stop_auto_share" in source
    assert "ctypes" not in source
    assert "SetForegroundWindow" not in source
    assert "sys.platform" not in source
    assert ".winId(" not in source
    assert "raise_projection_window" in source


def test_auto_share_settings_section_receives_native_accessibility_probe():
    path = (
        PROJECT_ROOT
        / "src"
        / "solin"
        / "widgets"
        / "settings"
        / "auto_share_section.py"
    )
    source = path.read_text(encoding="utf-8")

    assert "core.integrations.automation.screen_share" not in source
    assert "macos_accessibility_trusted" not in source
    assert "_auto_share_accessibility_trusted" in source


def test_auto_key_action_model_has_no_framework_settings_or_process_dependencies():
    path = (
        PROJECT_ROOT
        / "src"
        / "solin"
        / "core"
        / "integrations"
        / "automation"
        / "auto_key_actions.py"
    )
    forbidden_roots = {
        "PySide6",
        "ctypes",
        "os",
        "shutil",
        "subprocess",
        "solin",
    }
    violations: list[str] = []

    for node in _imports(path):
        roots = (
            [alias.name.split(".", 1)[0] for alias in node.names]
            if isinstance(node, ast.Import)
            else [(node.module or "").split(".", 1)[0]]
        )
        for root in roots:
            if root in forbidden_roots:
                violations.append(_display(path, node))

    source = path.read_text(encoding="utf-8")
    for fragment in ("QObject", "QThread", "QTimer", "SettingsStore"):
        if fragment in source:
            violations.append(f"{path.relative_to(PROJECT_ROOT)} contains {fragment}")

    assert violations == []


def test_auto_key_dispatcher_has_no_settings_store_dependency():
    path = (
        PROJECT_ROOT
        / "src"
        / "solin"
        / "core"
        / "integrations"
        / "automation"
        / "shortcuts.py"
    )
    source = path.read_text(encoding="utf-8")

    assert "SettingsStore" not in source
    assert "SettingsKey" not in source
    assert "QSETTINGS_PREFS_APP" not in source
    assert "ProfileSettings" not in source
    assert "AutoKeySettingsStore" not in source
    assert "AutoKeySettingsSource" in source


def test_browser_download_widget_has_no_transfer_or_storage_adapters():
    browser_root = PROJECT_ROOT / "src" / "solin" / "widgets" / "browser"
    legacy_url_policy = browser_root / "url_utils.py"
    legacy_presentation_adapters = (
        browser_root / "native_adapters.py",
        browser_root / "scripts.py",
    )
    forbidden_modules = {
        "os",
        "tempfile",
        "threading",
        "urllib.request",
        "solin.core.network.http",
    }
    violations: list[str] = []

    for path in (browser_root / "downloads.py", browser_root / "widget.py"):
        for node in _imports(path):
            modules = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            if any(module in forbidden_modules for module in modules):
                violations.append(_display(path, node))

    assert not legacy_url_policy.exists()
    assert not any(path.exists() for path in legacy_presentation_adapters)
    assert violations == []


def test_browser_url_policy_has_no_framework_dependencies():
    path = PROJECT_ROOT / "src" / "solin" / "core" / "network" / "browser_urls.py"
    violations: list[str] = []

    for node in _imports(path):
        roots = (
            [alias.name.split(".", 1)[0] for alias in node.names]
            if isinstance(node, ast.Import)
            else [(node.module or "").split(".", 1)[0]]
        )
        if any(root in {"PySide6", "solin"} for root in roots):
            violations.append(_display(path, node))

    assert violations == []


def test_media_info_services_live_outside_widget_package():
    legacy_path = (
        PROJECT_ROOT / "src" / "solin" / "widgets" / "media_info_extractor.py"
    )

    assert not legacy_path.exists()


def test_profile_media_bytes_are_persisted_outside_widgets():
    widget_root = PROJECT_ROOT / "src" / "solin" / "widgets"
    paths = (
        widget_root / "playlist" / "import_export.py",
        widget_root / "playlist" / "list_view.py",
        widget_root / "meetings" / "tree_controller.py",
        widget_root / "wifi_receive_widget.py",
        widget_root / "projection" / "bar.py",
    )
    violations: list[str] = []

    for path in paths:
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            is_open = (
                isinstance(function, ast.Name)
                and function.id == "open"
            ) or (
                isinstance(function, ast.Attribute)
                and function.attr == "open"
            )
            if not is_open:
                continue
            mode_index = 1 if isinstance(function, ast.Name) else 0
            if len(node.args) <= mode_index:
                continue
            mode = node.args[mode_index]
            if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
                if any(flag in mode.value for flag in ("w", "a", "x")):
                    violations.append(_display(path, node))

    assert violations == []


def test_widgets_do_not_construct_infrastructure_services_or_repositories():
    widget_root = PROJECT_ROOT / "src" / "solin" / "widgets"
    forbidden_constructors = {
        "JWMediaCatalogService",
        "JwpubService",
        "MemorialService",
        "PlaylistCleanupQueue",
        "PlaylistRepository",
        "WatchedFolderWatcher",
        "WifiReceiveServer",
        "YeartextService",
    }
    violations: list[str] = []

    for path in sorted(widget_root.rglob("*.py")):
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in forbidden_constructors
            ):
                violations.append(_display(path, node))

    assert violations == []


def test_thumbnail_persistence_is_outside_widgets():
    widget_root = PROJECT_ROOT / "src" / "solin" / "widgets"
    legacy_path = widget_root / "playlist" / "thumbnails.py"
    forbidden_fragments = {
        "meeting_thumb_path(",
        "playlist_thumb_path(",
        "shutil.copy2(thumb_path",
        ".save(os.fspath(path), \"JPEG\"",
    }
    violations: list[str] = []

    for path in sorted(widget_root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for fragment in forbidden_fragments:
            if fragment in source:
                violations.append(f"{path.relative_to(PROJECT_ROOT)}: {fragment}")

    assert not legacy_path.exists()
    assert violations == []


def test_widget_file_mutations_go_through_injected_stores():
    widget_root = PROJECT_ROOT / "src" / "solin" / "widgets"
    forbidden_fragments = {
        "os.remove(",
        "os.unlink(",
        "_os.unlink(",
        ".unlink(",
        "shutil.copy2(",
        "shutil.rmtree(",
    }
    violations: list[str] = []

    for path in sorted(widget_root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for fragment in forbidden_fragments:
            if fragment in source:
                violations.append(f"{path.relative_to(PROJECT_ROOT)}: {fragment}")

    assert violations == []


def test_widgets_do_not_read_files_directly():
    widget_root = PROJECT_ROOT / "src" / "solin" / "widgets"
    violations: list[str] = []

    for path in sorted(widget_root.rglob("*.py")):
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if isinstance(function, ast.Name) and function.id == "open":
                violations.append(_display(path, node))
            elif isinstance(function, ast.Attribute) and function.attr == "open":
                violations.append(_display(path, node))

    assert violations == []


def test_widgets_do_not_call_raw_jwlplaylist_reader_or_writer():
    widget_root = PROJECT_ROOT / "src" / "solin" / "widgets"
    violations: list[str] = []

    for path in sorted(widget_root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        forbidden = (
            "core.playlists.reader",
            "core.playlists.writer",
            "read_jwlplaylist(",
            "write_jwlplaylist(",
        )
        if any(fragment in source for fragment in forbidden):
            violations.append(str(path.relative_to(PROJECT_ROOT)))

    assert violations == []


def test_controllers_do_not_call_raw_jwlplaylist_reader_or_writer():
    controller_root = PROJECT_ROOT / "src" / "solin" / "controllers"
    violations: list[str] = []

    for path in sorted(controller_root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        forbidden = (
            "core.playlists.reader",
            "core.playlists.writer",
            "read_jwlplaylist(",
            "write_jwlplaylist(",
        )
        if any(fragment in source for fragment in forbidden):
            violations.append(str(path.relative_to(PROJECT_ROOT)))

    assert violations == []


def test_jwl_document_consumers_use_shared_import_policy():
    consumer_paths = (
        PROJECT_ROOT / "src" / "solin" / "controllers" / "open_media_controller.py",
        PROJECT_ROOT / "src" / "solin" / "controllers" / "playlist_import_controller.py",
        PROJECT_ROOT / "src" / "solin" / "widgets" / "wifi_receive_widget.py",
        PROJECT_ROOT / "src" / "solin" / "widgets" / "meetings" / "tree_controller.py",
        PROJECT_ROOT / "src" / "solin" / "widgets" / "playlist" / "import_export.py",
        PROJECT_ROOT / "src" / "solin" / "widgets" / "playlist" / "list_view.py",
    )
    violations: list[str] = []

    for path in consumer_paths:
        source = path.read_text(encoding="utf-8")
        if "read_jwlplaylist_document" not in source:
            violations.append(f"{path.relative_to(PROJECT_ROOT)}: no document helper")
        if "playlist_items_from_jwl_document_items" not in source:
            violations.append(f"{path.relative_to(PROJECT_ROOT)}: no import policy")

    assert violations == []


def test_widgets_use_jwl_export_service_for_writes():
    widget_root = PROJECT_ROOT / "src" / "solin" / "widgets"
    violations: list[str] = []

    for path in sorted(widget_root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "write_jwlplaylist_document" in source:
            violations.append(str(path.relative_to(PROJECT_ROOT)))

    assert violations == []


def test_main_window_does_not_expose_media_factories_as_service_locator_state():
    path = PROJECT_ROOT / "src" / "solin" / "main_window.py"
    forbidden_attributes = {
        "media_info_queue_factory",
        "media_info_service_factory",
    }
    violations: list[str] = []

    for node in ast.walk(_tree(path)):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and target.attr in forbidden_attributes
            ):
                violations.append(_display(path, node))

    assert violations == []
