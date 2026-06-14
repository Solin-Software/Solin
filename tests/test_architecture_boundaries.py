from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


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


def test_shell_composition_controllers_do_not_store_main_window():
    controller_root = PROJECT_ROOT / "src" / "solin" / "controllers"
    controller_files = (
        "ipc_controller.py",
        "lazy_page_controller.py",
        "live_integration_controller.py",
        "main_window_bootstrap_controller.py",
        "navigation_controller.py",
        "open_media_controller.py",
        "playlist_import_controller.py",
        "remote_services_controller.py",
        "shutdown_controller.py",
        "signal_connection_controller.py",
        "wifi_playlist_controller.py",
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
        "models.py",
        "schedule.py",
        "section_meta.py",
        "tree_builder.py",
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


def test_media_and_playlist_item_domains_have_no_framework_dependencies():
    domain_files = (
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


def test_remote_update_policy_has_no_framework_or_network_dependencies():
    path = (
        PROJECT_ROOT
        / "src"
        / "solin"
        / "core"
        / "remote"
        / "update_policy.py"
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
