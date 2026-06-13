from __future__ import annotations

from pathlib import Path


def package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def application_resource_roots() -> tuple[Path, ...]:
    package = package_root()
    return (
        package.parent,
        package.parents[1],
        Path.cwd(),
    )


def application_asset_path(filename: str) -> Path:
    for root in application_resource_roots():
        candidate = root / "assets" / filename
        if candidate.is_file():
            return candidate
    return package_root().parents[1] / "assets" / filename
