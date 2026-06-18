from __future__ import annotations

from pathlib import Path


def package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def application_resource_path(*parts: str) -> Path:
    return package_root() / "resources" / Path(*parts)


def application_asset_path(filename: str) -> Path:
    return application_resource_path("assets", filename)


def application_translation_root() -> Path:
    return application_resource_path("translations")
