"""Verified release boundaries, GitHub identities, and owned release-body updates."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
import html
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from scripts import release_contributors as contributors
from scripts.release_contributors import (
    END,
    HISTORICAL_BASE,
    START,
    Change,
    ContributionRange,
    Credits,
    GitHubAPI,
    RelatedIssue,
    collect_credits,
    contribution_range,
    recorded_base,
    update_body,
)
from solin.core.releases.version import ReleaseVersion

BASE = "a" * 40
HEAD = "b" * 40
COMMIT = "c" * 40
OUTSIDE = "d" * 40
REPOSITORY = "fixture/release-repository"
INTERVAL = ContributionRange(BASE, HEAD, (COMMIT, HEAD))


def version(value: str) -> ReleaseVersion:
    parsed = ReleaseVersion.parse(value)
    assert parsed is not None
    return parsed


def published(value: str) -> dict:
    parsed = version(value)
    return {"tag_name": parsed.tag, "draft": False, "prerelease": parsed.is_beta}


@dataclass
class ScriptedGit:
    replies: dict[tuple[str, ...], str | Exception]
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, *arguments: str) -> str:
        self.calls.append(arguments)
        reply = self.replies[arguments]
        if isinstance(reply, Exception):
            raise reply
        return reply


def range_git(tag: str | None = None, *, commits: str = f"{COMMIT}\n{HEAD}\n") -> ScriptedGit:
    base = BASE if tag else HISTORICAL_BASE
    replies: dict[tuple[str, ...], str | Exception] = {
        ("merge-base", "--is-ancestor", HISTORICAL_BASE, HEAD): "",
        ("rev-list", "--reverse", f"{base}..{HEAD}"): commits,
    }
    if tag:
        replies.update(
            {
                ("rev-parse", f"refs/tags/{tag}^{{commit}}"): BASE,
                ("merge-base", "--is-ancestor", HISTORICAL_BASE, BASE): "",
                ("merge-base", "--is-ancestor", BASE, HEAD): "",
            }
        )
    return ScriptedGit(replies)


@pytest.mark.parametrize(
    ("target", "available", "expected"),
    [
        ("26.32.0b11", ["26.32.0b9", "26.31.9", "26.32.0b10"], "26.32.0b10"),
        ("26.32.0b1", ["26.31.9b10", "26.31.9"], "26.31.9"),
        ("26.32.0", ["26.32.0b10", "26.31.9", "26.31.10"], "26.31.10"),
        ("27.1.0", ["26.99.0", "26.100.0", "27.1.0", "27.2.0"], "26.100.0"),
        ("26.32.0b10", ["26.32.0", "26.32.0b10", "26.32.0b9"], "26.32.0b9"),
    ],
)
def test_predecessor_uses_published_channel_and_semantic_order(
    target: str,
    available: list[str],
    expected: str,
) -> None:
    tag = version(expected).tag
    git = range_git(tag)
    releases = [published(value) for value in available]
    # Dates and listing order must not determine the predecessor.
    for index, release in enumerate(releases):
        release["published_at"] = f"2026-10-{30 - index:02d}T00:00:00Z"

    assert contribution_range(version(target), HEAD, releases, git) == INTERVAL
    assert git.calls == [
        ("merge-base", "--is-ancestor", HISTORICAL_BASE, HEAD),
        ("rev-parse", f"refs/tags/{tag}^{{commit}}"),
        ("merge-base", "--is-ancestor", HISTORICAL_BASE, BASE),
        ("merge-base", "--is-ancestor", BASE, HEAD),
        ("rev-list", "--reverse", f"{BASE}..{HEAD}"),
    ]


def test_drafts_failed_tags_and_invalid_tags_do_not_select_a_base() -> None:
    releases = [
        published("26.31.1"),
        {**published("26.32.0b8"), "draft": True},
        {"tag_name": "26.32.0-beta.9"},  # A tag with no published release.
        {**published("26.32.0b7"), "draft": True, "prerelease": False},
        {"tag_name": "v26.31.9", "draft": False, "prerelease": False},
        {"tag_name": "26.032.0-beta.9", "draft": False, "prerelease": True},
        {"tag_name": None, "draft": False, "prerelease": False},
    ]
    git = range_git("26.31.1")

    assert contribution_range(version("26.32.0b10"), HEAD, releases, git) == INTERVAL
    assert ("rev-parse", "refs/tags/26.31.1^{commit}") in git.calls


@pytest.mark.parametrize("releases", [[], [published("26.32.0b1")]])
def test_no_eligible_release_uses_only_the_verified_historical_boundary(
    releases: list[dict],
) -> None:
    git = range_git()

    result = contribution_range(version("26.32.0"), HEAD, releases, git)

    assert result == ContributionRange(HISTORICAL_BASE, HEAD, (COMMIT, HEAD))
    assert git.calls == [
        ("merge-base", "--is-ancestor", HISTORICAL_BASE, HEAD),
        ("rev-list", "--reverse", f"{HISTORICAL_BASE}..{HEAD}"),
    ]


@pytest.mark.parametrize("tag", ["26.31.1", "26.32.0-beta.1", "27.1.0"])
@pytest.mark.parametrize("channel", [None, "false", 0, "opposite"])
def test_contradictory_published_channel_aborts_even_with_a_recorded_base(
    tag: str,
    channel: object,
) -> None:
    parsed = ReleaseVersion.from_tag(tag)
    assert parsed is not None
    prerelease = not parsed.is_beta if channel == "opposite" else channel
    git = range_git()
    with pytest.raises(ValueError, match="Published release channel differs"):
        contribution_range(
            version("26.32.0"),
            HEAD,
            [{"tag_name": tag, "draft": False, "prerelease": prerelease}],
            git,
            recorded_base=HISTORICAL_BASE,
        )
    assert len(git.calls) == 1


@pytest.mark.parametrize("base", [BASE, HISTORICAL_BASE])
def test_recorded_base_preserves_exact_range_when_new_predecessors_are_published(base: str) -> None:
    git = range_git("26.31.1") if base == BASE else range_git()
    result = contribution_range(
        version("26.32.0"),
        HEAD,
        [published("26.31.1"), published("26.31.9")],
        git,
        recorded_base=base,
    )
    assert result == ContributionRange(base, HEAD, (COMMIT, HEAD))
    assert not any(call[0] == "rev-parse" for call in git.calls)
    assert git.calls[-1] == ("rev-list", "--reverse", f"{base}..{HEAD}")


@pytest.mark.parametrize("base", ["main", BASE.upper(), BASE[:-1], BASE + "\n"])
def test_invalid_recorded_base_is_rejected(base: str) -> None:
    git = range_git()
    with pytest.raises(ValueError, match="previous release SHA"):
        contribution_range(version("26.32.0"), HEAD, [], git, recorded_base=base)
    assert len(git.calls) == 1


@pytest.mark.parametrize("ancestor", [HISTORICAL_BASE, BASE])
def test_recorded_base_must_still_pass_both_ancestry_checks(ancestor: str) -> None:
    git = range_git("26.31.1")
    command = (
        "merge-base",
        "--is-ancestor",
        ancestor,
        BASE if ancestor == HISTORICAL_BASE else HEAD,
    )
    failure = subprocess.CalledProcessError(1, ["git", *command])
    git.replies[command] = failure
    with pytest.raises(subprocess.CalledProcessError) as caught:
        contribution_range(version("26.32.0"), HEAD, [], git, recorded_base=BASE)
    assert caught.value is failure
    assert not any(call[0] == "rev-list" for call in git.calls)


@pytest.mark.parametrize(
    "command",
    [
        ("merge-base", "--is-ancestor", HISTORICAL_BASE, HEAD),
        ("rev-parse", "refs/tags/26.31.1^{commit}"),
        ("merge-base", "--is-ancestor", HISTORICAL_BASE, BASE),
        ("merge-base", "--is-ancestor", BASE, HEAD),
    ],
)
def test_missing_tag_or_failed_ancestry_aborts_without_guessing_a_range(
    command: tuple[str, ...],
) -> None:
    git = range_git("26.31.1")
    failure = subprocess.CalledProcessError(1, ["git", *command])
    git.replies[command] = failure

    with pytest.raises(subprocess.CalledProcessError) as caught:
        contribution_range(version("26.32.0"), HEAD, [published("26.31.1")], git)

    assert caught.value is failure
    assert git.calls[-1] == command
    assert not any(call[0] == "rev-list" for call in git.calls)


@pytest.mark.parametrize("head", ["main", "b" * 39, "b" * 41, "B" * 40, HEAD + "\n"])
def test_invalid_target_is_rejected_before_git(head: str) -> None:
    git = ScriptedGit({})
    with pytest.raises(ValueError, match="target SHA"):
        contribution_range(version("26.32.0"), head, [], git)
    assert git.calls == []


def test_invalid_resolved_predecessor_is_rejected_before_range_lookup() -> None:
    git = range_git("26.31.1")
    git.replies[("rev-parse", "refs/tags/26.31.1^{commit}")] = "not-a-sha"
    with pytest.raises(ValueError, match="previous release SHA"):
        contribution_range(version("26.32.0"), HEAD, [published("26.31.1")], git)
    assert len(git.calls) == 2


@pytest.mark.parametrize("commits", ["main\n", COMMIT + "\n\n" + HEAD, f"{COMMIT}\n{COMMIT}\n"])
def test_invalid_or_duplicate_rev_list_output_is_rejected(commits: str) -> None:
    with pytest.raises(ValueError, match="commit range"):
        contribution_range(version("26.32.0"), HEAD, [], range_git(commits=commits))


def test_empty_range_is_valid_and_needs_no_github_requests() -> None:
    interval = contribution_range(version("26.32.0"), HEAD, [], range_git(commits=""))
    api = ScriptedAPI([])
    assert collect_credits(interval, REPOSITORY, api) == Credits(interval, (), ())
    assert api.queries == []


@pytest.fixture
def local_history(tmp_path: Path) -> tuple[Callable[..., str], tuple[str, ...]]:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git is required for the disposable history fixture")
    assert executable is not None
    repository = tmp_path / "history"
    repository.mkdir()

    def git(*arguments: str) -> str:
        return subprocess.run(
            [
                executable,
                "-c",
                "user.name=Release Fixture",
                "-c",
                "user.email=release@example.invalid",
                "-c",
                "commit.gpgSign=false",
                *arguments,
            ],
            cwd=repository,
            input="",
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            timeout=10,
        ).stdout.strip()

    git("init", "--quiet", "--template=", "--object-format=sha1")
    tree = git("mktree")
    commits: list[str] = []
    for name in ("initial", "historical", "published", "change", "release", "later"):
        parents = ("-p", commits[-1]) if commits else ()
        commits.append(git("commit-tree", tree, *parents, "-m", name))
    git("tag", "26.31.1", commits[2])
    return git, tuple(commits)


@pytest.mark.parametrize("has_predecessor", [False, True])
def test_real_rev_list_excludes_initial_base_and_commits_after_target(
    local_history: tuple[Callable[..., str], tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
    has_predecessor: bool,
) -> None:
    git, commits = local_history
    initial, historical, previous, change, head, later = commits
    monkeypatch.setattr(contributors, "HISTORICAL_BASE", historical)
    releases = [published("26.31.1")] if has_predecessor else []

    result = contribution_range(version("26.32.0"), head, releases, git)

    expected = (change, head) if has_predecessor else (previous, change, head)
    assert result == ContributionRange(previous if has_predecessor else historical, head, expected)
    assert not {initial, historical, result.base, later}.intersection(result.commits)


def connection(nodes: list[dict], *, cursor: str | None = None) -> dict:
    return {"nodes": nodes, "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}}


def user(login: str, kind: str = "User") -> dict:
    return {"login": login, "__typename": kind}


def pull(
    author: object,
    *,
    sha: str = COMMIT,
    merged: object = True,
    repository: str = REPOSITORY,
    number: int = 7,
    title: str = "feat(player): Improve playback",
) -> dict:
    return {
        "number": number,
        "title": title,
        "merged": merged,
        "mergeCommit": {"oid": sha},
        "author": author,
        "repository": {"nameWithOwner": repository},
    }


def commit_data(
    sha: str,
    *,
    authors: list[dict] | None = None,
    pulls: list[dict] | None = None,
    title: str = "fix: Restore playback",
) -> dict:
    return {
        "oid": sha,
        "messageHeadline": title,
        "authors": connection(
            authors if authors is not None else [{"user": user("direct-author")}]
        ),
        "associatedPullRequests": connection(pulls if pulls is not None else []),
    }


def response(repository: dict) -> dict:
    return {"data": {"repository": repository}}


def direct_change(sha: str, logins: tuple[str, ...] = ("direct-author",)) -> Change:
    return Change(
        "fix: Restore playback",
        f"Commit {sha[:7]}",
        f"https://github.com/{REPOSITORY}/commit/{sha}",
        logins,
    )


def pr_change(logins: tuple[str, ...], *, number: int = 7) -> Change:
    return Change(
        "feat(player): Improve playback",
        f"PR #{number}",
        f"https://github.com/{REPOSITORY}/pull/{number}",
        logins,
    )


def pull_api(pulls: list[dict], stage: str) -> ScriptedAPI:
    initial = commit_data(COMMIT)
    if stage == "batch":
        initial["associatedPullRequests"] = connection(pulls)
        return ScriptedAPI([response({"c0": initial})])
    initial["associatedPullRequests"]["pageInfo"] = {
        "hasNextPage": True,
        "endCursor": "pull-page",
    }
    return ScriptedAPI(
        [
            response({"c0": initial}),
            response({"commit": {"oid": COMMIT, "associatedPullRequests": connection(pulls)}}),
        ]
    )


@dataclass
class ScriptedAPI:
    replies: list[object]
    queries: list[str] = field(default_factory=list)
    # Explicit detail replies disable the default, including an empty list:
    # regressions must fail on a missing or unexpected issue-resolution request.
    detail_replies: list[object] | None = None
    issue_replies: dict[str, object] = field(default_factory=dict)
    issue_calls: list[str] = field(default_factory=list)
    known_pulls: dict[int, dict] = field(default_factory=dict)
    default_detail_numbers: list[int] = field(default_factory=list)
    repositories: tuple[str, ...] = (REPOSITORY,)
    query_repositories: list[str] = field(default_factory=list)

    def __call__(
        self, endpoint: str, *, method: str = "GET", payload: dict | None = None
    ) -> object:
        if endpoint != "graphql":
            assert method == "GET" and payload is None
            self.issue_calls.append(endpoint)
            reply = self.issue_replies.pop(endpoint)
            if isinstance(reply, Exception):
                raise reply
            return reply
        assert endpoint == "graphql"
        assert method == "POST"
        assert payload is not None
        variables = payload["variables"]
        assert set(variables) == {"owner", "name"}
        repository = f"{variables['owner']}/{variables['name']}"
        assert repository in self.repositories
        self.query_repositories.append(repository)
        query = payload["query"]
        self.queries.append(query)
        detail = re.search(r"pullRequest\(number:\s*(\d+)", query)
        if detail:
            if self.detail_replies is not None:
                assert self.detail_replies, "Unexpected extra pull request detail query"
                reply = self.detail_replies.pop(0)
                if isinstance(reply, Exception):
                    raise reply
                return reply
            number = int(detail[1])
            assert number in self.known_pulls, "Detail query for a pull request never collected"
            assert number not in self.default_detail_numbers, "Duplicate pull request detail query"
            self.default_detail_numbers.append(number)
            node = self.known_pulls[number]
            return response(
                {
                    "pullRequest": {
                        "number": number,
                        "body": "",
                        "merged": node["merged"],
                        "mergeCommit": node["mergeCommit"],
                        "closingIssuesReferences": connection([]),
                        "labels": connection([]),
                    }
                }
            )
        assert self.replies, "Unexpected extra GitHub query"
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, dict):
            data = reply.get("data")
            repo = data.get("repository") if isinstance(data, dict) else None
            if isinstance(repo, dict):
                for value in repo.values():
                    if not isinstance(value, dict):
                        continue
                    pulls = value.get("associatedPullRequests")
                    if isinstance(pulls, dict) and isinstance(pulls.get("nodes"), list):
                        for node in pulls["nodes"]:
                            if isinstance(node, dict) and type(node.get("number")) is int:
                                self.known_pulls[node["number"]] = node
        return reply


def test_batches_lookup_exact_range_shas_and_keep_aliases_aligned_across_batches() -> None:
    shas = tuple(f"{number:040x}" for number in range(1, 42))
    interval = ContributionRange(BASE, shas[-1], shas)
    scripted = ScriptedAPI(
        [
            response(
                {
                    f"c{index}": commit_data(sha)
                    for index, sha in enumerate(shas[offset : offset + 20])
                }
            )
            for offset in range(0, len(shas), 20)
        ]
    )
    api: GitHubAPI = scripted

    assert collect_credits(interval, REPOSITORY, api) == Credits(
        interval, tuple(direct_change(sha) for sha in shas), ()
    )
    assert scripted.replies == []
    assert len(scripted.queries) == 3
    for offset, query in zip(range(0, len(shas), 20), scripted.queries, strict=True):
        batch = shas[offset : offset + 20]
        assert re.findall(r'c(\d+): object\(expression: "([0-9a-f]{40})"\)', query) == [
            (str(index), sha) for index, sha in enumerate(batch)
        ]
        assert query.count("authors(first: 100)") == len(batch)
        assert query.count("messageHeadline") == len(batch)
        assert query.count("associatedPullRequests(first: 100)") == len(batch)
        assert query.count("repository { nameWithOwner }") == len(batch)


def test_credit_identity_uses_linked_authors_coauthors_and_only_prs_merged_inside_range() -> None:
    first = commit_data(
        COMMIT,
        authors=[
            {"user": user("Zelda")},
            {"user": user("alice")},
            {"user": user("ALICE")},
            {"user": None, "name": "Not a login", "email": "guess@example.invalid"},
            {"user": None},
            {"user": user("automation", "Bot")},
            {"user": user("dependabot[bot]")},
            {"user": user("RUNNER[BOT]")},
        ],
        pulls=[
            pull(user("Bob"), sha=HEAD, number=1),
            pull(user("ALIce"), number=2),
            pull(user("robot", "Bot"), number=3),
            pull(None, number=4),
            pull(user("open-pr"), merged=False, number=5),
            pull(user("base-pr"), sha=BASE, number=6),
            pull(user("later-pr"), sha=OUTSIDE, number=7),
            {**pull(user("no-merge"), merged=False, number=8), "mergeCommit": None},
        ],
    )
    second = commit_data(HEAD, authors=[{"user": user("Direct-Only")}, {"user": None}])
    api = ScriptedAPI([response({"c0": first, "c1": second})])

    result = collect_credits(INTERVAL, REPOSITORY, api)
    assert tuple(login.lower() for login in result.logins) == (
        "alice",
        "bob",
        "direct-only",
        "zelda",
    )
    assert result.unlinked_commits == tuple(sorted((COMMIT, HEAD)))
    assert {change.reference for change in result.changes} == {"PR #1", "PR #2", "PR #3", "PR #4"}
    assert set(api.default_detail_numbers) == {1, 2, 3, 4}
    assert BASE not in api.queries[0] and OUTSIDE not in api.queries[0]


@pytest.mark.parametrize("field_name", ["authors", "associatedPullRequests"])
def test_both_connections_paginate_every_page_for_the_same_commit(field_name: str) -> None:
    initial = commit_data(COMMIT)
    cursor = 'first-"page\\cursor'
    initial[field_name]["pageInfo"] = {"hasNextPage": True, "endCursor": cursor}
    if field_name == "authors":
        pages = [[{"user": user("coauthor")}], [{"user": None}]]
        expected = ("direct-author", "coauthor")
        unlinked = (COMMIT,)
    else:
        pages = [
            [pull(user("pr-author"))],
            [pull(user("outside-author"), sha=BASE, number=8)],
        ]
        expected = ("pr-author", "direct-author")
        unlinked = ()
    api = ScriptedAPI(
        [
            response({"c0": initial}),
            response(
                {"commit": {"oid": COMMIT, field_name: connection(pages[0], cursor="second-page")}}
            ),
            response({"commit": {"oid": COMMIT, field_name: connection(pages[1])}}),
        ]
    )
    interval = ContributionRange(BASE, COMMIT, (COMMIT,))

    expected_change = (
        direct_change(COMMIT, expected) if field_name == "authors" else pr_change(expected)
    )
    assert collect_credits(interval, REPOSITORY, api) == Credits(
        interval, (expected_change,), unlinked
    )
    assert api.replies == []
    assert len(api.queries) == (3 if field_name == "authors" else 4)
    for query, after in zip(api.queries[1:3], (cursor, "second-page"), strict=True):
        assert f'commit: object(expression: "{COMMIT}")' in query
        assert f"{field_name}(first: 100, after: {json.dumps(after)})" in query
        other = "authors" if field_name == "associatedPullRequests" else "associatedPullRequests"
        assert f"{other}(" not in query
        if field_name == "associatedPullRequests":
            assert "repository { nameWithOwner }" in query


@pytest.mark.parametrize("stage", ["batch", "page"])
@pytest.mark.parametrize(
    "repository",
    [
        {"nameWithOwner": "other/release-repository"},
        {"nameWithOwner": "fixture/other-repository"},
    ],
)
def test_prs_with_valid_foreign_repository_are_excluded(stage: str, repository: object) -> None:
    foreign = {**pull(user("foreign-author"), number=8), "repository": repository}
    matching = pull(user("local-author"), repository=REPOSITORY.upper())
    interval = ContributionRange(BASE, COMMIT, (COMMIT,))

    assert collect_credits(interval, REPOSITORY, pull_api([foreign, matching], stage)) == Credits(
        interval,
        (pr_change(("local-author", "direct-author")),),
        (),
    )


@pytest.mark.parametrize("stage", ["batch", "page"])
@pytest.mark.parametrize("eligibility", ["included", "unmerged", "foreign"])
@pytest.mark.parametrize("missing", ["merged", "repository", "author", "mergeCommit"])
def test_pr_required_fields_are_checked_before_eligibility_filtering(
    stage: str,
    eligibility: str,
    missing: str,
) -> None:
    node = pull(
        user("pr-author"),
        merged=eligibility != "unmerged",
        repository="other/repository" if eligibility == "foreign" else REPOSITORY,
    )
    del node[missing]
    api = pull_api([node], stage)

    with pytest.raises(ValueError, match="Invalid GitHub pull request attribution"):
        collect_credits(ContributionRange(BASE, COMMIT, (COMMIT,)), REPOSITORY, api)
    assert len(api.queries) == (2 if stage == "page" else 1)
    assert api.replies == []


@pytest.mark.parametrize("stage", ["batch", "page"])
@pytest.mark.parametrize("eligibility", ["included", "unmerged", "foreign"])
@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        pytest.param("merged", None, id="null-merged"),
        pytest.param("merged", "true", id="string-merged"),
        pytest.param("merged", 1, id="integer-merged"),
        pytest.param("merged", 0, id="integer-unmerged"),
        pytest.param("repository", None, id="null-repository"),
        pytest.param("repository", {}, id="missing-repository-name"),
        pytest.param("repository", [], id="list-repository"),
        pytest.param("repository", REPOSITORY, id="string-repository"),
        pytest.param("repository", {"nameWithOwner": None}, id="null-repository-name"),
        pytest.param("repository", {"nameWithOwner": ""}, id="empty-repository-name"),
        pytest.param("repository", {"nameWithOwner": []}, id="list-repository-name"),
        pytest.param("repository", {"nameWithOwner": 1}, id="integer-repository-name"),
        pytest.param("mergeCommit", {}, id="missing-merge-sha"),
        pytest.param("mergeCommit", [], id="list-merge-commit"),
        pytest.param("mergeCommit", COMMIT, id="string-merge-commit"),
        pytest.param("mergeCommit", {"oid": None}, id="null-merge-sha"),
        pytest.param("mergeCommit", {"oid": []}, id="unhashable-list-merge-sha"),
        pytest.param("mergeCommit", {"oid": {}}, id="unhashable-dict-merge-sha"),
        pytest.param("mergeCommit", {"oid": 1}, id="integer-merge-sha"),
        pytest.param("mergeCommit", {"oid": "main"}, id="symbolic-merge-sha"),
        pytest.param("mergeCommit", {"oid": COMMIT[:-1]}, id="short-merge-sha"),
        pytest.param("mergeCommit", {"oid": COMMIT + "c"}, id="long-merge-sha"),
        pytest.param("mergeCommit", {"oid": COMMIT.upper()}, id="uppercase-merge-sha"),
        pytest.param("mergeCommit", {"oid": COMMIT + "\n"}, id="newline-merge-sha"),
    ],
)
def test_malformed_pr_shapes_abort_with_value_error_before_eligibility_filtering(
    stage: str,
    eligibility: str,
    field_name: str,
    value: object,
) -> None:
    node = pull(
        user("pr-author"),
        merged=eligibility != "unmerged",
        repository="other/repository" if eligibility == "foreign" else REPOSITORY,
    )
    node[field_name] = value
    api = pull_api([node], stage)

    with pytest.raises(ValueError, match="Invalid GitHub pull request"):
        collect_credits(ContributionRange(BASE, COMMIT, (COMMIT,)), REPOSITORY, api)
    assert len(api.queries) == (2 if stage == "page" else 1)
    assert api.replies == []


@pytest.mark.parametrize("stage", ["batch", "page"])
@pytest.mark.parametrize("repository", [REPOSITORY, "other/repository"])
def test_merged_pr_requires_a_merge_commit_even_when_foreign(stage: str, repository: str) -> None:
    node = {**pull(user("pr-author"), repository=repository), "mergeCommit": None}
    with pytest.raises(ValueError, match="Invalid GitHub pull request merge identity"):
        collect_credits(
            ContributionRange(BASE, COMMIT, (COMMIT,)), REPOSITORY, pull_api([node], stage)
        )


@pytest.mark.parametrize("stage", ["batch", "page"])
@pytest.mark.parametrize("repository", [REPOSITORY, "other/repository"])
def test_unmerged_pr_may_have_a_null_merge_commit(stage: str, repository: str) -> None:
    node = {**pull(user("open-pr"), merged=False, repository=repository), "mergeCommit": None}
    interval = ContributionRange(BASE, COMMIT, (COMMIT,))

    assert collect_credits(interval, REPOSITORY, pull_api([node], stage)) == Credits(
        interval,
        (direct_change(COMMIT),),
        (),
    )


def test_authors_and_prs_have_independent_pagination_cursors() -> None:
    initial = commit_data(COMMIT)
    for field_name in ("authors", "associatedPullRequests"):
        initial[field_name]["pageInfo"] = {"hasNextPage": True, "endCursor": "shared-cursor"}
    api = ScriptedAPI(
        [
            response({"c0": initial}),
            response(
                {"commit": {"oid": COMMIT, "authors": connection([{"user": user("coauthor")}])}}
            ),
            response(
                {
                    "commit": {
                        "oid": COMMIT,
                        "associatedPullRequests": connection([pull(user("pr-author"))]),
                    }
                }
            ),
        ]
    )
    interval = ContributionRange(BASE, COMMIT, (COMMIT,))

    assert collect_credits(interval, REPOSITORY, api) == Credits(
        interval,
        (pr_change(("pr-author", "direct-author", "coauthor")),),
        (),
    )
    assert len(api.queries) == 4
    assert 'authors(first: 100, after: "shared-cursor")' in api.queries[1]
    assert 'associatedPullRequests(first: 100, after: "shared-cursor")' in api.queries[2]


def test_graphql_errors_reject_otherwise_complete_data() -> None:
    partial = response({"c0": commit_data(COMMIT), "c1": commit_data(HEAD)})
    partial["errors"] = [{"message": "One resolver failed"}]
    with pytest.raises(ValueError, match="query failed"):
        collect_credits(INTERVAL, REPOSITORY, ScriptedAPI([partial]))


@pytest.mark.parametrize("field_name", ["authors", "associatedPullRequests"])
@pytest.mark.parametrize("stage", ["batch", "page"])
@pytest.mark.parametrize(
    "bad_response",
    [
        None,
        [],
        {},
        {"data": None},
        {"data": {"repository": None}},
        {"data": {"repository": {}}, "errors": [{"message": "rate limit"}]},
    ],
)
def test_failed_graphql_envelopes_never_return_partial_credits(
    field_name: str,
    stage: str,
    bad_response: object,
) -> None:
    initial = commit_data(COMMIT)
    initial[field_name]["pageInfo"] = {"hasNextPage": True, "endCursor": "next"}
    replies = [response({"c0": initial}), bad_response] if stage == "page" else [bad_response]
    api = ScriptedAPI(replies)

    with pytest.raises(ValueError, match="query failed|Missing GitHub contributor repository"):
        collect_credits(ContributionRange(BASE, COMMIT, (COMMIT,)), REPOSITORY, api)
    assert len(api.queries) == (2 if stage == "page" else 1)


@pytest.mark.parametrize("field_name", ["authors", "associatedPullRequests"])
@pytest.mark.parametrize("stage", ["batch", "page"])
def test_transport_failure_propagates_instead_of_returning_partial_credits(
    field_name: str,
    stage: str,
) -> None:
    initial = commit_data(COMMIT)
    initial[field_name]["pageInfo"] = {"hasNextPage": True, "endCursor": "next"}
    failure = subprocess.CalledProcessError(1, ["gh", "api", "graphql"])
    api = ScriptedAPI([response({"c0": initial}), failure] if stage == "page" else [failure])

    with pytest.raises(subprocess.CalledProcessError) as caught:
        collect_credits(ContributionRange(BASE, COMMIT, (COMMIT,)), REPOSITORY, api)
    assert caught.value is failure


@pytest.mark.parametrize("field_name", ["authors", "associatedPullRequests"])
@pytest.mark.parametrize("stage", ["batch", "page"])
@pytest.mark.parametrize("bad_commit", [None, {}, {"oid": OUTSIDE}])
def test_absent_or_wrong_commit_identity_is_rejected(
    field_name: str,
    stage: str,
    bad_commit: object,
) -> None:
    initial = commit_data(COMMIT)
    initial[field_name]["pageInfo"] = {"hasNextPage": True, "endCursor": "next"}
    replies: list[object] = (
        [response({"c0": initial}), response({"commit": bad_commit})]
        if stage == "page"
        else [response({"c0": bad_commit})]
    )
    with pytest.raises(ValueError, match="commit identity mismatch"):
        collect_credits(
            ContributionRange(BASE, COMMIT, (COMMIT,)), REPOSITORY, ScriptedAPI(replies)
        )


def test_partial_batch_with_a_missing_commit_alias_aborts_all_credits() -> None:
    api = ScriptedAPI([response({"c0": commit_data(COMMIT)})])
    with pytest.raises(ValueError, match="commit identity mismatch"):
        collect_credits(INTERVAL, REPOSITORY, api)


@pytest.mark.parametrize("field_name", ["authors", "associatedPullRequests"])
@pytest.mark.parametrize("stage", ["batch", "page"])
@pytest.mark.parametrize(
    ("bad_connection", "message"),
    [
        (None, "connection"),
        ({}, "connection"),
        ({"nodes": None}, "connection"),
        ({"nodes": [None]}, "node"),
        ({"nodes": []}, "pagination"),
        ({"nodes": [], "pageInfo": None}, "pagination"),
        ({"nodes": [], "pageInfo": {"hasNextPage": 1}}, "pagination"),
        ({"nodes": [], "pageInfo": {"hasNextPage": True}}, "did not advance"),
        ({"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": ""}}, "did not advance"),
        ({"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": 1}}, "did not advance"),
    ],
)
def test_partial_or_malformed_connection_data_aborts_in_initial_and_later_pages(
    field_name: str,
    stage: str,
    bad_connection: object,
    message: str,
) -> None:
    initial = commit_data(COMMIT)
    replies: list[object]
    if stage == "page":
        initial[field_name]["pageInfo"] = {"hasNextPage": True, "endCursor": "next"}
        replies = [
            response({"c0": initial}),
            response(
                {
                    "commit": {
                        "oid": COMMIT,
                        field_name: bad_connection,
                    }
                }
            ),
        ]
    else:
        initial[field_name] = bad_connection
        replies = [response({"c0": initial})]
    with pytest.raises(ValueError, match=message):
        collect_credits(
            ContributionRange(BASE, COMMIT, (COMMIT,)), REPOSITORY, ScriptedAPI(replies)
        )


@pytest.mark.parametrize("field_name", ["authors", "associatedPullRequests"])
@pytest.mark.parametrize("cursors", [("same", "same"), ("first", "second", "first")])
def test_repeated_or_cycling_cursor_is_rejected_before_another_request(
    field_name: str,
    cursors: tuple[str, ...],
) -> None:
    initial = commit_data(COMMIT)
    initial[field_name]["pageInfo"] = {"hasNextPage": True, "endCursor": cursors[0]}
    api = ScriptedAPI(
        [
            response({"c0": initial}),
            *[
                response({"commit": {"oid": COMMIT, field_name: connection([], cursor=cursor)}})
                for cursor in cursors[1:]
            ],
        ]
    )
    with pytest.raises(ValueError, match="pagination did not advance"):
        collect_credits(ContributionRange(BASE, COMMIT, (COMMIT,)), REPOSITORY, api)
    assert len(api.queries) == len(cursors)
    assert api.replies == []


@pytest.mark.parametrize("source", ["authors", "associatedPullRequests"])
@pytest.mark.parametrize(
    "account", ["alice", {}, user("invalid login"), user("alice", "Organization")]
)
def test_invalid_linked_account_is_rejected_instead_of_rendered(
    source: str, account: object
) -> None:
    data = commit_data(COMMIT)
    node = {"user": account} if source == "authors" else pull(account)
    data[source] = connection([node])
    with pytest.raises(ValueError, match="account|login"):
        collect_credits(
            ContributionRange(BASE, COMMIT, (COMMIT,)),
            REPOSITORY,
            ScriptedAPI([response({"c0": data})]),
        )


@pytest.mark.parametrize("source", ["authors", "associatedPullRequests"])
def test_missing_author_data_is_not_treated_as_an_unlinked_account(source: str) -> None:
    data = commit_data(COMMIT)
    node = {} if source == "authors" else pull(user("alice"))
    if source == "associatedPullRequests":
        del node["author"]
    data[source] = connection([node])
    message = (
        "Missing GitHub commit author"
        if source == "authors"
        else "Invalid GitHub pull request attribution"
    )
    with pytest.raises(ValueError, match=message):
        collect_credits(
            ContributionRange(BASE, COMMIT, (COMMIT,)),
            REPOSITORY,
            ScriptedAPI([response({"c0": data})]),
        )


def issue_node(
    number: int = 3,
    *,
    repository: str = REPOSITORY,
    state: str = "CLOSED",
    reason: str | None = "COMPLETED",
) -> dict:
    return {
        "number": number,
        "state": state,
        "stateReason": reason,
        "repository": {"nameWithOwner": repository},
    }


def pull_detail(
    issues: list[dict],
    *,
    cursor: str | None = None,
    body: str = "",
    labels: tuple[str, ...] = (),
    label_cursor: str | None = None,
) -> dict:
    return response(
        {
            "pullRequest": {
                "number": 7,
                "body": body,
                "merged": True,
                "mergeCommit": {"oid": COMMIT},
                "closingIssuesReferences": connection(issues, cursor=cursor),
                "labels": connection([{"name": label} for label in labels], cursor=label_cursor),
            }
        }
    )


def issue_api(
    issues: list[dict],
    detail_replies: Sequence[object] | None = None,
) -> ScriptedAPI:
    return ScriptedAPI(
        [response({"c0": commit_data(COMMIT, pulls=[pull(user("pr-author"))])})],
        detail_replies=[pull_detail(issues)] if detail_replies is None else list(detail_replies),
    )


def collect_single(api: ScriptedAPI) -> Credits:
    return collect_credits(ContributionRange(BASE, COMMIT, (COMMIT,)), REPOSITORY, api)


def test_pr_labels_group_changes_and_preserve_authors_and_related_issues() -> None:
    api = issue_api(
        [],
        detail_replies=[pull_detail([issue_node()], labels=("enhancement", "bug"))],
    )
    result = collect_single(api)
    rendered = result.markdown()

    assert "#### Bug fixes\n\n" in rendered
    assert "#### Features and improvements" not in rendered
    assert rendered.count("[PR #7]") == 1
    assert ", by @pr-author and @direct-author." in rendered
    assert f"Related issue: [#3](https://github.com/{REPOSITORY}/issues/3)." in rendered
    assert "labels(first: 100) { nodes { name }" in api.queries[1]
    assert "labels(" not in api.queries[0]
    assert len(api.queries) == 2


@pytest.mark.parametrize(
    "labels,heading",
    [
        (
            (
                "bug",
                "enhancement",
                "github_actions",
                "dependencies",
                "translations",
                "documentation",
            ),
            "Bug fixes",
        ),
        (
            ("enhancement", "github_actions", "dependencies", "translations", "documentation"),
            "Features and improvements",
        ),
        (("github_actions", "dependencies", "translations", "documentation"), "CI/CD"),
        (("dependencies", "translations", "documentation"), "Dependencies"),
        (("translations", "documentation"), "Translations"),
        (("documentation",), "Documentation"),
        (("unrecognized",), "Other changes"),
        ((), "Other changes"),
    ],
)
def test_release_label_priority_is_independent_of_github_label_order(
    labels: tuple[str, ...], heading: str
) -> None:
    for ordered in (labels, tuple(reversed(labels))):
        api = issue_api([], detail_replies=[pull_detail([], labels=ordered)])
        result = collect_single(api)
        assert re.findall(r"^#### (.+)$", result.markdown(), re.MULTILINE) == [heading]
        assert result.markdown().count("[PR #7]") == 1


def test_group_order_and_rows_are_stable_with_direct_commits_last() -> None:
    labels = (
        "documentation",
        "translations",
        "dependencies",
        "github_actions",
        "enhancement",
        "bug",
    )
    changes = tuple(
        replace(pr_change((f"author-{number}",), number=number), labels=(label,))
        for number, label in enumerate(labels, start=1)
    )
    second_bug = replace(pr_change(("another-author",), number=8), labels=("bug", "enhancement"))
    unlabelled = pr_change(("unlabelled-author",), number=9)
    direct = direct_change(HEAD)
    credits = Credits(INTERVAL, (direct, *changes, second_bug, unlabelled), ())

    rendered = credits.markdown()

    assert re.findall(r"^#### (.+)$", rendered, re.MULTILINE) == [
        "Bug fixes",
        "Features and improvements",
        "CI/CD",
        "Dependencies",
        "Translations",
        "Documentation",
        "Other changes",
    ]
    assert re.findall(r"\[(PR #[0-9]+|Commit [0-9a-f]+)\]", rendered) == [
        "PR #6",
        "PR #8",
        "PR #5",
        "PR #4",
        "PR #3",
        "PR #2",
        "PR #1",
        f"Commit {HEAD[:7]}",
        "PR #9",
    ]
    assert rendered.count(f"<!-- contribution-range: {INTERVAL.base}..{INTERVAL.head} -->") == 1
    assert credits.changes == (direct, *changes, second_bug, unlabelled)
    assert set(credits.logins) == {
        "direct-author",
        "another-author",
        "unlabelled-author",
        *(f"author-{number}" for number in range(1, 7)),
    }


def test_labels_are_case_insensitive_and_deduplicated() -> None:
    api = issue_api([], detail_replies=[pull_detail([], labels=("BUG", "bug", "Enhancement"))])
    result = collect_single(api)
    assert result.changes[0].labels == ("bug", "enhancement")
    assert re.findall(r"^#### (.+)$", result.markdown(), re.MULTILINE) == ["Bug fixes"]


def test_label_and_issue_connections_paginate_independently_without_nested_commit_queries() -> None:
    cursor = 'next-"page\\cursor'
    ignored = tuple(f"unrecognized-{number}" for number in range(100))
    api = issue_api(
        [],
        detail_replies=[
            pull_detail([issue_node(9)], cursor=cursor, labels=ignored, label_cursor=cursor),
            pull_detail([issue_node(3)]),
            pull_detail([], labels=("enhancement",), label_cursor="last-labels"),
            pull_detail([], labels=("bug", "enhancement")),
        ],
    )

    result = collect_single(api)

    assert result.changes[0].issues == (
        RelatedIssue("#3", f"https://github.com/{REPOSITORY}/issues/3"),
        RelatedIssue("#9", f"https://github.com/{REPOSITORY}/issues/9"),
    )
    assert set(result.changes[0].labels) == {"bug", "enhancement", *ignored}
    assert re.findall(r"^#### (.+)$", result.markdown(), re.MULTILINE) == ["Bug fixes"]
    assert len(api.queries) == 5
    assert "labels(" not in api.queries[0]
    assert f"closingIssuesReferences(first: 100, after: {json.dumps(cursor)})" in api.queries[2]
    assert "labels(" not in api.queries[2]
    for query, after in zip(api.queries[3:], (cursor, "last-labels"), strict=True):
        assert f"labels(first: 100, after: {json.dumps(after)})" in query
        assert "closingIssuesReferences(" not in query
    assert api.replies == [] and api.detail_replies == []


@pytest.mark.parametrize("cursors", [("same", "same"), ("first", "second", "first")])
def test_label_pagination_rejects_repeated_or_cycling_cursors(cursors: tuple[str, ...]) -> None:
    api = issue_api([], detail_replies=[pull_detail([], label_cursor=cursor) for cursor in cursors])
    with pytest.raises(ValueError, match="pagination did not advance"):
        collect_single(api)
    assert len(api.queries) == 1 + len(cursors)
    assert api.detail_replies == []


@pytest.mark.parametrize("stage", ["initial", "page"])
@pytest.mark.parametrize(
    "bad_connection,message",
    [
        (None, "connection"),
        ({}, "connection"),
        ({"nodes": None}, "connection"),
        ({"nodes": [None]}, "node"),
        ({"nodes": []}, "pagination"),
        ({"nodes": [], "pageInfo": {"hasNextPage": 1}}, "pagination"),
        ({"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": None}}, "did not advance"),
        ({"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": ""}}, "did not advance"),
    ],
)
def test_malformed_label_connections_abort_instead_of_silently_using_fallback(
    stage: str, bad_connection: object, message: str
) -> None:
    detail = pull_detail([])
    detail["data"]["repository"]["pullRequest"]["labels"] = bad_connection
    replies = [detail] if stage == "initial" else [pull_detail([], label_cursor="next"), detail]
    api = issue_api([], detail_replies=replies)
    with pytest.raises(ValueError, match=message):
        collect_single(api)
    assert api.detail_replies == []


@pytest.mark.parametrize("stage", ["initial", "page"])
@pytest.mark.parametrize(
    "label", [{}, {"name": None}, {"name": 1}, {"name": []}, {"name": ""}, {"name": " \t\n"}]
)
def test_invalid_label_names_abort_on_every_page(stage: str, label: dict) -> None:
    detail = pull_detail([])
    detail["data"]["repository"]["pullRequest"]["labels"] = connection([label])
    replies = [detail] if stage == "initial" else [pull_detail([], label_cursor="next"), detail]
    with pytest.raises(ValueError, match="pull request label name"):
        collect_single(issue_api([], detail_replies=replies))


def test_catalog_defines_headings_priority_and_order_without_automation_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = tmp_path / "labels.json"
    catalog.write_text(
        json.dumps(
            [
                {"name": "custom", "release_heading": "Custom heading"},
                {"name": "enhancement", "release_heading": "Renamed improvements"},
                {"name": "bug", "release_heading": "Renamed repairs"},
                {"name": "internal", "release_heading": None},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(contributors, "_LABELS_PATH", catalog)
    changes = (
        replace(pr_change((), number=1), labels=("bug",)),
        replace(pr_change((), number=2), labels=("bug", "enhancement")),
        replace(pr_change((), number=3), labels=("custom", "bug")),
        replace(pr_change((), number=4), labels=("internal",)),
    )

    rendered = Credits(INTERVAL, changes, ()).markdown()

    assert re.findall(r"^#### (.+)$", rendered, re.MULTILINE) == [
        "Custom heading",
        "Renamed improvements",
        "Renamed repairs",
        "Other changes",
    ]
    assert re.findall(r"\[PR #(\d+)\]", rendered) == ["3", "2", "1", "4"]


@pytest.mark.parametrize(
    "catalog",
    [
        None,
        {},
        [None],
        [{"release_heading": "Repairs"}],
        [{"name": "", "release_heading": "Repairs"}],
        [{"name": "bug", "release_heading": ""}],
        [{"name": "bug", "release_heading": []}],
        [{"name": "bug", "release_heading": "Repairs\nInjected heading"}],
        [
            {"name": "bug", "release_heading": "Repairs"},
            {"name": "BUG", "release_heading": "Other repairs"},
        ],
    ],
)
def test_invalid_catalog_aborts_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog: object
) -> None:
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(catalog), encoding="utf-8")
    monkeypatch.setattr(contributors, "_LABELS_PATH", path)
    with pytest.raises(ValueError, match="release label"):
        Credits(INTERVAL, (pr_change(()),), ()).markdown()


@pytest.mark.parametrize("contents", [None, "not json"])
def test_missing_or_unreadable_catalog_does_not_silently_reclassify_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: str | None
) -> None:
    path = tmp_path / "labels.json"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(contributors, "_LABELS_PATH", path)
    with pytest.raises(FileNotFoundError if contents is None else json.JSONDecodeError):
        Credits(INTERVAL, (pr_change(()),), ()).markdown()


def test_catalog_path_is_independent_of_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    change = replace(pr_change(()), labels=("bug",))
    assert "#### Bug fixes\n\n" in Credits(INTERVAL, (change,), ()).markdown()


def test_grouping_update_preserves_range_surrounding_notes_and_idempotency() -> None:
    prefix = f"# Reviewed notes\r\n<!-- source-commit: {HEAD} -->\r\n"
    suffix = "\r\nReviewed footer  \r\n"
    changes = (pr_change(("alice",)), direct_change(HEAD, ("bob",)))
    previous = Credits(INTERVAL, changes, ())
    credits = Credits(INTERVAL, (replace(changes[0], labels=("bug",)), changes[1]), ())

    updated = update_body(prefix + previous.markdown() + suffix, credits)

    assert updated == prefix + credits.markdown() + suffix
    assert re.findall(r"^#### (.+)$", updated, re.MULTILINE) == ["Bug fixes", "Other changes"]
    assert recorded_base(updated, HEAD) == BASE
    assert update_body(updated, credits) == updated
    assert credits.logins == previous.logins == ("alice", "bob")


def test_open_related_issue_is_included_without_closure_lookup() -> None:
    api = issue_api([issue_node(state="OPEN", reason=None)])
    assert collect_single(api).changes[0].issues == (
        RelatedIssue("#3", f"https://github.com/{REPOSITORY}/issues/3"),
    )
    assert len(api.queries) == 2


def test_issue_already_in_title_is_not_appended_again() -> None:
    issue = RelatedIssue("#3", f"https://github.com/{REPOSITORY}/issues/3")
    change = Change(
        "fix: repair #3", "PR #7", f"https://github.com/{REPOSITORY}/pull/7", (), (issue,)
    )
    assert "Related issue:" not in change.markdown()


def test_changes_follow_commit_order_and_merge_authors_from_every_associated_commit() -> None:
    direct = "e" * 40
    interval = ContributionRange(BASE, HEAD, (direct, COMMIT, HEAD))
    node = pull(user("pr-author"), sha=HEAD)
    api = ScriptedAPI(
        [
            response(
                {
                    "c0": commit_data(direct),
                    "c1": commit_data(
                        COMMIT,
                        authors=[
                            {"user": user("original-author")},
                            {"user": user("coauthor")},
                            {"user": None},
                        ],
                        pulls=[node, node],
                    ),
                    "c2": commit_data(
                        HEAD,
                        authors=[{"user": user("merge-author")}, {"user": user("COAUTHOR")}],
                        pulls=[node],
                    ),
                }
            )
        ]
    )

    result = collect_credits(interval, REPOSITORY, api)

    assert result == Credits(
        interval,
        (
            direct_change(direct),
            pr_change(("pr-author", "original-author", "coauthor", "merge-author")),
        ),
        (COMMIT,),
    )
    assert api.default_detail_numbers == [7]
    assert len(api.queries) == 2
    assert "closingIssuesReferences" not in api.queries[0]
    assert "timelineItems" not in api.queries[1]


def test_related_queries_wait_for_all_batches_and_use_later_merge_commit_authors() -> None:
    shas = tuple(f"{number:040x}" for number in range(1, 22))
    interval = ContributionRange(BASE, shas[-1], shas)
    node = pull(user("pr-author"), sha=shas[-1])
    commits = [commit_data(sha) for sha in shas]
    commits[0] = commit_data(shas[0], authors=[{"user": user("original-author")}], pulls=[node])
    commits[-1] = commit_data(shas[-1], authors=[{"user": user("merge-author")}])
    api = ScriptedAPI(
        [
            response({f"c{index}": commit for index, commit in enumerate(commits[:20])}),
            response({"c0": commits[-1]}),
        ]
    )

    result = collect_credits(interval, REPOSITORY, api)

    assert result.changes == (
        *(direct_change(sha) for sha in shas[1:-1]),
        pr_change(("pr-author", "original-author", "merge-author")),
    )
    assert len(api.queries) == 3
    assert "c0: object" in api.queries[1]
    assert "pullRequest(number: 7)" in api.queries[2]
    assert api.replies == []


@pytest.mark.parametrize("source", ["commit", "pull"])
def test_changes_by_bots_are_retained_without_bot_mentions(source: str) -> None:
    bot = user("automation", "Bot")
    node = commit_data(
        COMMIT,
        authors=[{"user": bot}],
        pulls=[pull(user("dependabot[bot]"))] if source == "pull" else [],
    )
    api = ScriptedAPI([response({"c0": node})])

    result = collect_single(api)

    expected = pr_change(()) if source == "pull" else direct_change(COMMIT, ())
    assert result.changes == (expected,)
    assert result.logins == ()
    assert "Related pull requests and issues" in result.markdown()
    assert "@" not in result.markdown()
    assert "by " not in result.markdown()


def test_unlinked_direct_commit_retains_its_title_and_link_without_guessing_identity() -> None:
    title = "custom(scope)!: Preserve direct changes"
    node = commit_data(
        COMMIT,
        title=title,
        authors=[{"user": None, "name": "Someone", "email": "alice@example.test"}],
    )
    result = collect_single(ScriptedAPI([response({"c0": node})]))
    assert result.changes == (
        Change(
            title, f"Commit {COMMIT[:7]}", f"https://github.com/{REPOSITORY}/commit/{COMMIT}", ()
        ),
    )
    assert result.unlinked_commits == (COMMIT,)
    assert result.logins == ()
    assert "Preserve direct changes" in result.markdown()
    assert "custom(scope)!:" not in result.markdown()


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("title", None),
        ("title", ""),
        ("title", " \t\n"),
        ("title", []),
        ("number", None),
        ("number", True),
        ("number", 0),
        ("number", -1),
        ("number", "7"),
    ],
)
@pytest.mark.parametrize("stage", ["batch", "page"])
def test_invalid_included_pr_titles_and_numbers_abort(
    stage: str, field_name: str, value: object
) -> None:
    node = pull(user("pr-author"))
    node[field_name] = value
    with pytest.raises(ValueError, match="title|number"):
        collect_single(pull_api([node], stage))


@pytest.mark.parametrize("title", [None, "", " \r\n", [], 7])
def test_invalid_commit_headline_aborts_before_any_related_query(title: object) -> None:
    node = commit_data(COMMIT, pulls=[pull(user("pr-author"))])
    node["messageHeadline"] = title
    api = ScriptedAPI([response({"c0": node})], detail_replies=[])
    with pytest.raises(ValueError, match="title"):
        collect_single(api)
    assert len(api.queries) == 1


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("title", "Different title"),
        ("mergeCommit", {"oid": HEAD}),
        ("author", user("other-author")),
    ],
)
def test_repeated_pr_identity_must_remain_consistent(field_name: str, value: object) -> None:
    node = pull(user("pr-author"))
    changed = {**node, field_name: value}
    api = ScriptedAPI(
        [
            response(
                {
                    "c0": commit_data(COMMIT, pulls=[node]),
                    "c1": commit_data(HEAD, pulls=[changed]),
                }
            )
        ],
        detail_replies=[],
    )
    with pytest.raises(ValueError, match="changed during"):
        collect_credits(INTERVAL, REPOSITORY, api)
    assert len(api.queries) == 1


@pytest.mark.parametrize(
    "state,reason",
    [
        ("OPEN", None),
        ("OPEN", "REOPENED"),
        ("CLOSED", "COMPLETED"),
        ("CLOSED", "NOT_PLANNED"),
        ("CLOSED", None),
        ("CLOSED", "DUPLICATE"),
    ],
)
def test_related_issue_does_not_depend_on_state_or_closure(state: str, reason: str | None) -> None:
    api = issue_api([issue_node(state=state, reason=reason)])
    assert collect_single(api).changes[0].issues == (
        RelatedIssue("#3", f"https://github.com/{REPOSITORY}/issues/3"),
    )
    assert len(api.queries) == 2
    assert "closingIssuesReferences" not in api.queries[0]
    assert "closingIssuesReferences(first: 100)" in api.queries[1]
    assert not any(token in api.queries[1] for token in ("stateReason", "timelineItems", "state "))
    assert api.issue_calls == []
    assert api.detail_replies == [] and api.replies == []


def test_empty_closing_references_need_no_issue_lookups() -> None:
    api = issue_api([])
    assert collect_single(api).changes[0].issues == ()
    assert len(api.queries) == 2
    assert api.issue_calls == []


def test_closing_references_paginate_deduplicate_and_order_issue_links() -> None:
    cursor = 'next-"issues\\cursor'
    api = issue_api(
        [],
        detail_replies=[
            pull_detail([issue_node(9)], cursor=cursor),
            pull_detail([issue_node(9, repository=REPOSITORY.upper()), issue_node(3)]),
        ],
    )
    assert collect_single(api).changes[0].issues == (
        RelatedIssue("#3", f"https://github.com/{REPOSITORY}/issues/3"),
        RelatedIssue("#9", f"https://github.com/{REPOSITORY}/issues/9"),
    )
    assert len(api.queries) == 3
    assert f"closingIssuesReferences(first: 100, after: {json.dumps(cursor)})" in api.queries[2]
    assert api.issue_calls == []
    assert api.replies == [] and api.detail_replies == []


def test_cross_repository_link_keeps_qualified_reference() -> None:
    foreign = "other/issues"
    api = issue_api([issue_node(repository=foreign)])
    assert collect_single(api).changes[0].issues == (
        RelatedIssue(f"{foreign}#3", f"https://github.com/{foreign}/issues/3"),
    )
    assert api.query_repositories == [REPOSITORY, REPOSITORY]


@pytest.mark.parametrize("stage", ["initial", "page", "labels-page"])
@pytest.mark.parametrize(
    "field_name,value",
    [
        ("number", 8),
        ("merged", False),
        ("merged", 1),
        ("mergeCommit", None),
        ("mergeCommit", {"oid": OUTSIDE}),
    ],
)
def test_related_pr_identity_is_revalidated_on_each_page(
    stage: str, field_name: str, value: object
) -> None:
    detail = pull_detail([])
    detail["data"]["repository"]["pullRequest"][field_name] = value
    replies = (
        [detail]
        if stage == "initial"
        else [
            pull_detail([], cursor="next")
            if stage == "page"
            else pull_detail([], label_cursor="next"),
            detail,
        ]
    )
    api = issue_api([], detail_replies=replies)
    with pytest.raises(ValueError, match="pull request identity mismatch"):
        collect_single(api)
    assert api.detail_replies == []


@pytest.mark.parametrize("number", [None, True, "7", 0, []])
def test_related_pr_detail_rejects_malformed_numbers(number: object) -> None:
    detail = pull_detail([])
    detail["data"]["repository"]["pullRequest"]["number"] = number
    api = issue_api([], detail_replies=[detail])
    with pytest.raises(ValueError, match="reference number"):
        collect_single(api)
    assert api.detail_replies == []


@pytest.mark.parametrize("stage", ["related", "related-page", "labels-page"])
@pytest.mark.parametrize(
    "bad_reply",
    [
        None,
        [],
        {},
        {"data": {"repository": None}},
        {"data": {"repository": {}}, "errors": [{"message": "rate limit"}]},
    ],
)
def test_related_query_failures_never_return_partial_credits(stage: str, bad_reply: object) -> None:
    details = (
        [bad_reply]
        if stage == "related"
        else [
            pull_detail([], cursor="next")
            if stage == "related-page"
            else pull_detail([], label_cursor="next"),
            bad_reply,
        ]
    )
    api = issue_api([], detail_replies=details)
    with pytest.raises(ValueError, match="query failed|Missing GitHub contributor repository"):
        collect_single(api)
    assert api.replies == [] and api.detail_replies == []


@pytest.mark.parametrize("stage", ["initial", "labels-page"])
def test_related_query_transport_failures_propagate(stage: str) -> None:
    failure = subprocess.CalledProcessError(1, ["gh", "api", "graphql"])
    details = [failure] if stage == "initial" else [pull_detail([], label_cursor="next"), failure]
    api = issue_api([], detail_replies=details)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        collect_single(api)
    assert caught.value is failure


@pytest.mark.parametrize(
    "bad_connection,message",
    [
        (None, "connection"),
        ({}, "connection"),
        ({"nodes": None}, "connection"),
        ({"nodes": [None]}, "node"),
        ({"nodes": []}, "pagination"),
        ({"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": None}}, "did not advance"),
        ({"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": ""}}, "did not advance"),
        ({"nodes": [], "pageInfo": {"hasNextPage": 1}}, "pagination"),
    ],
)
@pytest.mark.parametrize("stage", ["initial", "page"])
def test_malformed_closing_connections_abort_on_every_page(
    bad_connection: object,
    message: str,
    stage: str,
) -> None:
    detail = pull_detail([])
    detail["data"]["repository"]["pullRequest"]["closingIssuesReferences"] = bad_connection
    details = [detail] if stage == "initial" else [pull_detail([], cursor="next"), detail]
    api = issue_api([], detail_replies=details)
    with pytest.raises(ValueError, match=message):
        collect_single(api)
    assert api.detail_replies == []


@pytest.mark.parametrize("cursors", [("same", "same"), ("first", "second", "first")])
def test_closing_reference_pagination_rejects_repeated_or_cycling_cursors(
    cursors: tuple[str, ...],
) -> None:
    api = issue_api([], detail_replies=[pull_detail([], cursor=cursor) for cursor in cursors])
    with pytest.raises(ValueError, match="pagination did not advance"):
        collect_single(api)
    assert len(api.queries) == 1 + len(cursors)
    assert api.detail_replies == []


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("number", None),
        ("number", True),
        ("number", "3"),
        ("number", 0),
        ("repository", None),
        ("repository", {}),
        ("repository", {"nameWithOwner": "owner/repository/extra"}),
        ("repository", {"nameWithOwner": "owner/.."}),
        ("repository", {"nameWithOwner": "owner/repository\n"}),
    ],
)
def test_issue_reference_identity_is_validated_even_when_open(
    field_name: str, value: object
) -> None:
    node = issue_node(state="OPEN", reason=None)
    node[field_name] = value
    api = issue_api([node])
    with pytest.raises(ValueError, match="reference"):
        collect_single(api)
    assert len(api.queries) == 2


@pytest.mark.parametrize(
    "title,rendered",
    [
        ("fix: restore playback", "Restore playback"),
        ("feat(player)!: restore playback", "Restore playback"),
        ("custom(scope): restore playback", "Restore playback"),
        ("security!: restore playback", "Restore playback"),
        ("Restore playback: keep punctuation", "Restore playback: keep punctuation"),
        ("Playback without prefix", "Playback without prefix"),
    ],
)
def test_title_prefix_is_stripped_only_during_rendering(title: str, rendered: str) -> None:
    change = Change(title, "PR #7", f"https://github.com/{REPOSITORY}/pull/7", ("alice",))
    assert change.title == title
    assert change.markdown() == f"- {rendered}: [PR #7]({change.url}), by @alice."
    assert change.title == title


def test_untrusted_title_html_markdown_mentions_and_newlines_are_escaped() -> None:
    title = "fix: <script> @pretender [label](https://evil.test) *bold* _italic_ `code` ~strike~ \\\nnext"
    change = Change(title, "PR #7", f"https://github.com/{REPOSITORY}/pull/7", ("real-author",))
    rendered = change.markdown()
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "@pretender" not in rendered
    assert "<code>&#64;pretender</code>" in rendered
    assert "<code>@pretender</code>" in html.unescape(rendered)
    assert "\u200b" not in html.unescape(rendered)
    assert r"\[label\]" in rendered
    assert r"\*bold\*" in rendered
    assert r"\_italic\_" in rendered
    assert r"\`code\`" in rendered
    assert r"\~strike\~" in rendered
    assert "\\\\ next" in rendered
    assert "\n" not in rendered
    assert re.findall(r"@[A-Za-z0-9-]+", rendered) == ["@real-author"]


@pytest.mark.parametrize(
    "token",
    [
        "@pretender",
        "@organization/team",
    ],
)
def test_title_handles_are_literal_code_while_verified_authors_and_issue_links_are_preserved(
    token: str,
) -> None:
    issue = RelatedIssue("#3", f"https://github.com/{REPOSITORY}/issues/3")
    change = Change(
        f"fix: Review {token}",
        "PR #7",
        f"https://github.com/{REPOSITORY}/pull/7",
        ("real-author",),
        (issue,),
    )

    rendered = change.markdown()
    decoded = html.unescape(rendered)
    title = decoded.split(": [PR #7]", 1)[0]

    # Numeric entities alone become normal @ characters before GitHub autolinking.
    # The handle must remain in code after decoding without changing copyable text.
    assert title == f"- Review <code>{token}</code>"
    assert title.replace("<code>", "").replace("</code>", "") == f"- Review {token}"
    assert "\u200b" not in decoded
    assert f"[PR #7]({change.url}), by @real-author." in rendered
    assert f"Related issue: [#3]({issue.url})." in rendered
    assert change.title == f"fix: Review {token}"


@pytest.mark.parametrize(
    "reference", ["#48", "other/repository#48", f"https://github.com/{REPOSITORY}/issues/48"]
)
def test_title_references_are_verified_and_not_appended_twice(reference: str) -> None:
    title = f"fix: Review {reference} mentioned by @pretender"
    repo = "other/repository" if reference.startswith("other/") else REPOSITORY
    api = ScriptedAPI(
        [response({"c0": commit_data(COMMIT, pulls=[pull(user("real-author"), title=title)])})],
        detail_replies=[pull_detail([])],
        issue_replies={
            f"repos/{repo}/issues/48": {
                "number": 48,
                "html_url": f"https://github.com/{repo}/issues/48",
            }
        },
    )
    result = collect_single(api)
    assert result.changes[0].issues == (
        RelatedIssue(
            "#48" if repo == REPOSITORY else f"{repo}#48", f"https://github.com/{repo}/issues/48"
        ),
    )
    assert result.logins == ("direct-author", "real-author")
    assert "Related issue:" not in result.markdown()
    assert reference in result.markdown()
    assert "<code>&#64;pretender</code>" in result.markdown()
    assert not api.issue_replies


def referenced_api(body: str, reply: object) -> ScriptedAPI:
    api = issue_api([], detail_replies=[pull_detail([], body=body)])
    api.issue_replies[f"repos/{REPOSITORY}/issues/3"] = reply
    return api


def test_body_mentions_and_closing_links_are_combined_without_duplicate_lookups() -> None:
    body = f"Related to #3, {REPOSITORY.upper()}#3 and https://github.com/{REPOSITORY}/issues/3."
    api = issue_api([issue_node(3)], detail_replies=[pull_detail([issue_node(3)], body=body)])
    assert collect_single(api).changes[0].issues == (
        RelatedIssue("#3", f"https://github.com/{REPOSITORY}/issues/3"),
    )
    assert api.issue_calls == []


@pytest.mark.parametrize(
    "body",
    [
        "Related to #3",
        "Fixes: #3",
        f"See {REPOSITORY}#3",
        f"[Context](https://github.com/{REPOSITORY}/issues/3)",
        f"https://github.com/{REPOSITORY}/issues/3#issuecomment-123",
    ],
)
def test_body_reference_is_included_without_closure_or_issue_author_credit(body: str) -> None:
    api = referenced_api(
        body,
        {
            "number": 3,
            "html_url": f"https://github.com/{REPOSITORY}/issues/3",
            "state": "open",
            "user": {"login": "issue-author"},
        },
    )
    result = collect_single(api)
    assert result.changes[0].issues == (
        RelatedIssue("#3", f"https://github.com/{REPOSITORY}/issues/3"),
    )
    assert "issue-author" not in result.logins
    assert api.issue_calls == [f"repos/{REPOSITORY}/issues/3"]


@pytest.mark.parametrize(
    "body",
    [
        "",
        "PR #0",
        "token#3",
        "https://example.test/#3",
        f"https://github.com/{REPOSITORY}/pull/3",
        "other/../#3",
    ],
)
def test_non_issue_references_are_not_collected(body: str) -> None:
    api = issue_api([], detail_replies=[pull_detail([], body=body)])
    assert collect_single(api).changes[0].issues == ()
    assert api.issue_calls == []


def test_reference_to_pull_request_is_not_rendered_as_issue() -> None:
    api = referenced_api(
        "#3",
        {"number": 3, "pull_request": {}, "html_url": f"https://github.com/{REPOSITORY}/pull/3"},
    )
    assert collect_single(api).changes[0].issues == ()


def test_cross_repository_body_references_are_verified_and_deduplicated() -> None:
    repo = "other/issues"
    body = f"Context: {repo}#3, {repo.upper()}#3, https://github.com/{repo}/issues/3."
    api = issue_api([], detail_replies=[pull_detail([], body=body)])
    api.issue_replies[f"repos/{repo}/issues/3"] = {
        "number": 3,
        "html_url": f"https://github.com/{repo}/issues/3",
    }
    assert collect_single(api).changes[0].issues == (
        RelatedIssue(f"{repo}#3", f"https://github.com/{repo}/issues/3"),
    )
    assert api.issue_calls == [f"repos/{repo}/issues/3"]


def test_title_deduplication_preserves_other_numbers_and_repositories() -> None:
    issues = (
        RelatedIssue("#3", f"https://github.com/{REPOSITORY}/issues/3"),
        RelatedIssue("#30", f"https://github.com/{REPOSITORY}/issues/30"),
        RelatedIssue("other/issues#3", "https://github.com/other/issues/issues/3"),
    )
    change = Change(
        "fix: repair #3", "PR #7", f"https://github.com/{REPOSITORY}/pull/7", (), issues
    )
    rendered = change.markdown()
    assert f"[#3]({issues[0].url})" not in rendered
    assert f"Related issues: [#30]({issues[1].url}), [other/issues#3]({issues[2].url})." in rendered


def test_issue_removed_with_conventional_prefix_still_has_related_link() -> None:
    issue = RelatedIssue("#3", f"https://github.com/{REPOSITORY}/issues/3")
    change = Change(
        "fix(#3): repair", "PR #7", f"https://github.com/{REPOSITORY}/pull/7", (), (issue,)
    )
    assert f"Related issue: [#3]({issue.url})." in change.markdown()


def test_quoted_issue_url_is_recognized() -> None:
    api = referenced_api(
        f'Context: "https://github.com/{REPOSITORY}/issues/3"',
        {"number": 3, "html_url": f"https://github.com/{REPOSITORY}/issues/3"},
    )
    assert len(collect_single(api).changes[0].issues) == 1


def test_numeric_html_entity_is_not_an_issue_reference() -> None:
    api = issue_api([], detail_replies=[pull_detail([], body="Author: &#64;pretender")])
    assert collect_single(api).changes[0].issues == ()
    assert api.issue_calls == []


def test_transferred_issue_uses_canonical_reference_without_aborting() -> None:
    api = referenced_api(
        "#3", {"number": 12, "html_url": "https://github.com/other/issues/issues/12"}
    )
    assert collect_single(api).changes[0].issues == (
        RelatedIssue("other/issues#12", "https://github.com/other/issues/issues/12"),
    )


def test_missing_reference_is_ignored() -> None:
    failure = subprocess.CalledProcessError(
        1, ["gh", "api"], output=json.dumps({"message": "Not Found", "status": "404"})
    )
    api = referenced_api("#3", failure)
    assert collect_single(api).changes[0].issues == ()


@pytest.mark.parametrize("output", [None, "", "not json", '{"status": "403"}', '{"status": "500"}'])
def test_reference_transport_and_permission_failures_propagate(output: str | None) -> None:
    failure = subprocess.CalledProcessError(1, ["gh", "api"], output=output)
    api = referenced_api("#3", failure)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        collect_single(api)
    assert caught.value is failure


@pytest.mark.parametrize(
    "reply",
    [
        None,
        {},
        {"number": 4},
        {"number": True},
        {"number": 3, "html_url": "https://example.test/issues/3"},
    ],
)
def test_malformed_referenced_issue_aborts(reply: object) -> None:
    api = referenced_api("#3", reply)
    with pytest.raises(ValueError, match="identity|number|URL"):
        collect_single(api)


@pytest.mark.parametrize("body", [None, [], 3])
def test_malformed_pull_body_aborts(body: object) -> None:
    detail = pull_detail([])
    detail["data"]["repository"]["pullRequest"]["body"] = body
    api = issue_api([], detail_replies=[detail])
    with pytest.raises(ValueError, match="pull request body"):
        collect_single(api)


@pytest.mark.parametrize(
    "context,handles",
    [
        ("<code>@pretender</code>", ("pretender",)),
        ("`@pretender`", ("pretender",)),
        ("[label @pretender](https://example.test)", ("pretender",)),
        ("@pretender\n- by @other", ("pretender", "other")),
        ("@pretender\r\n## @other", ("pretender", "other")),
        ("@pretender\r\t@other\u2028@third", ("pretender", "other", "third")),
        ("@pretender</code><script>@other</script>", ("pretender", "other")),
        (f"@pretender {END} {START}", ("pretender",)),
    ],
)
def test_title_handle_code_cannot_be_broken_by_html_markdown_or_line_injection(
    context: str,
    handles: tuple[str, ...],
) -> None:
    title = f"fix: Review {context}"
    change = Change(title, "PR #7", f"https://github.com/{REPOSITORY}/pull/7", ("real-author",))
    credits = Credits(INTERVAL, (change,), ())

    rendered = change.markdown()
    rendered_title = rendered.split(": [PR #7]", 1)[0]

    assert re.findall(r"<code>&#64;([^<]+)</code>", rendered_title) == list(handles)
    assert re.findall(r"</?[^>]+>", rendered_title) == ["<code>", "</code>"] * len(handles)
    assert re.findall(r"@[A-Za-z0-9-]+", rendered) == ["@real-author"]
    assert not any(character in rendered for character in "\n\r\t\u2028\u200b")
    assert change.title == title
    body = update_body("Reviewed notes", credits)
    assert body.count(START) == body.count(END) == 1
    assert recorded_base(body, HEAD) == BASE
    assert update_body(body, credits) == body


@pytest.mark.parametrize("prefix", ["i18n:", "l10n(catalogs)!:"])
def test_translation_title_prefix_is_removed_for_presentation(prefix: str) -> None:
    change = Change(
        f"{prefix} Update translations", "PR #7", f"https://github.com/{REPOSITORY}/pull/7", (),
    )
    assert change.markdown().startswith("- Update translations: [PR #7]")


def test_title_preencoded_handle_entity_is_preserved_as_literal_text() -> None:
    change = Change(
        "fix: Review &#64;pretender",
        "PR #7",
        f"https://github.com/{REPOSITORY}/pull/7",
        ("real-author",),
    )
    rendered = change.markdown()
    assert "&amp;#64;pretender" in rendered
    assert "@pretender" not in html.unescape(rendered)
    assert re.findall(r"@[A-Za-z0-9-]+", rendered) == ["@real-author"]


def test_title_cannot_inject_owned_markers_or_contribution_metadata() -> None:
    change = Change(
        f"fix: {END} {START} <!-- contribution-range: {OUTSIDE}..{HEAD} -->",
        "PR #7",
        f"https://github.com/{REPOSITORY}/pull/7",
        (),
    )
    credits = Credits(INTERVAL, (change,), ())
    body = update_body("Notes", credits)
    assert body.count(START) == body.count(END) == 1
    assert recorded_base(body, HEAD) == BASE
    assert update_body(body, credits) == body


def test_credits_logins_are_derived_case_insensitively_from_all_change_rows() -> None:
    credits = Credits(
        INTERVAL,
        (
            pr_change(("Zelda", "alice", "ALICE")),
            direct_change(HEAD, ("Bob", "zelda", "bob")),
        ),
        (),
    )
    assert credits.logins == ("alice", "Bob", "Zelda")


def test_related_issue_links_render_singular_plural_and_qualified_references() -> None:
    issue = RelatedIssue("#3", f"https://github.com/{REPOSITORY}/issues/3")
    other = RelatedIssue("other/issues#4", "https://github.com/other/issues/issues/4")
    single = Change(
        "Fix playback", "PR #7", f"https://github.com/{REPOSITORY}/pull/7", (), (issue,)
    )
    multiple = Change(single.title, single.reference, single.url, (), (issue, other))
    assert single.markdown().endswith(f"Related issue: [#3]({issue.url}).")
    assert multiple.markdown().endswith(
        f"Related issues: [#3]({issue.url}), [other/issues#4]({other.url})."
    )


def test_old_owned_thanks_block_is_replaced_with_related_changes_without_changing_baseline() -> (
    None
):
    prefix = f"# Reviewed notes\r\n<!-- source-commit: {HEAD} -->\r\n"
    suffix = "\r\nReviewed footer  \r\n"
    old = (
        f"{START}\nThanks to @former for contributing to this release.\n\n"
        f"<!-- contribution-range: {BASE}..{HEAD} -->\n{END}"
    )
    credits = Credits(INTERVAL, (pr_change(("alice",)), direct_change(HEAD, ("bob",))), ())
    updated = update_body(prefix + old + suffix, credits)
    assert updated == prefix + credits.markdown() + suffix
    assert "Thanks to @former" not in updated
    assert updated.index("PR #7") < updated.index(f"Commit {HEAD[:7]}")
    assert recorded_base(updated, HEAD) == BASE
    assert update_body(updated, credits) == updated


@pytest.mark.parametrize("body", ["", "Release notes", "Release notes\n", "Notes\r\n\r\n"])
def test_managed_block_append_preserves_original_bytes_and_is_idempotent(body: str) -> None:
    credits = Credits(INTERVAL, (pr_change(("alice", "Bob")),), ())
    block = (
        f"{START}\n### Related pull requests and issues\n\n"
        "#### Other changes\n\n"
        f"- Improve playback: [PR #7](https://github.com/{REPOSITORY}/pull/7), by @alice and @Bob.\n\n"
        f"<!-- contribution-range: {BASE}..{HEAD} -->\n{END}"
    )
    separator = "\n" if body.endswith("\n") else "\n\n"

    result = update_body(body, credits)

    assert result == body + separator + block + "\n"
    assert result[: len(body)].encode("utf-8") == body.encode("utf-8")
    assert update_body(result, credits) == result


def test_managed_block_replacement_preserves_every_byte_of_surrounding_notes() -> None:
    prefix = (
        f"\ufeff# Mudanças 📝\r\n\tKeep trailing spaces  \r\n<!-- source-commit: {HEAD} -->\r\n"
    )
    suffix = "\r\n\r\n<details>Other credits: @someone</details>\t \r\n"
    body = prefix + START + "\r\nOutdated credits and range\r\n" + END + suffix
    credits = Credits(INTERVAL, (pr_change(("alice",)),), ())

    result = update_body(body, credits)

    assert result == prefix + credits.markdown() + suffix
    assert result[: len(prefix)].encode("utf-8") == prefix.encode("utf-8")
    assert result[-len(suffix) :].encode("utf-8") == suffix.encode("utf-8")
    assert update_body(result, credits) == result


def test_no_linked_humans_renders_range_without_an_empty_thanks_section() -> None:
    credits = Credits(INTERVAL, (), (COMMIT,))
    assert credits.markdown() == (f"{START}\n<!-- contribution-range: {BASE}..{HEAD} -->\n{END}")
    assert update_body(update_body("Notes", credits), credits) == update_body("Notes", credits)


@pytest.mark.parametrize(
    "body", ["", "Plain release notes", f"<!-- contribution-range: {BASE}..{HEAD} -->"]
)
def test_recorded_base_is_absent_when_no_managed_block_exists(body: str) -> None:
    assert recorded_base(body, HEAD) is None


def test_recorded_base_reads_only_the_metadata_inside_the_owned_block() -> None:
    prefix = f"Notes\r\n<!-- contribution-range: {OUTSIDE}..{HEAD} -->\r\n"
    suffix = f"\r\nOther metadata <!-- contribution-range: {OUTSIDE}..{OUTSIDE} -->"
    body = prefix + Credits(INTERVAL, (pr_change(("alice",)),), ()).markdown() + suffix
    assert recorded_base(body, HEAD) == BASE
    assert recorded_base(update_body(body, Credits(INTERVAL, (), (COMMIT,))), HEAD) == BASE


@pytest.mark.parametrize(
    "metadata",
    [
        "No range marker",
        f"<!-- contribution-range: {BASE}..{OUTSIDE} -->",
        f"<!-- contribution-range: {BASE.upper()}..{HEAD} -->",
        f"<!-- contribution-range: {BASE[:-1]}..{HEAD} -->",
        f"<!-- contribution-range: {BASE}..{HEAD[:-1]} -->",
        f"<!-- contribution-range: main..{HEAD} -->",
        f"<!-- contribution-range: {BASE}..{HEAD} -->\n<!-- contribution-range: {BASE}..{HEAD} -->",
        f"<!-- contribution-range: {BASE}..{HEAD} -->\n<!-- contribution-range: malformed -->",
        f"<!-- contribution-range: {BASE}...{HEAD} -->",
    ],
)
def test_recorded_base_rejects_missing_duplicate_or_wrong_range_metadata(metadata: str) -> None:
    body = f"Keep notes\r\n{START}\n{metadata}\n{END}\r\nKeep footer"
    with pytest.raises(ValueError, match="range is missing or differs"):
        recorded_base(body, HEAD)


@pytest.mark.parametrize(
    "markers",
    [
        START,
        END,
        END + START,
        START + START + END,
        START + END + END,
        START + END + START + END,
        START + START + END + END,
    ],
)
def test_malformed_managed_blocks_are_rejected_without_changing_release_text(markers: str) -> None:
    body = "Keep these notes\r\n" + markers + "\r\nAnd this footer  "
    original = body.encode("utf-8")
    with pytest.raises(ValueError, match="Malformed managed contributor block"):
        update_body(body, Credits(INTERVAL, (pr_change(("alice",)),), ()))
    with pytest.raises(ValueError, match="Malformed managed contributor block"):
        recorded_base(body, HEAD)
    assert body.encode("utf-8") == original
