"""Prepare, validate and publish an explicitly reviewed Solin release."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from solin.core.releases.manifest import (  # noqa: E402
    MANIFEST_FILENAME, MAX_MANIFEST_BYTES, RELEASE_REPOSITORY, REQUIRED_ASSETS,
    LocalizedReleaseNotes, ReleaseAsset, ReleaseManifest, asset_filename,
)
from solin.core.releases.version import ReleaseVersion  # noqa: E402

NOTES = ROOT / "docs" / "release-notes"


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True, encoding="utf-8").strip()


def current_version() -> ReleaseVersion:
    namespace: dict = {}
    exec((ROOT / "src/solin/version.py").read_text(encoding="utf-8"), namespace)
    version = ReleaseVersion.parse(namespace["VERSION"])
    if version is None:
        raise ValueError("Invalid canonical VERSION")
    _ = version.windows_version
    return version


def gh_api(endpoint: str, *, method: str = "GET", payload: dict | None = None) -> object:
    command = ["gh", "api", endpoint, "--method", method,
               "-H", "Accept: application/vnd.github+json"]
    if payload is not None:
        command += ["--input", "-"]
    result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                            capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(result.stdout) if result.stdout.strip() else None


def published_releases() -> list[dict]:
    releases: list[dict] = []
    page = 1
    while True:
        batch = gh_api(f"repos/{RELEASE_REPOSITORY}/releases?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise ValueError("Invalid GitHub release listing")
        releases.extend(batch)
        if len(batch) < 100:
            return releases
        page += 1


def emit(values: dict[str, str], *, output: bool = True, environment: bool = False) -> None:
    text = "".join(f"{key}={value}\n" for key, value in values.items())
    if any("\n" in value or "\r" in value for value in values.values()):
        raise ValueError("Workflow values must fit on one line")
    for variable, enabled in (("GITHUB_OUTPUT", output), ("GITHUB_ENV", environment)):
        if enabled and os.environ.get(variable):
            with Path(os.environ[variable]).open("a", encoding="utf-8") as stream:
                stream.write(text)
    print(text, end="")


def metadata() -> None:
    version = current_version()
    emit({"app_version": version.display_version, "canonical_version": version.canonical,
          "windows_version": version.windows_version, "macos_version": version.macos_version,
          "macos_build": version.macos_build, "tag": version.tag})
    emit({"APP_VERSION": version.display_version, "WINDOWS_VERSION": version.windows_version,
          "MACOS_VERSION": version.macos_version, "MACOS_BUILD": version.macos_build},
         output=False, environment=True)


def load_note_directory(directory: Path) -> LocalizedReleaseNotes:
    version = ReleaseVersion.from_tag(directory.name)
    if not directory.is_dir() or version is None:
        raise ValueError(f"Invalid release-note directory: {directory.name}")
    translations = {
        path.stem: path.read_text(encoding="utf-8").strip() for path in directory.glob("*.md")
    }
    if not translations.get("en") or "<!-- TODO" in translations["en"]:
        raise ValueError(f"Reviewed English notes required for {directory.name}")
    locales = ROOT / "src/solin/resources/translations/locales"
    if any(not (locales / f"{locale}.json").exists() for locale in translations):
        raise ValueError(f"Unknown note locale in {directory.name}")
    return LocalizedReleaseNotes(version, translations)


def load_notes(target: ReleaseVersion) -> tuple[LocalizedReleaseNotes, ...]:
    directory = NOTES / target.tag
    if not directory.is_dir():
        raise ValueError(f"Missing release notes for {target.tag}")
    return (load_note_directory(directory),)


def validate_note_tree() -> None:
    if not NOTES.exists():
        return
    for directory in sorted(NOTES.iterdir()):
        if directory.name != "README.md":
            load_note_directory(directory)


def notes_markdown(notes: tuple[LocalizedReleaseNotes, ...]) -> str:
    return "\n\n".join(f"## {note.version.display_version}\n\n{note.translations['en']}" for note in notes) + "\n"


def calculate_next(current: ReleaseVersion, intent: str, year: int) -> ReleaseVersion:
    yy, line, patch = current.base
    if intent == "release":
        if current.is_beta:
            raise ValueError("Promote or continue the current beta before starting a release line")
        yy, line, patch = (year, line + 1, 0) if yy == year else (year, 1, 0)
        suffix = ""
    elif intent == "patch":
        if current.is_beta:
            raise ValueError("Promote or continue the current beta before preparing a patch")
        patch += 1
        suffix = ""
    elif intent == "beta":
        if current.is_beta:
            suffix = f"b{current.value.pre[1] + 1}"
        else:
            yy, line, patch = (year, line + 1, 0) if yy == year else (year, 1, 0)
            suffix = "b1"
    elif intent == "promote":
        if not current.is_beta:
            raise ValueError("Only a beta can be promoted")
        suffix = ""
    else:
        raise ValueError("Unknown release intent")
    result = ReleaseVersion.parse(f"{yy}.{line}.{patch}{suffix}")
    if result is None or result <= current:
        raise ValueError("Release version must increase and fit native version limits")
    return result


def prepare(intent: str) -> None:
    if git("status", "--porcelain"):
        raise ValueError("Prepare releases from a clean checkout")
    current = current_version()
    version = calculate_next(current, intent, date.today().year % 100)
    # Read remote tags, not just a possibly stale local checkout.
    tags = set(git("tag", "--list").splitlines())
    tags.update(
        line.split("refs/tags/", 1)[1].removesuffix("^{}")
        for line in git("ls-remote", "--tags", "origin").splitlines()
        if "refs/tags/" in line
    )
    if version.tag in tags or f"v{version.tag}" in tags:
        raise ValueError("That release tag already exists")
    destination = NOTES / version.tag
    if destination.exists():
        raise ValueError("Release notes already exist; refusing to overwrite them")
    review_marker = "<!-- TODO: Review user-facing release notes before tagging. -->\n\n"
    english = review_marker + "### Changes\n\n"
    if intent == "promote":
        beta_notes = [
            load_note_directory(path)
            for path in NOTES.iterdir()
            if path.is_dir()
            and (candidate := ReleaseVersion.from_tag(path.name)) is not None
            and candidate.is_beta
            and candidate.base == current.base
        ]
        beta_notes.sort(key=lambda note: note.version)
        english = review_marker + "\n\n".join(
            f"### Changes tested in {note.version.display_version}\n\n{note.translations['en']}"
            for note in beta_notes
        )
    version_path = ROOT / "src/solin/version.py"
    version_temporary = version_path.with_suffix(".py.tmp")
    try:
        destination.mkdir(parents=True)
        (destination / "en.md").write_text(english, encoding="utf-8")
        version_temporary.write_text(
            '"""Canonical CalVer (YY.RELEASE.PATCH, optionally followed by bN)."""\n\n'
            f'VERSION = "{version.canonical}"\n__version__ = VERSION\n', encoding="utf-8")
        version_temporary.replace(version_path)
    except OSError:
        # A failed preparation must not leave a half-created release-note tree.
        version_temporary.unlink(missing_ok=True)
        try:
            (destination / "en.md").unlink(missing_ok=True)
            destination.rmdir()
        except OSError:
            pass
        raise
    print(f"Prepared {version.tag}. Review notes and open a release preparation PR; no tag was created.")


def verify_identity(tag: str, sha: str) -> ReleaseVersion:
    version = current_version()
    if version.tag != tag or not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("Tag/SHA does not match the source release identity")
    if git("rev-parse", "HEAD") != sha or git("rev-parse", f"refs/tags/{tag}^{{commit}}") != sha:
        raise ValueError("Checkout and tag must identify the exact release commit")
    subprocess.run(["git", "merge-base", "--is-ancestor", sha, "origin/main"], cwd=ROOT, check=True)
    return version


def preflight(tag: str, sha: str) -> None:
    version = verify_identity(tag, sha)
    load_notes(version)
    repository = gh_api(f"repos/{RELEASE_REPOSITORY}")
    if not isinstance(repository, dict) or repository.get("private") is not False:
        raise ValueError("Distribution requires the public repository; diagnostic builds remain available")
    releases = published_releases()
    for release in releases:
        other = ReleaseVersion.from_tag(str(release.get("tag_name", "")))
        if release.get("draft") is False and (other == version or (
            version.is_beta and other is not None and other.base == version.base and not other.is_beta
        )):
            raise ValueError("Version is already published or its stable release already exists")
    for platform, architecture in (("windows", "x86_64"), ("macos", "x86_64"), ("linux", "x86_64"), ("macos", "arm64")):
        resolve_predecessor(platform, architecture, version, releases)
    emit({"tag": tag, "sha": sha, "app_version": version.display_version})


def digest_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def create_manifest(directory: Path, tag: str, sha: str) -> ReleaseManifest:
    version = verify_identity(tag, sha)
    assets = []
    for (platform, architecture, kind), (_, minimum_os) in REQUIRED_ASSETS.items():
        filename = asset_filename(version, platform, architecture, kind)
        path = directory / filename
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Missing distribution asset: {filename}")
        assets.append(ReleaseAsset(platform, architecture, kind, filename,
                                   path.stat().st_size, digest_file(path), minimum_os))
    manifest = ReleaseManifest(version, tag, version.channel, sha, tuple(assets), load_notes(version))
    if ReleaseManifest.parse(manifest.to_dict()) is None:
        raise ValueError("Generated manifest does not satisfy the distribution contract")
    manifest_path = directory / MANIFEST_FILENAME
    manifest_json = json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2) + "\n"
    if len(manifest_json.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise ValueError("Release manifest exceeds the updater's size limit")
    manifest_path.write_text(manifest_json, encoding="utf-8")
    sums = [f"{asset.sha256}  {asset.filename}" for asset in assets]
    sums.append(f"{digest_file(manifest_path)}  {MANIFEST_FILENAME}")
    (directory / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
    (directory / "release-notes.md").write_text(notes_markdown((manifest.notes[0],)), encoding="utf-8")
    return manifest


def publish(directory: Path, tag: str, sha: str) -> None:
    manifest = create_manifest(directory, tag, sha)
    releases = published_releases()
    existing = next((r for r in releases if r.get("tag_name") == tag), None)
    if existing is not None and not existing.get("draft"):
        raise ValueError("Published releases are immutable; create a new version")
    body = (directory / "release-notes.md").read_text(encoding="utf-8")
    marker = f"<!-- source-commit: {sha} -->"
    if existing is not None and marker not in str(existing.get("body", "")):
        raise ValueError("Existing draft was not created for this source SHA")
    if existing is None:
        existing = gh_api(f"repos/{RELEASE_REPOSITORY}/releases", method="POST", payload={
            "tag_name": tag, "target_commitish": sha, "name": tag,
            "body": body + "\n" + marker, "draft": True,
            "prerelease": manifest.version.is_beta, "make_latest": "false",
        })
    assert isinstance(existing, dict)
    expected = {asset.filename for asset in manifest.assets} | {MANIFEST_FILENAME, "SHA256SUMS"}
    if any(asset["name"] not in expected for asset in existing.get("assets", [])):
        raise ValueError("Draft contains unexpected files")
    # Replacing files is permitted only while this owned release is still a draft.
    for name in sorted(expected):
        subprocess.run(["gh", "release", "upload", tag, str(directory / name), "--clobber",
                        "--repo", RELEASE_REPOSITORY], check=True)
    uploaded = gh_api(f"repos/{RELEASE_REPOSITORY}/releases/{existing['id']}")
    if not isinstance(uploaded, dict) or uploaded.get("draft") is not True:
        raise ValueError("Release is no longer a draft")
    actual = {asset["name"]: asset for asset in uploaded.get("assets", [])}
    if set(actual) != expected:
        raise ValueError("Uploaded release inventory differs from the validated package set")
    for name in expected:
        asset = actual[name]
        if asset.get("state") != "uploaded" or asset.get("size") != (directory / name).stat().st_size:
            raise ValueError(f"Incomplete upload: {name}")
        if asset.get("digest") != "sha256:" + digest_file(directory / name):
            raise ValueError(f"Uploaded checksum mismatch: {name}")
    stable = [ReleaseVersion.from_tag(str(r.get("tag_name", ""))) for r in published_releases()
              if r.get("draft") is False and r.get("prerelease") is False]
    latest = not manifest.version.is_beta and all(manifest.version > v for v in stable if v)
    gh_api(f"repos/{RELEASE_REPOSITORY}/releases/{existing['id']}", method="PATCH", payload={
        "draft": False, "prerelease": manifest.version.is_beta,
        "make_latest": "true" if latest else "false", "body": body + "\n" + marker,
    })


def read_remote_manifest(tag: str) -> ReleaseManifest:
    import requests

    url = f"https://github.com/{RELEASE_REPOSITORY}/releases/download/{tag}/{MANIFEST_FILENAME}"
    with requests.get(url, timeout=20, stream=True) as response:
        response.raise_for_status()
        content = bytearray()
        for chunk in response.iter_content(65536):
            content.extend(chunk)
            if len(content) > MAX_MANIFEST_BYTES:
                raise ValueError("Oversized predecessor manifest")
    manifest = ReleaseManifest.parse(json.loads(content))
    if manifest is None or manifest.tag != tag:
        raise ValueError("Invalid predecessor manifest")
    return manifest


def resolve_predecessor(platform: str, architecture: str, version: ReleaseVersion,
                        releases: list[dict]) -> tuple[str, str] | None:
    candidates: list[tuple[ReleaseVersion, dict]] = []
    for release in releases:
        other = ReleaseVersion.from_tag(str(release.get("tag_name", "")))
        if release.get("draft") is False and other is not None and other < version:
            if version.is_beta or not other.is_beta:
                candidates.append((other, release))
    for other, release in sorted(candidates, key=lambda item: item[0], reverse=True):
        assets = release.get("assets")
        if not isinstance(assets, list) or not any(
            isinstance(asset, dict) and asset.get("name") == MANIFEST_FILENAME
            for asset in assets
        ):
            # Releases created before centralization have no manifest. The
            # first centralized release uses the pinned transition package.
            continue
        manifest = read_remote_manifest(other.tag)
        kind = {"windows": "installer", "macos": "dmg", "linux": "appimage"}[platform]
        asset = next((a for a in manifest.assets if (a.platform, a.architecture, a.kind) ==
                      (platform, architecture, kind)), None)
        if asset:
            return asset.download_url(other.tag), asset.sha256
    if platform == "macos" and architecture == "arm64":
        return None
    prefix = f"SOLIN_TRANSITION_{platform.upper()}"
    url, digest = os.environ.get(prefix + "_URL", ""), os.environ.get(prefix + "_SHA256", "")
    if not url.startswith("https://") or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError(f"First release requires verified predecessor: {prefix}_URL and {prefix}_SHA256")
    return url, digest


def download_predecessor(platform: str, architecture: str, output: Path, env_key: str) -> None:
    import requests

    source = resolve_predecessor(platform, architecture, current_version(), published_releases())
    emit({"has_predecessor": "true" if source else "false"})
    if source is None:
        return
    url, expected = source
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(output.suffix + ".part")
    try:
        total = 0
        with requests.get(url, stream=True, timeout=(20, 60)) as response, partial.open("wb") as stream:
            response.raise_for_status()
            for chunk in response.iter_content(1024 * 1024):
                total += len(chunk)
                if total > 2 * 1024**3:
                    raise ValueError("Predecessor exceeds package size limit")
                stream.write(chunk)
        if digest_file(partial) != expected:
            raise ValueError("Predecessor checksum mismatch")
        partial.replace(output)
    finally:
        partial.unlink(missing_ok=True)
    emit({env_key: str(output.resolve())}, output=False, environment=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("intent", choices=("release", "patch", "beta", "promote"))
    meta = commands.add_parser("metadata")
    meta.add_argument("--github-output", action="store_true")
    meta.add_argument("--github-env", action="store_true")
    commands.add_parser("check-notes")
    for name in ("preflight", "manifest", "publish"):
        command = commands.add_parser(name)
        command.add_argument("--tag", required=True)
        command.add_argument("--sha", required=True)
        command.add_argument("--github-output", action="store_true")
        if name != "preflight":
            command.add_argument("--artifacts", type=Path, required=True)
    predecessor = commands.add_parser("download-predecessor")
    predecessor.add_argument("--platform", choices=("windows", "macos", "linux"), required=True)
    predecessor.add_argument("--architecture", choices=("x86_64", "arm64"), required=True)
    predecessor.add_argument("--output", type=Path, required=True)
    predecessor.add_argument("--github-env", required=True)
    args = parser.parse_args()
    if args.command == "metadata":
        metadata()
    elif args.command == "prepare":
        prepare(args.intent)
    elif args.command == "check-notes":
        validate_note_tree()
    elif args.command == "preflight":
        preflight(args.tag, args.sha)
    elif args.command == "manifest":
        create_manifest(args.artifacts, args.tag, args.sha)
    elif args.command == "publish":
        publish(args.artifacts, args.tag, args.sha)
    else:
        download_predecessor(args.platform, args.architecture, args.output, args.github_env)


if __name__ == "__main__":
    main()
