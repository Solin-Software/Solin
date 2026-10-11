"""Published release maintenance changes credits without republishing packages."""

from copy import deepcopy

import pytest

from scripts import release
from scripts.release_contributors import (
    END,
    Change,
    ContributionRange,
    Credits,
    HISTORICAL_BASE,
    RelatedIssue,
    update_body,
)

TAG = "26.32.0-beta.4"
HEAD = "a" * 40


@pytest.fixture
def maintenance(monkeypatch):
    repository_url = f"https://github.com/{release.RELEASE_REPOSITORY}"
    change = Change(
        "fix(player): Restore playback",
        "PR #7",
        f"{repository_url}/pull/7",
        ("alice", "bob"),
        (RelatedIssue("#3", f"{repository_url}/issues/3"),),
    )
    credits = Credits(ContributionRange(HISTORICAL_BASE, HEAD, (HEAD,)), (change,), ())
    original = {
        "id": 7,
        "tag_name": TAG,
        "draft": False,
        "prerelease": True,
        "body": f"Reviewed notes\r\n\r\n<!-- source-commit: {HEAD} -->\nCustom footer\n",
        "assets": [
            {
                "id": 1,
                "name": "package.exe",
                "size": 10,
                "digest": "sha256:" + "b" * 64,
                "state": "uploaded",
                "download_count": 1,
            }
        ],
    }
    calls = []
    looked_up = []
    current = deepcopy(original)
    response = {}

    def api(endpoint, *, method="GET", payload=None):
        calls.append((endpoint, method, payload))
        if method == "PATCH":
            assert isinstance(payload, dict)
            return response or {**deepcopy(current), **payload}
        return deepcopy(current if endpoint.endswith("/7") else original)

    def get_credits(version, sha, releases, *, body=""):
        looked_up.append((version.tag, sha, releases, body))
        return credits

    monkeypatch.setattr(release, "gh_api", api)
    monkeypatch.setattr(release, "published_releases", lambda: [deepcopy(original)])
    monkeypatch.setattr(release, "git", lambda *args: HEAD if args[0] == "rev-parse" else "")
    monkeypatch.setattr(release, "release_credits", get_credits)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return original, current, response, credits, calls, looked_up


def test_manual_preview_has_no_mutations_and_uses_the_fetched_notes(maintenance, capsys):
    original, _current, _response, credits, calls, looked_up = maintenance
    release.contributors(TAG)
    assert all(method == "GET" for _endpoint, method, _payload in calls)
    assert looked_up == [(TAG, HEAD, [original], original["body"])]
    assert "Preview:" in capsys.readouterr().out
    assert credits.logins == ("alice", "bob")


def test_manual_apply_patches_only_body_and_preserves_custom_notes(maintenance, capsys):
    original, _current, _response, credits, calls, _looked_up = maintenance
    release.contributors(TAG, apply=True)
    patches = [(endpoint, payload) for endpoint, method, payload in calls if method == "PATCH"]
    assert patches == [
        (
            f"repos/{release.RELEASE_REPOSITORY}/releases/7",
            {"body": update_body(original["body"], credits)},
        )
    ]
    assert patches[0][1]["body"].startswith(original["body"])
    assert "Updated:" in capsys.readouterr().out


def test_manual_apply_unchanged_is_a_no_op(maintenance, capsys):
    original, _current, _response, credits, calls, _looked_up = maintenance
    original["body"] = update_body(original["body"], credits)
    release.contributors(TAG, apply=True)
    assert all(method == "GET" for _endpoint, method, _payload in calls)
    assert "Unchanged:" in capsys.readouterr().out


def test_source_marker_inside_the_replaced_block_is_rejected_without_mutation(maintenance):
    original, _current, _response, credits, calls, _looked_up = maintenance
    original["body"] = "Reviewed notes\n" + credits.markdown().replace(
        END, f"<!-- source-commit: {HEAD} -->\n{END}"
    )
    with pytest.raises(ValueError, match="source marker"):
        release.contributors(TAG, apply=True)
    assert all(method == "GET" for _endpoint, method, _payload in calls)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item.update(body="Someone edited these notes"),
        lambda item: item.update(tag_name="26.32.0-beta.5"),
        lambda item: item.update(draft=True),
        lambda item: item.update(prerelease=False),
        lambda item: item.update(id=8),
        lambda item: item["assets"][0].update(digest="sha256:" + "c" * 64),
        lambda item: item["assets"].clear(),
    ],
)
def test_manual_apply_rejects_intervening_changes(maintenance, mutation):
    _original, current, _response, _credits, calls, _looked_up = maintenance
    mutation(current)
    with pytest.raises(ValueError, match="changed during"):
        release.contributors(TAG, apply=True)
    assert all(method == "GET" for _endpoint, method, _payload in calls)


def test_manual_apply_does_not_confuse_download_counts_with_an_asset_change(maintenance):
    _original, current, _response, _credits, calls, _looked_up = maintenance
    current["assets"][0]["download_count"] += 1
    release.contributors(TAG, apply=True)
    assert sum(method == "PATCH" for _endpoint, method, _payload in calls) == 1


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda item: item.update(draft=True), "published release"),
        (lambda item: item.update(prerelease=False), "published release"),
        (lambda item: item.update(id=True), "published release"),
        (lambda item: item.update(id=0), "published release"),
        (lambda item: item.update(tag_name="26.32.0-beta.5"), "published release"),
        (lambda item: item.update(body="Unowned release"), "source marker"),
        (lambda item: item.update(body=f"<!-- source-commit: {'b' * 40} -->"), "source marker"),
        (
            lambda item: item.update(body=item["body"] + f"<!-- source-commit: {HEAD} -->"),
            "source marker",
        ),
        (lambda item: item.update(assets=None), "asset inventory"),
        (lambda item: item["assets"][0].update(id=None), "asset identity"),
        (lambda item: item["assets"][0].pop("name"), "asset inventory"),
        (lambda item: item["assets"][0].update(name=None), "asset inventory"),
        (lambda item: item["assets"][0].update(name=""), "asset inventory"),
        (lambda item: item["assets"][0].pop("size"), "asset inventory"),
        (lambda item: item["assets"][0].update(size=True), "asset inventory"),
        (lambda item: item["assets"][0].update(size=0), "asset inventory"),
        (lambda item: item["assets"][0].update(size=-1), "asset inventory"),
        (lambda item: item["assets"][0].pop("digest"), "asset inventory"),
        (lambda item: item["assets"][0].update(digest="SHA256:invalid"), "asset inventory"),
        (lambda item: item["assets"][0].pop("state"), "asset inventory"),
        (lambda item: item["assets"][0].update(state="open"), "asset inventory"),
    ],
)
def test_manual_update_rejects_unowned_or_invalid_releases(maintenance, mutation, match):
    original, _current, _response, _credits, calls, looked_up = maintenance
    mutation(original)
    with pytest.raises(ValueError, match=match):
        release.contributors(TAG, apply=True)
    assert not looked_up
    assert all(method == "GET" for _endpoint, method, _payload in calls)


@pytest.mark.parametrize("tag", ["main", "v26.32.0-beta.4", "26.32.0b4", "$(whoami)", ""])
def test_invalid_tags_are_rejected_before_any_remote_request(maintenance, tag):
    _original, _current, _response, _credits, calls, _looked_up = maintenance
    with pytest.raises(ValueError, match="Invalid release tag"):
        release.contributors(tag, apply=True)
    assert not calls


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item.update(body="Update not saved"),
        lambda item: item.update(draft=True),
        lambda item: item["assets"][0].update(size=11),
    ],
)
def test_manual_apply_requires_remote_confirmation(maintenance, mutation):
    original, _current, response, credits, calls, _looked_up = maintenance
    response.update(deepcopy(original))
    response["body"] = update_body(original["body"], credits)
    mutation(response)
    with pytest.raises(ValueError, match="did not confirm"):
        release.contributors(TAG, apply=True)
    assert sum(method == "PATCH" for _endpoint, method, _payload in calls) == 1


def test_manual_summary_records_the_interval_without_requiring_an_apply(
    maintenance,
    monkeypatch,
    tmp_path,
):
    _original, _current, _response, _credits, calls, _looked_up = maintenance
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    release.contributors(TAG)
    report = summary.read_text(encoding="utf-8")
    assert HISTORICAL_BASE in report and HEAD in report
    assert "### Related pull requests and issues" in report
    assert "by @alice and @bob" in report
    assert f"[PR #7](https://github.com/{release.RELEASE_REPOSITORY}/pull/7)" in report
    assert (
        f"Related issue: [#3](https://github.com/{release.RELEASE_REPOSITORY}/issues/3)" in report
    )
    assert all(method == "GET" for _endpoint, method, _payload in calls)


def test_collection_failure_prevents_published_body_mutation(maintenance, monkeypatch):
    _original, _current, _response, _credits, calls, _looked_up = maintenance

    def fail_collection(*_args, **_kwargs):
        raise ValueError("GitHub issue identity mismatch")

    monkeypatch.setattr(release, "release_credits", fail_collection)
    with pytest.raises(ValueError, match="issue identity mismatch"):
        release.contributors(TAG, apply=True)
    assert all(method == "GET" for _endpoint, method, _payload in calls)
