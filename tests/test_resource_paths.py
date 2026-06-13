from solin.core.foundation.resources import application_asset_path


def test_application_asset_path_resolves_repository_asset() -> None:
    icon_path = application_asset_path("icon.ico")

    assert icon_path.is_file()
    assert icon_path.name == "icon.ico"
