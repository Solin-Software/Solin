from __future__ import annotations

from copy import deepcopy

import pytest

from scripts import release as release_script
from scripts.release import calculate_next, resolve_predecessor
from scripts.release_contributors import (
    Change,
    Credits,
    ContributionRange,
    HISTORICAL_BASE,
    RelatedIssue,
)
from solin.core.releases.manifest import (
    LocalizedReleaseNotes,
    REQUIRED_ASSETS,
    ReleaseAsset,
    ReleaseManifest,
    asset_filename,
)
from solin.core.releases.version import ReleaseVersion


def version(value: str) -> ReleaseVersion:
    result = ReleaseVersion.parse(value)
    assert result is not None
    return result


def manifest_payload(value: str = "26.32.0") -> dict:
    release = version(value)
    return {
        "schema_version": 1,
        "version": release.canonical,
        "tag": release.tag,
        "channel": release.channel,
        "commit_sha": "a" * 40,
        "assets": [
            {
                "platform": platform,
                "architecture": architecture,
                "kind": kind,
                "filename": asset_filename(release, platform, architecture, kind),
                "size": 10,
                "sha256": "b" * 64,
                "minimum_os": contract[1],
            }
            for (platform, architecture, kind), contract in REQUIRED_ASSETS.items()
        ],
        "notes": [{"version": release.canonical, "translations": {"en": "Changes"}}],
    }


def test_calver_projects_beta_and_stable_native_versions() -> None:
    beta = version("26.32.1b10")
    stable = version("26.32.1")

    assert beta.display_version == "26.32.1-beta.10"
    assert beta.tag == "26.32.1-beta.10"
    assert beta.windows_version == "26.32.1.10"
    assert stable.windows_version == "26.32.1.65535"
    assert beta.macos_build == "26.32.65546"
    assert stable.macos_build == "26.32.131071"
    assert beta.notification_version == "26.32.1.10"
    assert stable.notification_version == "26.32.1.0"
    assert beta < stable


@pytest.mark.parametrize("value", ["26.31.1.0", "26.31.1.7"])
def test_public_versions_reject_a_fourth_component(value: str) -> None:
    assert ReleaseVersion.parse(value) is None


def test_release_intents_cover_year_rollover_beta_progression_and_promotion() -> None:
    assert calculate_next(version("26.31.1"), "release", 27).canonical == "27.1.0"
    assert calculate_next(version("26.31.1"), "patch", 26).canonical == "26.31.2"
    assert calculate_next(version("26.31.1"), "beta", 26).canonical == "26.32.0b1"
    assert calculate_next(version("26.32.0b9"), "beta", 26).canonical == "26.32.0b10"
    assert calculate_next(version("26.32.0b10"), "promote", 26).canonical == "26.32.0"
    with pytest.raises(ValueError, match="Promote or continue"):
        calculate_next(version("26.32.0b1"), "release", 26)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data["assets"].pop(),
        lambda data: data["assets"][0].update(sha256="0" * 63),
        lambda data: data["assets"][0].update(filename="unexpected.exe"),
        lambda data: data.update(commit_sha="main"),
        lambda data: data["notes"][0].update(translations={"pt_BR": "Mudanças"}),
    ],
)
def test_manifest_rejects_incomplete_or_untrusted_distribution_metadata(mutation) -> None:
    payload = deepcopy(manifest_payload())
    mutation(payload)
    assert ReleaseManifest.parse(payload) is None


def test_manifest_accepts_exact_cross_platform_inventory() -> None:
    parsed = ReleaseManifest.parse(manifest_payload("26.32.0b2"))
    assert parsed is not None
    assert parsed.version.canonical == "26.32.0b2"
    assert len(parsed.assets) == 4


def test_first_centralized_release_uses_pinned_transition_predecessor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLIN_TRANSITION_WINDOWS_URL", "https://example.test/Solin.exe")
    monkeypatch.setenv("SOLIN_TRANSITION_WINDOWS_SHA256", "c" * 64)

    result = resolve_predecessor(
        "windows",
        "x86_64",
        version("26.32.0"),
        [{"draft": False, "tag_name": "26.31.1", "assets": []}],
    )

    assert result == ("https://example.test/Solin.exe", "c" * 64)


def publish_manifest() -> ReleaseManifest:
    release = version("26.32.0")
    return ReleaseManifest(
        release,
        release.tag,
        release.channel,
        "a" * 40,
        (ReleaseAsset("linux", "x86_64", "appimage", "Solin.AppImage", 1, "b" * 64, "2.38"),),
        (LocalizedReleaseNotes(release, {"en": "Changes"}),),
    )


@pytest.fixture
def publication_credits(monkeypatch):
    repository_url = f"https://github.com/{release_script.RELEASE_REPOSITORY}"
    change = Change(
        "fix(player): Restore playback",
        "PR #7",
        f"{repository_url}/pull/7",
        ("contributor",),
        (RelatedIssue("#3", f"{repository_url}/issues/3"),),
    )
    credits = Credits(ContributionRange(HISTORICAL_BASE, "a" * 40, ("a" * 40,)), (change,), ())
    monkeypatch.setattr(release_script, "release_credits", lambda *_args, **_kwargs: credits)
    monkeypatch.setattr(release_script, "verify_identity", lambda tag, sha: ReleaseVersion.from_tag(tag))
    return credits


def test_publisher_refuses_to_modify_an_already_published_release(monkeypatch, tmp_path) -> None:
    manifest = publish_manifest()
    (tmp_path / "release-notes.md").write_text("Changes", encoding="utf-8")
    monkeypatch.setattr(release_script, "create_manifest", lambda *_args, **_kwargs: manifest)
    monkeypatch.setattr(
        release_script,
        "published_releases",
        lambda: [{"tag_name": manifest.tag, "draft": False}],
    )

    with pytest.raises(ValueError, match="immutable"):
        release_script.publish(tmp_path, manifest.tag, manifest.commit_sha)


def test_publisher_keeps_partial_draft_unpublished(
    monkeypatch, tmp_path, publication_credits
) -> None:
    manifest = publish_manifest()
    marker = f"<!-- source-commit: {manifest.commit_sha} -->"
    (tmp_path / "release-notes.md").write_text("Changes", encoding="utf-8")
    monkeypatch.setattr(release_script, "create_manifest", lambda *_args, **_kwargs: manifest)
    monkeypatch.setattr(
        release_script,
        "published_releases",
        lambda: [{"id": 7, "tag_name": manifest.tag, "draft": True, "body": marker, "assets": []}],
    )
    monkeypatch.setattr(release_script.subprocess, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        release_script,
        "gh_api",
        lambda *_args, **_kwargs: {"id": 7, "draft": True, "assets": []},
    )

    with pytest.raises(ValueError, match="inventory"):
        release_script.publish(tmp_path, manifest.tag, manifest.commit_sha)


def test_publisher_rejects_uploaded_checksum_divergence(
    monkeypatch, tmp_path, publication_credits
) -> None:
    manifest = publish_manifest()
    marker = f"<!-- source-commit: {manifest.commit_sha} -->"
    expected = {manifest.assets[0].filename, "release-manifest.json", "SHA256SUMS"}
    (tmp_path / "release-notes.md").write_text("Changes", encoding="utf-8")
    for name in expected:
        (tmp_path / name).write_bytes(b"x")
    draft = {
        "id": 7,
        "tag_name": manifest.tag,
        "draft": True,
        "body": marker,
        "assets": [],
    }
    uploaded = {
        "id": 7,
        "draft": True,
        "assets": [
            {"name": name, "state": "uploaded", "size": 1, "digest": "sha256:" + "0" * 64}
            for name in expected
        ],
    }
    monkeypatch.setattr(release_script, "create_manifest", lambda *_args, **_kwargs: manifest)
    monkeypatch.setattr(release_script, "published_releases", lambda: [draft])
    monkeypatch.setattr(release_script.subprocess, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(release_script, "gh_api", lambda *_args, **_kwargs: uploaded)

    with pytest.raises(ValueError, match="checksum mismatch"):
        release_script.publish(tmp_path, manifest.tag, manifest.commit_sha)


def test_publisher_preserves_credits_between_draft_and_final_publication(
    monkeypatch,
    tmp_path,
    publication_credits,
) -> None:
    manifest = publish_manifest()
    marker = f"<!-- source-commit: {manifest.commit_sha} -->"
    (tmp_path / "release-notes.md").write_text("Reviewed changes\n", encoding="utf-8")
    names = {manifest.assets[0].filename, "release-manifest.json", "SHA256SUMS"}
    for name in names:
        (tmp_path / name).write_bytes(b"x")
    uploaded = {
        "id": 7,
        "draft": True,
        "assets": [
            {
                "name": name,
                "state": "uploaded",
                "size": 1,
                "digest": "sha256:" + release_script.digest_file(tmp_path / name),
            }
            for name in names
        ],
    }
    mutations = []

    def api(endpoint, *, method="GET", payload=None):
        if method != "GET":
            mutations.append((method, payload))
        return uploaded

    monkeypatch.setattr(release_script, "create_manifest", lambda *_args, **_kwargs: manifest)
    monkeypatch.setattr(release_script, "published_releases", lambda: [])
    monkeypatch.setattr(release_script.subprocess, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(release_script, "gh_api", api)
    release_script.publish(tmp_path, manifest.tag, manifest.commit_sha)

    assert [method for method, _payload in mutations] == ["POST", "PATCH"]
    draft, final = (payload for _method, payload in mutations)
    assert draft["draft"] is True and final["draft"] is False
    assert draft["body"] == final["body"]
    assert draft["body"].startswith("Reviewed changes\n")
    assert draft["body"].count(marker) == 1
    assert publication_credits.markdown() in draft["body"]
    assert "### Related pull requests and issues" in final["body"]
    assert "by @contributor" in final["body"]
    assert (
        f"Related issue: [#3](https://github.com/{release_script.RELEASE_REPOSITORY}/issues/3)"
        in final["body"]
    )
    assert (tmp_path / "release-notes.md").read_text() == "Reviewed changes\n"


def test_publisher_does_not_create_a_draft_when_related_metadata_collection_fails(
    monkeypatch,
    tmp_path,
) -> None:
    manifest = publish_manifest()
    (tmp_path / "release-notes.md").write_text("Reviewed changes\n", encoding="utf-8")
    monkeypatch.setattr(release_script, "create_manifest", lambda *_args, **_kwargs: manifest)
    monkeypatch.setattr(release_script, "verify_identity", lambda *_args: manifest.version)
    monkeypatch.setattr(release_script, "published_releases", lambda: [])
    calls = []

    def fail_collection(*_args, **_kwargs):
        raise ValueError("GitHub related pull request identity mismatch")

    monkeypatch.setattr(release_script, "release_credits", fail_collection)
    monkeypatch.setattr(
        release_script, "gh_api", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    monkeypatch.setattr(
        release_script.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs))
    )

    with pytest.raises(ValueError, match="pull request identity mismatch"):
        release_script.publish(tmp_path, manifest.tag, manifest.commit_sha)
    assert calls == []
    assert (tmp_path / "release-notes.md").read_text(encoding="utf-8") == "Reviewed changes\n"
