import hashlib
import json
from threading import Event
from types import SimpleNamespace

import pytest

from solin.core.network.http import HttpError, HttpHeaders, HttpStatusError
from solin.core.releases.channel import UpdateChannel
from solin.core.releases.manifest import REQUIRED_ASSETS, ReleaseManifest, asset_filename
from solin.core.releases.version import ReleaseVersion
from solin.core.remote import github_updates, update_download
from solin.core.remote.github_updates import GitHubReleases
from solin.core.remote.update_policy import UpdateAction, UpdateInfo, evaluate_update


class Response:
    def __init__(self, content=b"", status=200, headers=None):
        self.content = content
        self.status_code = status
        self.headers = HttpHeaders(headers)
        self.content_length = len(content)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_bytes(self):
        yield self.content


def payload(value="26.32.0") -> dict:
    version = ReleaseVersion.parse(value)
    assert version is not None
    return dict(
        schema_version=1,
        version=version.canonical,
        tag=version.tag,
        channel=version.channel,
        commit_sha="a" * 40,
        assets=[
            dict(
                platform=p,
                architecture=a,
                kind=k,
                filename=asset_filename(version, p, a, k),
                size=3,
                sha256=hashlib.sha256(b"abc").hexdigest(),
                minimum_os=minimum,
            )
            for (p, a, k), (_, minimum) in REQUIRED_ASSETS.items()
        ],
        notes=[dict(version=version.canonical, translations={"en": "Changes"})],
    )


def published(manifest) -> dict:
    return dict(
        draft=False,
        prerelease=manifest["channel"] == "beta",
        tag_name=manifest["tag"],
        assets=[dict(name=a["filename"], size=a["size"]) for a in manifest["assets"]]
        + [dict(name="release-manifest.json", size=100)],
    )


def test_discovery_validates_and_reuses_conditional_cache(monkeypatch, tmp_path):
    manifest = payload()
    release = published(manifest)
    requests = []

    def get(url, **kwargs):
        requests.append((url, kwargs))
        data = [release] if "api.github" in url else manifest
        return Response(json.dumps(data).encode(), headers={"ETag": "abc"})

    monkeypatch.setattr(github_updates, "stream_get", get)
    assert len(GitHubReleases(tmp_path).discover("26.31.0", UpdateChannel.STABLE)) == 1

    def cached(url, **kwargs):
        assert "api.github" in url  # Validated immutable manifests need no HTTP request.
        assert kwargs["headers"]["If-None-Match"] == "abc"
        return Response(status=304)

    monkeypatch.setattr(github_updates, "stream_get", cached)
    assert len(GitHubReleases(tmp_path).discover("26.31.0", UpdateChannel.STABLE)) == 1
    assert all(
        "id=" not in url and "Authorization" not in args["headers"] for url, args in requests
    )


@pytest.mark.parametrize(
    "current,channel,expected",
    [
        ("26.33.0b1", UpdateChannel.STABLE, ["26.32.0"]),
        ("26.33.0b1", UpdateChannel.BETA, ["26.33.0b10"]),
        ("26.32.0", UpdateChannel.STABLE, []),
        ("26.32.0", UpdateChannel.BETA, ["26.33.0b10", "26.33.0b1"]),
    ],
)
def test_discovery_uses_common_candidate_policy(monkeypatch, tmp_path, current, channel, expected):
    manifests = [payload(v) for v in ("26.31.0", "26.33.0b1", "26.32.0", "26.33.0b10")]
    by_tag = {m["tag"]: m for m in manifests}
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        data = [published(m) for m in manifests] if "api.github" in url else by_tag[url.split("/")[-2]]
        return Response(json.dumps(data).encode())

    monkeypatch.setattr(github_updates, "stream_get", get)
    result = GitHubReleases(tmp_path).discover(current, channel)
    assert [m.version.canonical for m in result] == expected
    assert len(calls) == 1 + len(expected)


@pytest.mark.parametrize("current,expected", [
    ("26.33.0b1", ["26.32.5"]),
    ("26.32.3b1", ["26.32.5", "26.32.4", "26.32.3"]),
])
def test_discovery_stops_before_historical_manifests_can_exhaust_deadline(
    monkeypatch, tmp_path, current, expected,
):
    manifests = [payload(f"26.32.{patch}") for patch in (0, 4, 1, 5, 2, 3)]
    by_tag = {manifest["tag"]: manifest for manifest in manifests}
    older_notes = payload("26.31.0")["notes"][0]
    for manifest in manifests:
        manifest["notes"].insert(0, older_notes)
    now, requested = [0.0], []
    monkeypatch.setattr(
        github_updates, "time", SimpleNamespace(monotonic=lambda: now[0], time=lambda: 0),
    )

    def get(url, **kwargs):
        if "api.github" in url:
            now[0] += 0.1
            data = [published(manifest) for manifest in manifests]
        else:
            tag = url.split("/")[-2]
            requested.append(tag)
            now[0] += 4
            data = by_tag[tag]
        return Response(json.dumps(data).encode())

    monkeypatch.setattr(github_updates, "stream_get", get)
    releases = GitHubReleases(tmp_path).discover(current, UpdateChannel.STABLE)
    assert requested == expected
    assert [release.version.canonical for release in releases] == expected
    # A parsed manifest includes every supported package and the same OS floors;
    # the cutoff must retain the best target for every supported computer.
    for (platform, architecture, kind), (_, minimum_os) in REQUIRED_ASSETS.items():
        info = evaluate_update(
            releases, current_version=current, channel=UpdateChannel.STABLE,
            platform=platform, architecture=architecture, os_version=minimum_os,
        )
        assert info is not None
        assert info.version.canonical == expected[0]
        assert info.asset.kind == kind
        assert info.is_downgrade is (len(expected) == 1)
        assert [note.version.canonical for note in info.changelog] == expected


@pytest.mark.parametrize("invalid", ["schema", "os_floor", "inventory", "missing"])
def test_discovery_keeps_searching_until_a_return_manifest_passes_validation(
    monkeypatch, tmp_path, invalid,
):
    manifests = [payload(f"26.32.{patch}") for patch in (0, 4, 1, 5, 2, 3)]
    by_tag = {manifest["tag"]: manifest for manifest in manifests}
    releases = [published(manifest) for manifest in manifests]
    newest = by_tag["26.32.5"]
    if invalid == "schema":
        newest["schema_version"] = 99
    elif invalid == "os_floor":
        newest["assets"][0]["minimum_os"] = "11"
    elif invalid == "inventory":
        newest["assets"][0]["size"] += 1
    requested = []

    def get(url, **kwargs):
        if "api.github" in url:
            return Response(json.dumps(releases).encode())
        tag = url.split("/")[-2]
        requested.append(tag)
        if invalid == "missing" and tag == newest["tag"]:
            raise HttpStatusError(url, 404, "removed")
        return Response(json.dumps(by_tag[tag]).encode())

    monkeypatch.setattr(github_updates, "stream_get", get)
    result = GitHubReleases(tmp_path).discover("26.33.0b1", UpdateChannel.STABLE)
    assert requested == ["26.32.5", "26.32.4"]
    assert [manifest.version.canonical for manifest in result] == ["26.32.4"]


@pytest.mark.parametrize("failure", ["cancelled", "timed out"])
def test_discovery_does_not_mask_cancellation_or_timeout_in_required_manifests(
    monkeypatch, tmp_path, failure,
):
    manifests = [payload("26.32.0"), payload("26.34.0"), payload("26.35.0")]
    by_tag = {manifest["tag"]: manifest for manifest in manifests}
    cancelled, now, requested = Event(), [0.0], []
    monkeypatch.setattr(
        github_updates, "time", SimpleNamespace(monotonic=lambda: now[0], time=lambda: 0),
    )

    def get(url, **kwargs):
        if "api.github" in url:
            return Response(json.dumps([published(manifest) for manifest in manifests]).encode())
        tag = url.split("/")[-2]
        requested.append(tag)
        if tag == "26.34.0":
            if failure == "cancelled":
                cancelled.set()
            else:
                now[0] = 21
        return Response(json.dumps(by_tag[tag]).encode())

    monkeypatch.setattr(github_updates, "stream_get", get)
    with pytest.raises(HttpError, match=failure):
        GitHubReleases(tmp_path, cancelled).discover("26.33.0b1", UpdateChannel.STABLE)
    assert requested == ["26.35.0", "26.34.0"]


@pytest.mark.parametrize("invalid", ["draft", "prerelease", "tag", "missing_manifest", "inventory"])
def test_discovery_rejects_inconsistent_release_metadata(monkeypatch, tmp_path, invalid):
    manifest = payload()
    release = published(manifest)
    if invalid == "draft":
        release["draft"] = True
    elif invalid == "prerelease":
        release["prerelease"] = True
    elif invalid == "tag":
        release["tag_name"] = "v26.32.0"
    elif invalid == "missing_manifest":
        release["assets"].pop()
    else:
        release["assets"][0]["size"] += 1

    def get(url, **kwargs):
        return Response(json.dumps([release] if "api.github" in url else manifest).encode())

    monkeypatch.setattr(github_updates, "stream_get", get)
    assert GitHubReleases(tmp_path).discover("26.31.0", UpdateChannel.STABLE) == []


def test_rate_limit_is_persisted_without_retry(monkeypatch, tmp_path):
    def limited(url, **kwargs):
        raise HttpStatusError(url, 429, "limited")

    monkeypatch.setattr(github_updates, "stream_get", limited)
    with pytest.raises(HttpStatusError):
        GitHubReleases(tmp_path).discover("26.31.0", UpdateChannel.STABLE)
    monkeypatch.setattr(github_updates, "stream_get", lambda *a, **k: pytest.fail("must not retry"))
    assert GitHubReleases(tmp_path).discover("26.31.0", UpdateChannel.STABLE) == []


def test_discovery_paginates_even_if_first_page_contains_only_old_releases(monkeypatch, tmp_path):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        content = [{"draft": True}] * 100 if url.endswith("&page=1") else []
        return Response(json.dumps(content).encode())

    monkeypatch.setattr(github_updates, "stream_get", get)
    assert GitHubReleases(tmp_path).discover("26.31.0", UpdateChannel.STABLE) == []
    assert len(calls) == 2


@pytest.mark.parametrize(
    "content,cancelled,success",
    [(b"abc", False, True), (b"abd", False, False), (b"ab", False, False), (b"abc", True, False)],
)
def test_download_only_exposes_verified_package(monkeypatch, tmp_path, content, cancelled, success):
    manifest = ReleaseManifest.parse(payload())
    assert manifest is not None
    info = UpdateInfo(
        UpdateAction.INSTALL, manifest.version, manifest.assets[0], "https://github.com/file"
    )
    monkeypatch.setattr(update_download, "stream_get", lambda *a, **k: Response(content))
    stop = Event()
    if cancelled:
        stop.set()
    transfer = update_download._Transfer(info, tmp_path, stop)
    completed, failed = [], []
    transfer.succeeded.connect(completed.append)
    transfer.failed.connect(failed.append)
    transfer.run()
    assert bool(completed) is success
    assert bool(failed) is not success
    assert not list(tmp_path.glob("*.part"))
    if success:
        assert (tmp_path / info.asset.filename).read_bytes() == b"abc"
    else:
        assert not list(tmp_path.iterdir())
