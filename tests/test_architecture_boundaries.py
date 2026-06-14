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


def test_meeting_schedule_domain_has_no_framework_or_application_dependencies():
    path = PROJECT_ROOT / "src" / "solin" / "core" / "meetings" / "schedule.py"
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
