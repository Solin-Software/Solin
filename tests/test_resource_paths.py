from solin.core.foundation.resources import application_asset_path, application_translation_root


def test_application_asset_path_resolves_repository_asset() -> None:
    icon_path = application_asset_path("icon.ico")

    assert icon_path.is_file()
    assert icon_path.name == "icon.ico"
    assert icon_path.parts[-3:-1] == ("resources", "assets")


def test_application_translation_root_resolves_repository_resources() -> None:
    translation_root = application_translation_root()

    assert (translation_root / "locales" / "pt_BR.json").is_file()
    assert translation_root.parts[-2:] == ("resources", "translations")
