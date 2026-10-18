from dataclasses import replace

import pytest

from solin.core.releases.channel import UpdateChannel
from solin.core.releases.manifest import (
    REQUIRED_ASSETS, LocalizedReleaseNotes, ReleaseAsset, ReleaseManifest,
)
from solin.core.releases.version import ReleaseVersion
from solin.core.remote.update_policy import (
    ReleaseNote, UpdateAction, UpdateInfo, evaluate_update, is_update_candidate,
)


def manifest(value="26.32.0", platform="windows", architecture="x86_64", kind="installer"):
    version = ReleaseVersion.parse(value)
    assert version
    asset = ReleaseAsset(platform, architecture, kind, "Solin.exe", 3, "a" * 64, "10.0")
    return ReleaseManifest(
        version,
        version.tag,
        version.channel,
        "a" * 40,
        (asset,),
        (LocalizedReleaseNotes(version, {"en": "Changes", "pt_BR": "Mudanças"}),),
    )


def select(releases, **kwargs):
    defaults = dict(
        current_version="26.31.0",
        platform="windows",
        architecture="x86_64",
        os_version="10.0.22631",
        installed_windows=True,
    )
    defaults.update(kwargs)
    return evaluate_update(releases, **defaults)


@pytest.mark.parametrize("current,candidate,stable,beta", [
    ("26.32.0", "26.31.0", False, False),
    ("26.32.0", "26.32.0", False, False),
    ("26.32.0", "26.33.0", True, True),
    ("26.32.0", "26.32.0b10", False, False),
    ("26.32.0", "26.33.0b1", False, True),
    ("26.32.0b2", "26.31.0", True, False),
    ("26.32.0b2", "26.32.0", True, True),
    ("26.32.0b2", "26.33.0", True, True),
    ("26.32.0b2", "26.31.0b1", False, False),
    ("26.32.0b2", "26.32.0b1", False, False),
    ("26.32.0b2", "26.32.0b2", False, False),
    ("26.32.0b2", "26.32.0b10", False, True),
    ("26.32.0b2", "26.33.0b1", False, True),
])
@pytest.mark.parametrize("channel", [UpdateChannel.STABLE, UpdateChannel.BETA])
def test_candidate_eligibility_matrix(current, candidate, stable, beta, channel):
    installed = ReleaseVersion.parse(current)
    target = ReleaseVersion.parse(candidate)
    assert installed is not None and target is not None
    expected = beta if channel == UpdateChannel.BETA else stable
    assert is_update_candidate(target, current_version=installed, channel=channel) is expected
    result = select([manifest(candidate)], current_version=installed, channel=channel)
    assert (result is not None) is expected


@pytest.mark.parametrize("current,target,channel,downgrade", [
    ("26.31.0", "26.32.0", UpdateChannel.STABLE, False),
    ("26.31.0", "26.32.0b1", UpdateChannel.BETA, False),
    ("26.32.0b1", "26.32.0b2", UpdateChannel.BETA, False),
    ("26.32.0b1", "26.32.0", UpdateChannel.STABLE, False),
    ("26.32.0b1", "26.32.0", UpdateChannel.BETA, False),
    ("26.33.0b1", "26.32.0", UpdateChannel.STABLE, True),
])
def test_selection_populates_source_version_and_downgrade_metadata(current, target, channel, downgrade):
    release = manifest(target)
    result = select([release], current_version=current, channel=channel)
    assert result is not None
    assert result.current_version == ReleaseVersion.parse(current)
    assert result.version == release.version
    assert result.is_downgrade is downgrade
    assert result.asset == release.assets[0]
    assert result.url == release.assets[0].download_url(release.tag)


def test_update_info_preserves_existing_positional_construction():
    release = manifest()
    notes = (ReleaseNote(release.version, "Sample notes", "en"),)
    info = UpdateInfo(
        UpdateAction.INSTALL, release.version, release.assets[0], "https://example.test", notes,
    )
    assert info.changelog == notes
    assert info.current_version is None
    assert info.is_downgrade is False


def test_stable_ignores_beta_and_never_downgrades_stable_installations():
    assert select([manifest("26.32.0b1")]) is None
    assert select([manifest()], current_version="26.33.0") is None


def test_stable_channel_returns_from_beta_to_older_stable():
    stable = manifest()
    result = select([stable], current_version="26.33.0b1")
    assert result is not None
    assert result.version == stable.version


@pytest.mark.parametrize("language,selected_language", [("pt_BR", "pt_BR"), ("es", "en")])
def test_downgrade_uses_only_target_manifests_own_notes(language, selected_language):
    older, target, newer = manifest("26.31.0"), manifest(), manifest("26.34.0")
    target = replace(target, notes=(*older.notes, *target.notes))
    newer = replace(
        newer,
        assets=(replace(newer.assets[0], architecture="arm64"),),
        notes=(
            LocalizedReleaseNotes(target.version, {"en": "Summary from a different release"}),
            *newer.notes,
        ),
    )
    result = select([newer, older, target], current_version="26.33.0b1", language=language)
    assert result is not None
    assert result.is_downgrade
    assert result.changelog == (
        ReleaseNote(target.version, target.notes[-1].translations[selected_language], selected_language),
    )


@pytest.mark.parametrize("incompatibility", [
    {"architecture": "arm64"},
    {"platform": "macos"},
    {"kind": "portable"},
    {"minimum_os": "11"},
    {"minimum_os": "invalid"},
])
def test_return_to_stable_chooses_highest_compatible_release(incompatibility):
    older, target, newer = manifest("26.30.0"), manifest(), manifest("26.34.0")
    newer = replace(newer, assets=(replace(newer.assets[0], **incompatibility),))
    releases = [older, manifest("26.35.0b1"), target, newer]
    result = select(releases, current_version="26.33.0b1")
    assert result is not None
    assert result.version == target.version
    assert result.is_downgrade
    assert select([newer], current_version="26.33.0b1") is None


def test_stable_channel_prefers_newer_stable_over_return_to_older_stable():
    newer = manifest("26.34.0")
    result = select([manifest(), newer], current_version="26.33.0b1")
    assert result is not None
    assert result.version == newer.version
    assert result.is_downgrade is False
    assert [note.version for note in result.changelog] == [newer.version]


def test_regular_update_keeps_incremental_localized_notes_from_consolidated_manifest():
    older, intermediate, newest = manifest("26.30.0"), manifest(), manifest("26.33.0")
    newest = replace(newest, notes=(*older.notes, *intermediate.notes, *newest.notes))
    result = select([newest], language="pt_BR")
    assert result is not None
    assert result.changelog == tuple(
        ReleaseNote(release.version, release.notes[0].translations["pt_BR"], "pt_BR")
        for release in (manifest("26.33.0"), intermediate)
    )


def test_beta_orders_numerically_and_accepts_final():
    result = select([manifest("26.32.0b2"), manifest("26.32.0b10")], channel=UpdateChannel.BETA)
    assert result.version.canonical == "26.32.0b10"
    assert [note.version.canonical for note in result.changelog] == ["26.32.0b10", "26.32.0b2"]
    result = select([manifest("26.32.0b10"), manifest()], channel=UpdateChannel.BETA)
    assert result.version.canonical == "26.32.0"
    assert len(result.changelog) == 1
    assert result.changelog[0].version.canonical == "26.32.0"


def test_selection_requires_compatible_architecture_os_and_package():
    assert select([manifest(architecture="arm64")]) is None
    assert select([manifest()], os_version="6.1") is None
    result = select([manifest()], installed_windows=False)
    assert result.action == UpdateAction.REVEAL


def test_localized_notes_fall_back_and_noncanonical_current_version_is_rejected():
    assert select([manifest()], language="pt_BR").changelog[0].markdown == "Mudanças"
    assert select([manifest()], language="es").changelog[0].language == "en"
    assert select([manifest()], current_version="26.32.0.1") is None


def test_older_compatible_release_is_used_when_newest_cannot_run():
    newer = manifest("26.33.0")
    newer = replace(newer, assets=(replace(newer.assets[0], minimum_os="11"),))
    assert select([newer, manifest()]).version.canonical == "26.32.0"


def test_elevated_installation_gets_full_setup_for_assisted_install():
    result = select([manifest()], automatic_windows=False)
    assert result.asset.kind == "installer"
    assert result.action == UpdateAction.REVEAL


@pytest.mark.parametrize("glibc,compatible", [("2.35", False), ("2.37", False), ("2.38", True), ("2.39", True)])
def test_linux_update_respects_the_packaged_dependency_abi_floor(glibc, compatible):
    release = manifest(platform="linux", kind="appimage")
    floor = REQUIRED_ASSETS[("linux", "x86_64", "appimage")][1]
    release = replace(release, assets=(replace(release.assets[0], minimum_os=floor),))
    result = select([release], platform="linux", os_version=glibc)
    assert (result is not None) is compatible
