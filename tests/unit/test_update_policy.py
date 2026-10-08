from dataclasses import replace

import pytest

from solin.core.releases.channel import UpdateChannel
from solin.core.releases.manifest import (
    REQUIRED_ASSETS, LocalizedReleaseNotes, ReleaseAsset, ReleaseManifest,
)
from solin.core.releases.version import ReleaseVersion
from solin.core.remote.update_policy import UpdateAction, evaluate_update


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


def test_stable_ignores_beta_and_never_downgrades():
    assert select([manifest("26.32.0b1")]) is None
    assert select([manifest()], current_version="26.33.0b1") is None


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
