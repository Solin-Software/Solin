import hashlib
import json
from threading import Event

import pytest

from solin.core.network.http import HttpHeaders, HttpStatusError
from solin.core.releases.channel import UpdateChannel
from solin.core.releases.manifest import REQUIRED_ASSETS, ReleaseManifest, asset_filename
from solin.core.releases.version import ReleaseVersion
from solin.core.remote import github_updates, update_download
from solin.core.remote.github_updates import GitHubReleases
from solin.core.remote.update_policy import UpdateAction, UpdateInfo


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


def payload():
    version = ReleaseVersion.parse("26.32.0")
    return dict(
        schema_version=1,
        version=version.canonical,
        tag=version.tag,
        channel="stable",
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


def test_discovery_validates_and_reuses_conditional_cache(monkeypatch, tmp_path):
    manifest = payload()
    release = dict(
        draft=False,
        prerelease=False,
        tag_name=manifest["tag"],
        assets=[dict(name=a["filename"], size=a["size"]) for a in manifest["assets"]]
        + [dict(name="release-manifest.json", size=100)],
    )
    requests = []

    def get(url, **kwargs):
        requests.append((url, kwargs))
        data = [release] if "api.github" in url else manifest
        return Response(json.dumps(data).encode(), headers={"ETag": "abc"})

    monkeypatch.setattr(github_updates, "stream_get", get)
    assert len(GitHubReleases(tmp_path).discover("26.31.0", UpdateChannel.STABLE)) == 1

    def cached(url, **kwargs):
        assert kwargs["headers"]["If-None-Match"] == "abc"
        return Response(status=304)

    monkeypatch.setattr(github_updates, "stream_get", cached)
    assert len(GitHubReleases(tmp_path).discover("26.31.0", UpdateChannel.STABLE)) == 1
    assert all(
        "id=" not in url and "Authorization" not in args["headers"] for url, args in requests
    )


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
