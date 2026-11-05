from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QSettings

from solin.core.foundation import settings_store
from solin.core.foundation.constants import QSETTINGS_APP_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.jw.public_talk_settings import PublicTalkSettingsStore
from solin.core.profiles.settings import ProfileSettings


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings_store,
        "QSettings",
        lambda organization, application: QSettings(
            str(tmp_path / organization / f"{application}.ini"),
            QSettings.Format.IniFormat,
        ),
    )


def _store(profile_id: str = "first") -> PublicTalkSettingsStore:
    return PublicTalkSettingsStore.for_profile_settings(ProfileSettings.for_profile_id(profile_id))


def test_public_talk_source_path_defaults_without_writing_settings() -> None:
    store = _store()

    assert store.source_path() == ""
    assert store.settings.all_keys() == []


def test_public_talk_source_path_roundtrips_and_can_be_explicitly_cleared() -> None:
    store = _store()
    path = "publications/../Public talks/S-34mp_T.jwpub"

    store.set_source_path(path)

    assert _store().source_path() == path
    assert store.settings.string(SettingsKey.PUBLIC_TALK_SOURCE_PATH) == path
    assert store.settings.all_keys() == [SettingsKey.PUBLIC_TALK_SOURCE_PATH]

    store.set_source_path("")

    assert _store().source_path() == ""


def test_public_talk_source_path_is_isolated_between_profiles() -> None:
    first = _store("first")
    second = _store("second")

    first.set_source_path("first/S-34mp.jwpub")

    assert second.source_path() == ""

    second.set_source_path("second/S-34mp.jwpub")
    first.set_source_path("")

    assert _store("first").source_path() == ""
    assert _store("second").source_path() == "second/S-34mp.jwpub"
    assert (
        first.settings.namespace.organization
        == ProfileSettings.for_profile_id("first").organization
    )
    assert first.settings.namespace.application == QSETTINGS_APP_APP


def test_public_talk_source_path_is_preserved_when_file_is_missing_or_invalid(
    tmp_path: Path,
) -> None:
    source = tmp_path / "S-34mp.jwpub"
    store = _store()

    store.set_source_path(str(source))

    assert not source.exists()
    assert _store().source_path() == str(source)

    source.write_bytes(b"not a publication archive")
    assert _store().source_path() == str(source)

    source.unlink()
    assert _store().source_path() == str(source)
    assert store.settings.string(SettingsKey.PUBLIC_TALK_SOURCE_PATH) == str(source)
