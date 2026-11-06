from dataclasses import replace

import pytest

from solin.core.releases.compatibility import ReleaseCompatibility, ReleaseTarget
from solin.core.releases.manifest import ReleaseAsset
from solin.core.remote import release_target


@pytest.mark.parametrize(
    "detected,expected",
    [
        ("2.9", ReleaseCompatibility.SYSTEM_TOO_OLD),
        ("2.42", ReleaseCompatibility.SYSTEM_TOO_OLD),
        ("2.43", ReleaseCompatibility.COMPATIBLE),
        ("2.44", ReleaseCompatibility.COMPATIBLE),
        ("", ReleaseCompatibility.UNVERIFIED),
        ("unknown", ReleaseCompatibility.UNVERIFIED),
    ],
)
def test_system_requirement_comparison_is_numeric_and_unknown_is_not_incompatible(
    detected, expected
):
    asset = ReleaseAsset("linux", "x86_64", "appimage", "Solin.AppImage", 1, "a" * 64, "2.43")
    assert ReleaseTarget("linux", "x86_64", detected).compatibility(asset) == expected


def test_matching_checks_platform_architecture_and_package_before_os_requirements():
    target = ReleaseTarget("windows", "x86_64", "10.0.22631")
    asset = ReleaseAsset("windows", "x86_64", "installer", "Solin.exe", 1, "a" * 64, "10")
    others = (
        replace(asset, platform="linux", kind="appimage", minimum_os="999"),
        replace(asset, architecture="arm64"),
        replace(asset, kind="portable"),
    )
    assert target.matching_asset((*others, asset)) == asset
    assert target.compatibility(asset) == ReleaseCompatibility.COMPATIBLE
    assert target.matching_asset(others) is None
    assert all(target.compatibility(other) == ReleaseCompatibility.UNAVAILABLE for other in others)
    assert target.compatibility(None) == ReleaseCompatibility.UNAVAILABLE


@pytest.mark.parametrize(
    "operating_system,machine,libc,expected",
    [
        ("win32", "AMD64", ("", ""), ReleaseTarget("windows", "x86_64", "10.0.22631")),
        ("darwin", "arm64", ("", ""), ReleaseTarget("macos", "arm64", "14.0")),
        ("darwin", "x86_64", ("", ""), ReleaseTarget("macos", "x86_64", "14.0")),
        ("linux", "aarch64", ("glibc", "2.43"), ReleaseTarget("linux", "arm64", "2.43")),
        ("linux", "x86_64", ("glibc", "2.38"), ReleaseTarget("linux", "x86_64", "2.38")),
        ("linux", "x86_64", ("musl", "1.2.5"), ReleaseTarget("linux", "x86_64", "")),
    ],
)
def test_runtime_target_uses_each_platforms_actual_version_source(
    monkeypatch,
    operating_system,
    machine,
    libc,
    expected,
):
    monkeypatch.setattr(release_target.sys, "platform", operating_system)
    monkeypatch.setattr(release_target.platform, "machine", lambda: machine)
    monkeypatch.setattr(release_target.platform, "version", lambda: "10.0.22631")
    monkeypatch.setattr(release_target.platform, "mac_ver", lambda: ("14.0", (), ""))
    monkeypatch.setattr(release_target.platform, "libc_ver", lambda: libc)
    assert release_target.current_release_target() == expected
