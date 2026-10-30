"""Portable naming is independent of the operating system running the tests."""

import unicodedata

import pytest

from solin.core.foundation.folder_names import (
    FolderNameError,
    FolderNameIssue,
    ensure_available_folder_name,
    folder_name_key,
    normalize_portable_folder_name,
)
from solin.core.ingest.playlist_folder_names import (
    MeetingFolderNameError,
    normalize_playlist_folder_name,
)
from solin.core.meetings.folder_matcher import match_meeting_folder


@pytest.mark.parametrize(
    "name",
    [
        "Special event",
        "Reunião",
        "日本語",
        "لقاء",
        "🎵 Music",
        "COM10",
        "CONcert",
        "ＡＵＸ",
        "name.ext",
    ],
)
def test_portable_names_preserve_unicode_and_visible_spelling(name):
    assert normalize_portable_folder_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        None,
        123,
        "",
        ".",
        "..",
        ".hidden",
        ".solin_sync",
        " leading",
        "trailing ",
        "name.",
        "\tname",
        "name\u00a0",
        "\u2003name",
        "a/b",
        "a\\b",
        'a"b',
        "a<b",
        "a>b",
        "a:b",
        "a|b",
        "a?b",
        "a*b",
        "../escape",
        "C:\\escape",
        "\\\\server\\share",
        "a\x00b",
        "a\x1fb",
        "a\x7fb",
        "a\ud800b",
    ],
)
def test_invalid_components_are_rejected_without_sanitizing(name):
    with pytest.raises(FolderNameError):
        normalize_portable_folder_name(name)


@pytest.mark.parametrize(
    "device",
    [
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CONIN$",
        "CONOUT$",
        "CLOCK$",
        *[f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in "123456789¹²³"],
    ],
)
@pytest.mark.parametrize("suffix", ["", ".txt", " .archive.zip"])
def test_windows_device_basenames_are_reserved_even_with_extensions(device, suffix):
    with pytest.raises(FolderNameError) as error:
        normalize_portable_folder_name(device.lower() + suffix)
    assert error.value.issue is FolderNameIssue.RESERVED


@pytest.mark.parametrize("name", ["x" * 255, "é" * 127, "🎵" * 63])
def test_portable_length_boundaries_allow_valid_components(name):
    result = normalize_portable_folder_name(name)
    assert len(result.encode("utf-8")) <= 255
    assert len(result.encode("utf-16-le")) // 2 <= 255


@pytest.mark.parametrize("name", ["x" * 256, "é" * 128, "🎵" * 64, "🎵" * 128])
def test_portable_length_boundaries_reject_without_truncation(name):
    with pytest.raises(FolderNameError) as error:
        normalize_portable_folder_name(name)
    assert error.value.issue is FolderNameIssue.TOO_LONG


def test_normalization_precedes_length_validation():
    name = "e\u0301" * 127
    assert normalize_portable_folder_name(name) == "é" * 127


@pytest.mark.parametrize(
    "existing, requested",
    [("Reunião", "REUNIA\u0303O"), ("Straße", "STRASSE"), ("Café", "Cafe\u0301"), ("FOO", "foo")],
)
def test_collisions_use_canonical_unicode_and_casefold(existing, requested):
    assert folder_name_key(existing) == folder_name_key(requested)
    with pytest.raises(FolderNameError) as error:
        ensure_available_folder_name(requested, [existing])
    assert error.value.issue is FolderNameIssue.CONFLICT


def test_rename_excludes_only_the_exact_original_entry():
    assert ensure_available_folder_name("MUSIC", ["Music"], excluding_name="Music") == "MUSIC"
    with pytest.raises(FolderNameError):
        ensure_available_folder_name("MUSIC", ["Music", "music"], excluding_name="Music")


@pytest.mark.parametrize(
    "name", ["2026-10-21 MW", "2026-10-25 we", "2026-10-25   WE", "2026-10-25\u2003MW"]
)
def test_linked_playlist_policy_uses_meeting_classifier(name):
    assert match_meeting_folder(name) is not None
    with pytest.raises(MeetingFolderNameError):
        normalize_playlist_folder_name(name)


@pytest.mark.parametrize(
    "name", ["2026-02-30 MW", "2026-10-21", "2026-10-21 Other", "Special meeting"]
)
def test_names_not_recognized_by_classifier_remain_playlists(name):
    assert match_meeting_folder(name) is None
    assert normalize_playlist_folder_name(name) == unicodedata.normalize("NFC", name)
