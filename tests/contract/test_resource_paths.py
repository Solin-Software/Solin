from pathlib import Path
import tomllib

from solin.core.foundation.resources import (
    application_asset_path,
    application_resource_path,
    application_translation_root,
    package_root,
)
from tests._paths import REPO_ROOT


def test_application_asset_path_resolves_packaged_asset() -> None:
    icon_path = application_asset_path("icon.ico")

    assert icon_path.is_file()
    assert icon_path.name == "icon.ico"
    assert icon_path.parts[-4:-1] == ("solin", "resources", "assets")


def test_application_translation_root_resolves_packaged_resources() -> None:
    translation_root = application_translation_root()

    assert (translation_root / "locales" / "pt_BR.json").is_file()
    assert translation_root.parts[-3:] == ("solin", "resources", "translations")


def test_application_resources_are_resolved_strictly_from_package_root() -> None:
    expected = package_root() / "resources" / "assets" / "icon.ico"

    assert application_resource_path("assets", "icon.ico") == expected
    assert application_resource_path("missing", "file.txt") == (
        package_root() / "resources" / "missing" / "file.txt"
    )


def test_runtime_resources_are_declared_as_solin_package_data() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = pyproject["tool"]["setuptools"]["package-data"]["solin"]

    assert not (REPO_ROOT / "resources").exists()
    assert Path("resources/assets/*.ico").as_posix() in package_data
    assert Path("resources/assets/*.icns").as_posix() in package_data
    assert Path("resources/translations/*.qm").as_posix() in package_data
    assert Path("resources/translations/locales/*.json").as_posix() in package_data
    assert Path("resources/translations/*.ts").as_posix() not in package_data
