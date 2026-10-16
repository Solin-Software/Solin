"""Beta note generation and the strict stable preparation contract."""

from dataclasses import replace
import json

import pytest

from scripts import release
from scripts.release_contributors import Change, ContributionRange, Credits, RelatedIssue
from solin.core.releases.channel import UpdateChannel
from solin.core.releases.manifest import MANIFEST_FILENAME, REQUIRED_ASSETS, ReleaseManifest, asset_filename
from solin.core.releases.version import ReleaseVersion
from solin.core.remote.update_policy import evaluate_update

HEAD = "a" * 40
BASE = "b" * 40


def version(value):
    result = ReleaseVersion.parse(value)
    assert result is not None
    return result


@pytest.fixture
def note_tree(tmp_path, monkeypatch):
    source = tmp_path / "src/solin"
    source.mkdir(parents=True)
    (source / "version.py").write_text('VERSION = "26.32.0b5"\n', encoding="utf-8")
    locales = source / "resources/translations/locales"
    locales.mkdir(parents=True)
    for locale in ("en", "pt_BR"):
        (locales / f"{locale}.json").write_text("{}", encoding="utf-8")
    root = tmp_path / "docs/release-notes"
    root.mkdir(parents=True)
    monkeypatch.setattr(release, "ROOT", tmp_path)
    monkeypatch.setattr(release, "NOTES", root)
    monkeypatch.setattr(release, "git", lambda *_args: "")
    return root


def write_notes(root, target, translations):
    directory = root / target.tag
    directory.mkdir()
    for locale, content in translations.items():
        (directory / f"{locale}.md").write_text(content, encoding="utf-8")
    return directory


@pytest.mark.parametrize(("value", "translations", "accepted"), [
    ("26.32.0b6", {"en": ""}, True),
    ("26.32.0b6", {"en": " \n\t"}, True),
    ("26.32.0b6", {"en": "Reviewed changes"}, True),
    ("26.32.0b6", {"pt_BR": "Mudanças"}, False),
    ("26.32.0b6", {"en": "<!-- TODO: Review notes. -->"}, False),
    ("26.32.0", {"en": ""}, False),
    ("26.32.0", {"en": " \n\t"}, False),
    ("26.32.0", {"en": "<!-- TODO: Review notes. -->"}, False),
    ("26.32.0", {"en": "Reviewed changes"}, True),
    ("26.32.0b6", {"en": "", "unknown": ""}, False),
])
def test_note_validation_distinguishes_explicit_automatic_beta_from_incomplete_preparation(
    note_tree, value, translations, accepted,
):
    target = version(value)
    write_notes(note_tree, target, translations)
    if accepted:
        assert release.load_notes(target)[0].translations["en"] == translations["en"].strip()
        release.validate_note_tree()
    else:
        with pytest.raises(ValueError):
            release.load_notes(target)
        with pytest.raises(ValueError):
            release.validate_note_tree()


def test_beta_preparation_needs_no_editorial_step(note_tree):
    release.prepare("beta")
    target = version("26.32.0b6")
    assert release.current_version() == target
    assert (note_tree / target.tag / "en.md").read_text(encoding="utf-8") == ""
    assert release.load_notes(target)[0].translations == {"en": ""}


@pytest.mark.parametrize("reviewed_beta", [False, True])
def test_promotion_skips_automatic_beta_notes_and_keeps_stable_review_required(note_tree, reviewed_beta):
    write_notes(note_tree, version("26.32.0b1"), {"en": ""})
    write_notes(note_tree, version("26.32.0b2"), {"en": "Changes for testers" if reviewed_beta else ""})
    release.prepare("promote")
    target = version("26.32.0")
    draft = (note_tree / target.tag / "en.md").read_text(encoding="utf-8")
    assert "<!-- TODO" in draft
    assert "Changes tested in 26.32.0-beta.1" not in draft
    assert ("Changes for testers" in draft) is reviewed_beta
    assert ("### Changes\n" in draft) is not reviewed_beta
    with pytest.raises(ValueError, match="Reviewed English"):
        release.load_notes(target)


@pytest.fixture
def generated_release(note_tree, tmp_path, monkeypatch):
    target = version("26.32.0b6")
    note_directory = write_notes(note_tree, target, {"en": "", "pt_BR": " \n"})
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    for platform, architecture, kind in REQUIRED_ASSETS:
        (artifacts / asset_filename(target, platform, architecture, kind)).write_bytes(b"package")
    monkeypatch.setattr(release, "verify_identity", lambda *_args: target)
    repository = f"https://github.com/{release.RELEASE_REPOSITORY}"
    credits = Credits(ContributionRange(BASE, HEAD, (HEAD,)), (
        Change("fix(player): restore playback", "PR #7", f"{repository}/pull/7", ("alice",),
               (RelatedIssue("#3", f"{repository}/issues/3"),), ("bug", "documentation")),
        Change("chore(release): prepare version", "PR #8", f"{repository}/pull/8", ("alice",),
               (), ("skip-changelog",)),
    ), ())
    return target, artifacts, note_directory, credits


def test_generated_beta_manifest_reaches_existing_updater_without_changing_source_notes(generated_release):
    target, artifacts, note_directory, credits = generated_release
    manifest = release.create_manifest(artifacts, target.tag, HEAD, credits=credits)
    parsed = ReleaseManifest.parse(json.loads((artifacts / MANIFEST_FILENAME).read_text(encoding="utf-8")))
    assert parsed == manifest
    markdown = manifest.notes[0].translations["en"]
    assert markdown == credits.content_markdown().strip()
    assert "#### Bug fixes" in markdown and "Related issue: [#3]" in markdown
    assert "PR #8" not in markdown and "contribution-range" not in markdown
    assert "pt_BR" not in manifest.notes[0].translations
    selected = evaluate_update(
        [parsed], current_version="26.32.0b5", channel=UpdateChannel.BETA,
        platform="windows", architecture="x86_64", os_version="10.0.22631", language="pt_BR",
    )
    assert selected is not None
    assert selected.changelog[0].markdown == markdown
    assert selected.changelog[0].language == "en"
    assert (note_directory / "en.md").read_text(encoding="utf-8") == ""
    assert "Related pull requests" not in (artifacts / "release-notes.md").read_text(encoding="utf-8")
    sums = (artifacts / "SHA256SUMS").read_text(encoding="utf-8")
    assert f"{release.digest_file(artifacts / MANIFEST_FILENAME)}  {MANIFEST_FILENAME}" in sums


@pytest.mark.parametrize("english", ["", "Reviewed beta changes"])
def test_reviewed_localized_notes_take_precedence(generated_release, monkeypatch, english):
    target, artifacts, note_directory, credits = generated_release
    (note_directory / "en.md").write_text(english, encoding="utf-8")
    (note_directory / "pt_BR.md").write_text("Mudanças revisadas", encoding="utf-8")
    monkeypatch.setattr(release, "published_releases", lambda: pytest.fail("Unexpected GitHub request"))
    manifest = release.create_manifest(artifacts, target.tag, HEAD, credits=credits)
    assert manifest.notes[0].translations == {
        "en": english or credits.content_markdown().strip(), "pt_BR": "Mudanças revisadas",
    }


def test_all_excluded_beta_entries_provide_a_link_to_the_verified_interval(generated_release):
    target, artifacts, _directory, credits = generated_release
    credits = replace(credits, changes=(credits.changes[1],))
    manifest = release.create_manifest(artifacts, target.tag, HEAD, credits=credits)
    assert manifest.notes[0].translations == {
        "en": f"[Full changelog](https://github.com/{release.RELEASE_REPOSITORY}/compare/{BASE}...{HEAD})",
    }
    assert "PR #8" not in manifest.notes[0].translations["en"]


def test_standalone_manifest_collects_automatic_notes_once_and_propagates_failures(
    generated_release, monkeypatch,
):
    target, artifacts, _directory, credits = generated_release
    monkeypatch.setattr(release, "published_releases", lambda: [])
    calls = []

    def collect(*args):
        calls.append(args)
        return credits

    monkeypatch.setattr(release, "release_credits", collect)
    assert release.create_manifest(artifacts, target.tag, HEAD).notes[0].translations["en"]
    assert calls == [(target, HEAD, [])]
    (artifacts / MANIFEST_FILENAME).unlink()

    def unavailable(*args):
        raise ValueError("GitHub unavailable")

    monkeypatch.setattr(release, "release_credits", unavailable)
    with pytest.raises(ValueError, match="GitHub unavailable"):
        release.create_manifest(artifacts, target.tag, HEAD)
    assert not (artifacts / MANIFEST_FILENAME).exists()


@pytest.mark.parametrize("retry", [False, True])
def test_publication_uses_one_snapshot_for_manifest_and_github_without_duplicate_entries(
    generated_release, monkeypatch, retry,
):
    target, artifacts, _directory, credits = generated_release
    original_body = f"<!-- source-commit: {HEAD} -->\n" + credits.markdown()
    releases = [{"id": 7, "tag_name": target.tag, "draft": True, "body": original_body,
                 "assets": []}] if retry else []
    monkeypatch.setattr(release, "published_releases", lambda: releases)
    collections = []
    writes = []

    def collect(*args, **kwargs):
        collections.append((args, kwargs))
        return credits

    def api(endpoint, *, method="GET", payload=None):
        if method != "GET":
            writes.append((method, payload))
            return {"id": 7, "assets": []}
        return {"draft": True, "assets": [
            {"name": path.name, "state": "uploaded", "size": path.stat().st_size,
             "digest": "sha256:" + release.digest_file(path)}
            for path in artifacts.iterdir() if path.name != "release-notes.md"
        ]}

    monkeypatch.setattr(release, "release_credits", collect)
    monkeypatch.setattr(release, "gh_api", api)
    monkeypatch.setattr(release.subprocess, "run", lambda *_args, **_kwargs: None)
    release.publish(artifacts, target.tag, HEAD)
    assert collections == [((target, HEAD, releases), {"body": original_body if retry else ""})]
    assert [method for method, _ in writes] == (["PATCH"] if retry else ["POST", "PATCH"])
    body = writes[-1][1]["body"]
    assert body.count("### Related pull requests and issues") == 1
    assert body.count("[PR #7]") == 1
    assert "PR #8" not in body
    assert f"contribution-range: {BASE}..{HEAD}" in body
    assert writes[0][1]["body"] == body
    manifest = ReleaseManifest.parse(json.loads((artifacts / MANIFEST_FILENAME).read_text(encoding="utf-8")))
    assert manifest.notes[0].translations["en"] == credits.content_markdown().strip()


def test_oversized_automatic_notes_fail_before_remote_mutations(generated_release, monkeypatch):
    target, artifacts, _directory, credits = generated_release
    credits = replace(credits, changes=tuple(
        replace(credits.changes[0], title=f"fix: {'a' * 250} {number}") for number in range(700)
    ))
    monkeypatch.setattr(release, "published_releases", lambda: [])
    monkeypatch.setattr(release, "release_credits", lambda *_args, **_kwargs: credits)
    monkeypatch.setattr(release, "gh_api", lambda *_args, **_kwargs: pytest.fail("Unexpected mutation"))
    monkeypatch.setattr(release.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("Unexpected upload"))
    with pytest.raises(ValueError, match="distribution contract"):
        release.publish(artifacts, target.tag, HEAD)
    assert not (artifacts / MANIFEST_FILENAME).exists()


def test_reviewed_stable_notes_supersede_automatic_beta_notes(generated_release, note_tree, monkeypatch):
    beta, artifacts, _directory, credits = generated_release
    beta_manifest = release.create_manifest(artifacts, beta.tag, HEAD, credits=credits)
    stable = version("26.32.0")
    write_notes(note_tree, stable, {"en": "Reviewed stable summary"})
    for platform, architecture, kind in REQUIRED_ASSETS:
        (artifacts / asset_filename(stable, platform, architecture, kind)).write_bytes(b"stable package")
    monkeypatch.setattr(release, "verify_identity", lambda *_args: stable)
    stable_manifest = release.create_manifest(artifacts, stable.tag, HEAD)
    selected = evaluate_update(
        [beta_manifest, stable_manifest], current_version="26.32.0b5", channel=UpdateChannel.BETA,
        platform="windows", architecture="x86_64", os_version="10.0.22631",
    )
    assert selected is not None
    assert selected.version == stable
    assert [note.markdown for note in selected.changelog] == ["Reviewed stable summary"]
