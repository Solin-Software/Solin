from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from solin.core.remote.update_policy import (
    ReleaseVersion,
    UpdateKind,
    evaluate_update,
    is_safe_update_url,
)


def test_release_version_parses_two_to_four_numeric_components():
    assert str(ReleaseVersion.parse("26.17")) == "26.17.0.0"
    assert str(ReleaseVersion.parse("26.17.1")) == "26.17.1.0"
    assert str(ReleaseVersion.parse("26.17.1.2")) == "26.17.1.2"


@pytest.mark.parametrize(
    "raw",
    ["", "26", "26.17.1.2.3", "26.beta.1", "v26.17.1", "-1.2"],
)
def test_release_version_rejects_ambiguous_or_invalid_values(raw):
    assert ReleaseVersion.parse(raw) is None


def test_release_version_is_ordered_and_immutable():
    current = ReleaseVersion.parse("26.17.1.0")
    candidate = ReleaseVersion.parse("26.17.2")

    assert current is not None
    assert candidate is not None
    assert candidate > current
    with pytest.raises(FrozenInstanceError):
        candidate.components = (1, 0, 0, 0)  # type: ignore[misc]


def test_update_policy_prefers_eligible_patch():
    info = evaluate_update(
        {
            "patch": {
                "version": "26.17.2.0",
                "min_version": "26.17.0.0",
                "url": "https://releases.example/SolinPatch.exe",
            },
            "setup": {
                "version": "27.0.0.0",
                "url": "https://releases.example/SolinSetup.exe",
            },
        },
        current_version="26.17.1.0",
    )

    assert info is not None
    assert info.kind is UpdateKind.PATCH
    assert str(info.version) == "26.17.2.0"


def test_update_policy_falls_back_to_setup_when_patch_is_ineligible():
    info = evaluate_update(
        {
            "patch": {
                "version": "26.17.2.0",
                "min_version": "26.18.0.0",
                "url": "https://releases.example/SolinPatch.exe",
            },
            "setup": {
                "version": "27.0.0.0",
                "url": "https://releases.example/SolinSetup.exe",
            },
        },
        current_version="26.17.1.0",
    )

    assert info is not None
    assert info.kind is UpdateKind.SETUP


def test_update_policy_rejects_invalid_versions_and_unsafe_urls():
    assert (
        evaluate_update(
            {
                "setup": {
                    "version": "26.bad.2",
                    "url": "https://releases.example/SolinSetup.exe",
                }
            },
            current_version="26.17.1.0",
        )
        is None
    )
    assert (
        evaluate_update(
            {
                "setup": {
                    "version": "27.0.0.0",
                    "url": "http://releases.example/SolinSetup.exe",
                }
            },
            current_version="26.17.1.0",
        )
        is None
    )


def test_safe_update_urls_allow_https_and_local_development_http():
    assert is_safe_update_url("https://releases.example/setup.exe") is True
    assert is_safe_update_url("http://localhost:5000/setup.exe") is True
    assert is_safe_update_url("http://127.0.0.1:5000/setup.exe") is True
    assert is_safe_update_url("http://releases.example/setup.exe") is False
    assert is_safe_update_url("file:///tmp/setup.exe") is False
