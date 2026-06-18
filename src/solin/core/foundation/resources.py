from __future__ import annotations

from pathlib import Path


def package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def application_resource_roots() -> tuple[Path, ...]:
    package = package_root()
    return (
        package,
        package.parent,
        package.parents[1],
        Path.cwd(),
    )


def application_resource_path(*parts: str) -> Path:
    for root in application_resource_roots():
        candidate = root / "resources" / Path(*parts)
        if candidate.exists():
            return candidate
    return package_root().parents[1] / "resources" / Path(*parts)


def application_asset_path(filename: str) -> Path:
    return application_resource_path("assets", filename)


def application_translation_root() -> Path:
    return application_resource_path("translations")
