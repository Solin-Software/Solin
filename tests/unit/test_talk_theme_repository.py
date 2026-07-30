from __future__ import annotations

from dataclasses import replace

import pytest
from PIL import Image

from solin.core.talk_theme.models import TalkThemeLibrary, TalkThemePreset
from solin.core.talk_theme.presets import default_document
from solin.core.talk_theme.repository import (
    TalkThemeAssetError,
    TalkThemeAssetErrorCode,
    TalkThemeAssetStore,
    TalkThemeRepository,
)


def test_repository_recovers_from_invalid_json(tmp_path) -> None:
    path = tmp_path / "talk_theme.json"
    path.write_text("not-json", encoding="utf-8")

    library = TalkThemeRepository(path).load()

    assert library.document == default_document()


def test_repository_round_trip_restores_last_explicitly_saved_preset(tmp_path) -> None:
    repository = TalkThemeRepository(tmp_path / "talk_theme.json")
    document = replace(default_document(), updated_at="2026-01-02T03:04:05+00:00")
    preset = TalkThemePreset(id="mine", name="Mine", document=document)
    expected = TalkThemeLibrary(
        document=document,
        user_presets=(preset,),
        last_saved_preset_id="mine",
    )

    repository.save(expected)

    assert repository.load() == expected


def test_repository_does_not_persist_an_unsaved_working_document(tmp_path) -> None:
    repository = TalkThemeRepository(tmp_path / "talk_theme.json")
    unsaved = replace(default_document(), updated_at="2026-01-02T03:04:05+00:00")

    repository.save(TalkThemeLibrary(document=unsaved))

    assert repository.load().document == default_document()


def test_asset_store_normalizes_and_deduplicates_images(tmp_path) -> None:
    source = tmp_path / "source.png"
    Image.new("RGB", (5000, 2500), "#334466").save(source)
    store = TalkThemeAssetStore(tmp_path / "assets", max_long_edge=1024)

    first = store.import_image(source)
    second = store.import_image(source)

    assert first == second
    assert first.suffix == ".jpg"
    with Image.open(first) as normalized:
        assert normalized.size == (1024, 512)


def test_asset_store_prunes_only_unreferenced_managed_files(tmp_path) -> None:
    directory = tmp_path / "assets"
    directory.mkdir()
    kept = directory / "kept.jpg"
    removed = directory / "removed.png"
    kept.write_bytes(b"kept")
    removed.write_bytes(b"removed")

    result = TalkThemeAssetStore(directory).prune_unreferenced({kept.name})

    assert result == (removed,)
    assert kept.exists()
    assert not removed.exists()


def test_asset_store_returns_a_stable_error_code_for_unsupported_images(tmp_path) -> None:
    source = tmp_path / "source.gif"
    Image.new("RGB", (32, 32), "#334466").save(source)

    with pytest.raises(TalkThemeAssetError) as raised:
        TalkThemeAssetStore(tmp_path / "assets").import_image(source)

    assert raised.value.code is TalkThemeAssetErrorCode.UNSUPPORTED_FORMAT
