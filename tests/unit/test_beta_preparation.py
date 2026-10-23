"""Authorization, committed-content boundaries, and resumable beta preparation."""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from itertools import count
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts import prepare_beta as beta

VERSION_PATH = "src/solin/version.py"
NOTES = "docs/release-notes/26.32.0-beta.4/en.md"


def version_source(value: str) -> bytes:
    return (
        '"""Canonical CalVer (YY.RELEASE.PATCH, optionally followed by bN)."""\n\n'
        f'VERSION = "{value}"\n__version__ = VERSION\n'
    ).encode()


def git(repository: Path, *arguments: str, data: bytes | None = None) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        input=data,
        capture_output=True,
        check=True,
        timeout=10,
    )
    return result.stdout.decode().strip()


@dataclass
class History:
    repository: Path
    head: str = ""

    def commit(self, changes: dict[str, tuple[str, bytes]], *, parent: str | None = None) -> str:
        parent = self.head if parent is None else parent
        if parent:
            git(self.repository, "read-tree", parent)
        for path, (mode, source) in changes.items():
            blob = git(self.repository, "hash-object", "-w", "--stdin", data=source)
            git(self.repository, "update-index", "--add", "--cacheinfo", mode, blob, path)
        tree = git(self.repository, "write-tree")
        parents = ["-p", parent] if parent else []
        self.head = git(self.repository, "commit-tree", tree, *parents, data=b"Fixture commit\n")
        return self.head

    def preparation(self, **overrides) -> tuple[str, str]:
        base = self.head
        changes = {
            VERSION_PATH: ("100644", version_source("26.32.0b4")),
            NOTES: ("100644", b""),
        }
        changes.update(overrides)
        return base, self.commit(changes)


@pytest.fixture
def history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> History:
    # Ignore the developer's signing, hooks, line-ending, and Git identity settings.
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    repository = tmp_path / "checkout"
    repository.mkdir()
    git(repository, "init", "--quiet", "--initial-branch=main")
    git(repository, "config", "user.name", "Beta Fixture")
    git(repository, "config", "user.email", "beta@example.invalid")
    git(repository, "config", "core.autocrlf", "false")
    result = History(repository)
    result.commit(
        {
            VERSION_PATH: ("100644", version_source("26.32.0b3")),
            "src/solin/application.py": ("100644", b"VALUE = 1\n"),
        }
    )
    monkeypatch.setattr(beta, "ROOT", repository)
    monkeypatch.setattr(beta.release, "ROOT", repository)
    monkeypatch.setattr(beta.release, "NOTES", repository / "docs/release-notes")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return result


@pytest.fixture
def authorization() -> dict[str, str]:
    return {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "BETA_RELEASE_ACTORS": "maintainer, release-admin",
        "GITHUB_ACTOR": "maintainer",
        "BETA_RELEASE_TRIGGERING_ACTOR": "release-admin",
    }


@pytest.mark.parametrize(
    "actors",
    [
        "",
        " \n\t",
        ",",
        "*",
        "@maintainer",
        '["maintainer"]',
        "maintainer;guest",
        "maintainer,invalid/name",
    ],
)
def test_authorization_rejects_blank_or_malformed_allowlist(authorization, actors):
    authorization["BETA_RELEASE_ACTORS"] = actors
    with pytest.raises(ValueError):
        beta.authorize(authorization)


@pytest.mark.parametrize(
    "key,value",
    [
        ("GITHUB_EVENT_NAME", "push"),
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("GITHUB_REF", "refs/heads/feature"),
        ("GITHUB_REF", "refs/tags/main"),
        ("GITHUB_ACTOR", ""),
        ("GITHUB_ACTOR", "maintainer-extra"),
        ("BETA_RELEASE_TRIGGERING_ACTOR", ""),
        ("BETA_RELEASE_TRIGGERING_ACTOR", "outsider"),
    ],
)
def test_authorization_checks_context_and_both_actor_identities(authorization, key, value):
    authorization[key] = value
    with pytest.raises(ValueError):
        beta.authorize(authorization)


def test_authorization_requires_configuration_and_current_triggering_actor(authorization):
    authorization.pop("BETA_RELEASE_ACTORS")
    with pytest.raises(ValueError):
        beta.authorize(authorization)
    authorization["BETA_RELEASE_ACTORS"] = "maintainer"
    authorization.pop("BETA_RELEASE_TRIGGERING_ACTOR")
    authorization["GITHUB_TRIGGERING_ACTOR"] = "maintainer"
    with pytest.raises(ValueError):
        beta.authorize(authorization)


def test_authorization_accepts_exact_case_insensitive_usernames_and_separators(authorization):
    authorization["BETA_RELEASE_ACTORS"] = "  MAINTAINER\nrelease-admin, third-user\t"
    beta.authorize(authorization)


def test_validation_accepts_only_next_beta_with_new_empty_notes(history):
    base, head = history.preparation()
    assert beta.validate_preparation(base, head).canonical == "26.32.0b4"


@pytest.mark.parametrize(
    "path", ["README.md", "src/solin/application.py", ".github/workflows/x.yml"]
)
def test_validation_rejects_unrelated_changes_even_documentation(history, path):
    base, head = history.preparation(**{path: ("100644", b"unexpected\n")})
    with pytest.raises(ValueError):
        beta.validate_preparation(base, head)


@pytest.mark.parametrize(
    "source",
    [
        version_source("26.32.0b4") + b"EXTRA = True\n",
        version_source("26.32.0b4").replace(b"__version__ = VERSION", b'__version__ = "other"'),
        version_source("26.32.0b4").replace(b"Canonical CalVer", b"Changed documentation"),
    ],
)
def test_validation_rejects_non_version_edits_in_canonical_module(history, source):
    base, head = history.preparation(**{VERSION_PATH: ("100644", source)})
    with pytest.raises(ValueError):
        beta.validate_preparation(base, head)


@pytest.mark.parametrize(
    "path,mode,source",
    [
        (NOTES, "120000", b"../../README.md"),
        (NOTES, "100755", b""),
        (VERSION_PATH, "120000", version_source("26.32.0b4")),
        (VERSION_PATH, "100755", version_source("26.32.0b4")),
    ],
)
def test_validation_rejects_symlinks_and_executable_modes(history, path, mode, source):
    # Stage Git tree entries directly so Windows needs no symlink privilege.
    base, head = history.preparation(**{path: (mode, source)})
    with pytest.raises(ValueError):
        beta.validate_preparation(base, head)


@pytest.mark.parametrize("notes", [b"\n", b" \t\r\n", b"Release changes\n"])
def test_validation_requires_zero_byte_notes_not_stripped_empty_text(history, notes):
    base, head = history.preparation(**{NOTES: ("100644", notes)})
    with pytest.raises(ValueError):
        beta.validate_preparation(base, head)


@pytest.mark.parametrize("old_notes", [b"", b"Existing notes\n"])
def test_validation_rejects_reusing_existing_notes(history, old_notes):
    history.commit({NOTES: ("100644", old_notes)})
    base, head = history.preparation()
    with pytest.raises(ValueError):
        beta.validate_preparation(base, head)


@pytest.mark.parametrize(
    "version,tag",
    [
        ("26.32.0", "26.32.0"),
        ("26.32.0b5", "26.32.0-beta.5"),
        ("26.33.0b1", "26.33.0-beta.1"),
        ("26.32.0b2", "26.32.0-beta.2"),
    ],
)
def test_validation_rejects_stable_skipped_or_older_beta(history, version, tag):
    base = history.head
    head = history.commit(
        {
            VERSION_PATH: ("100644", version_source(version)),
            f"docs/release-notes/{tag}/en.md": ("100644", b""),
        }
    )
    with pytest.raises(ValueError):
        beta.validate_preparation(base, head)


def test_validation_rejects_missing_notes(history):
    base = history.head
    head = history.commit({VERSION_PATH: ("100644", version_source("26.32.0b4"))})
    with pytest.raises(ValueError):
        beta.validate_preparation(base, head)


def test_recovered_version_source_is_never_executed(history, tmp_path):
    marker = tmp_path / "executed"
    source = f'VERSION = __import__("pathlib").Path({str(marker)!r}).touch()\n'.encode()
    base, head = history.preparation(**{VERSION_PATH: ("100644", source)})
    with pytest.raises(ValueError):
        beta.validate_preparation(base, head)
    assert not marker.exists()


def quality(bucket="pass", **overrides):
    return {
        "name": "Quality Gate",
        "bucket": bucket,
        "workflow": "Quality",
        "event": "pull_request",
        **overrides,
    }


@pytest.mark.parametrize(
    "checks,ready",
    [
        ([], False),
        ([quality("pending")], False),
        ([quality("skipping")], False),
        ([quality(workflow="Impostor")], False),
        ([quality(event="push")], False),
        ([quality(name="Other check")], False),
        ([quality(), quality()], False),
        ([quality()], True),
        ([quality(), quality(name="Lint", bucket="pending")], False),
        ([quality(), quality(name="Optional platform", bucket="skipping")], True),
    ],
)
def test_required_checks_wait_for_unambiguous_quality_identity(monkeypatch, checks, ready):
    def run(arguments, **kwargs):
        assert arguments[:4] == ["gh", "pr", "checks", "7"]
        assert "--required" in arguments
        return subprocess.CompletedProcess(arguments, 8 if not ready else 0, json.dumps(checks), "")

    monkeypatch.setattr(beta.subprocess, "run", run)
    assert beta.BetaPreparation("123", "release-app[bot]").required_checks(7) is ready


@pytest.mark.parametrize("bucket", ["fail", "cancel"])
def test_any_failed_or_cancelled_required_check_stops_merge(monkeypatch, bucket):
    monkeypatch.setattr(
        beta.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [], 1, json.dumps([quality(), quality(bucket, name="Security")]), ""
        ),
    )
    with pytest.raises(ValueError):
        beta.BetaPreparation("123", "release-app[bot]").required_checks(7)


@pytest.mark.parametrize("stderr", ["no required checks reported", "no checks reported"])
def test_checks_not_yet_reported_are_pending(monkeypatch, stderr):
    monkeypatch.setattr(
        beta.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 1, "", stderr),
    )
    assert beta.BetaPreparation("123", "release-app[bot]").required_checks(7) is False


@pytest.mark.parametrize("stderr", ["HTTP 401: Bad credentials", "HTTP 403: Forbidden"])
def test_check_authentication_errors_are_terminal(monkeypatch, stderr):
    monkeypatch.setattr(
        beta.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 1, "", stderr),
    )
    with pytest.raises(RuntimeError):
        beta.BetaPreparation("123", "release-app[bot]").required_checks(7)


class GitHubFixture:
    """Fake GitHub transitions backed by actual commits, pull refs, and a bare origin."""

    def __init__(self, history: History, remote: Path, automation: beta.BetaPreparation):
        self.history, self.remote, self.automation = history, remote, automation
        self.pull = None
        self.calls = []
        self.check_heads = []
        self.checks = deque(["pass"])
        self.merge_error = None
        self.advance_on_rejection = False

    def advance_main(self, *, version_change=False):
        current = git(self.remote, "rev-parse", "refs/heads/main")
        changes = (
            {VERSION_PATH: ("100644", version_source("26.31.2"))}
            if version_change
            else {"src/solin/application.py": ("100644", b"VALUE = 2\n")}
        )
        sha = self.history.commit(changes, parent=current)
        git(self.history.repository, "push", "origin", f"{sha}:refs/heads/main")
        # commit-tree used the index; restore the checked-out preparation's index.
        git(self.history.repository, "reset", "--hard", "HEAD")
        return sha

    def __call__(self, path, *, method="GET", payload=None):
        self.calls.append((method, path, deepcopy(payload)))
        if path.startswith("pulls?"):
            query = parse_qs(urlsplit(path).query)
            assert query["state"] == ["all"]
            branch = query["head"][0].split(":", 1)[1]
            return [deepcopy(self.pull)] if self.pull and branch == self.automation.branch else []
        if method == "POST" and path == "pulls":
            assert self.pull is None
            head = git(self.remote, "rev-parse", f"refs/heads/{payload['head']}")
            self.pull = {
                "number": 7,
                "head": {
                    "ref": payload["head"],
                    "sha": head,
                    "repo": {"full_name": self.automation.repository},
                },
                "base": {"ref": "main", "repo": {"full_name": self.automation.repository}},
                "user": {"login": self.automation.bot},
                "title": payload["title"],
                "body": payload["body"],
                "draft": False,
                "state": "open",
                "merged": False,
                "merge_commit_sha": None,
                "mergeable_state": "clean",
            }
            git(self.remote, "update-ref", "refs/pull/7/head", head)
        elif path == "pulls/7" and method == "GET":
            pass
        elif path == "pulls/7/update-branch" and method == "PUT":
            assert payload["expected_head_sha"] == self.pull["head"]["sha"]
            main = git(self.remote, "rev-parse", "refs/heads/main")
            git(self.history.repository, "merge", "--no-ff", "--no-edit", main)
            head = git(self.history.repository, "rev-parse", "HEAD")
            git(
                self.history.repository,
                "push",
                "origin",
                f"HEAD:refs/heads/{self.automation.branch}",
            )
            git(self.remote, "update-ref", "refs/pull/7/head", head)
            self.pull["head"]["sha"] = head
        elif path == "pulls/7/merge" and method == "PUT":
            assert payload["sha"] == self.pull["head"]["sha"]
            assert payload["merge_method"] == "squash"
            if self.merge_error:
                error, self.merge_error = self.merge_error, None
                if self.advance_on_rejection:
                    self.advance_main()
                raise error
            repository = self.history.repository
            main = git(self.remote, "rev-parse", "refs/heads/main")
            tree = git(repository, "rev-parse", f"{payload['sha']}^{{tree}}")
            squash = git(repository, "commit-tree", tree, "-p", main, data=b"Squash beta PR\n")
            git(repository, "push", "origin", f"{squash}:refs/heads/main")
            git(self.remote, "update-ref", "-d", f"refs/heads/{self.automation.branch}")
            self.pull.update(merged=True, state="closed", merge_commit_sha=squash)
        else:
            pytest.fail(f"Unexpected API request: {method} {path}")
        return deepcopy(self.pull)


@pytest.fixture
def lifecycle(history, tmp_path, monkeypatch):
    # Start from stable to exercise the first beta and subsequent preparations.
    # Normalize native write_text endings when staging, including on Windows.
    git(history.repository, "config", "core.autocrlf", "input")
    stable = history.commit({VERSION_PATH: ("100644", version_source("26.31.1"))})
    git(history.repository, "update-ref", "refs/heads/main", stable)
    git(history.repository, "reset", "--hard", "main")
    remote = tmp_path / "origin.git"
    git(tmp_path, "init", "--quiet", "--bare", str(remote))
    git(history.repository, "remote", "add", "origin", str(remote))
    git(history.repository, "push", "--set-upstream", "origin", "main")
    automation = beta.BetaPreparation("123", "release-app[bot]")
    api = GitHubFixture(history, remote, automation)
    monkeypatch.setattr(automation, "api", api)
    monkeypatch.setattr(beta.release, "date", SimpleNamespace(today=lambda: date(2026, 10, 20)))
    ticks = count()
    monkeypatch.setattr(beta.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(beta.time, "sleep", lambda seconds: None)
    original_run = subprocess.run

    def run(arguments, **kwargs):
        if list(arguments[:3]) == ["gh", "pr", "checks"]:
            head = git(remote, "rev-parse", "refs/pull/7/head")
            api.check_heads.append(head)
            bucket = api.checks.popleft() if len(api.checks) > 1 else api.checks[0]
            return subprocess.CompletedProcess(
                arguments, 8 if bucket == "pending" else 0, json.dumps([quality(bucket)]), ""
            )
        return original_run(arguments, **kwargs)

    monkeypatch.setattr(beta.subprocess, "run", run)
    return automation, api


def test_complete_lifecycle_tags_exact_squash_and_resumes_after_branch_deletion(lifecycle, capsys):
    automation, api = lifecycle
    automation.run()
    pull = deepcopy(api.pull)
    repository, remote = api.history.repository, api.remote
    sha = pull["merge_commit_sha"]
    assert pull["merged"] and sha != pull["head"]["sha"]
    assert git(remote, "rev-parse", "refs/heads/main") == sha
    assert git(remote, "rev-parse", "refs/tags/26.32.0-beta.1^{commit}") == sha
    assert git(remote, "cat-file", "-t", "refs/tags/26.32.0-beta.1") == "tag"
    assert not git(remote, "for-each-ref", f"refs/heads/{automation.branch}")
    assert beta.version_at(sha).canonical == "26.32.0b1"
    assert (
        git(repository, "cat-file", "-s", f"{sha}:docs/release-notes/26.32.0-beta.1/en.md") == "0"
    )
    api.advance_main()
    advanced_main = git(remote, "rev-parse", "refs/heads/main")
    assert advanced_main != sha
    automation.run()
    assert git(remote, "rev-parse", "refs/heads/main") == advanced_main
    assert git(remote, "rev-parse", "refs/tags/26.32.0-beta.1^{commit}") == sha
    assert sum(method == "POST" for method, _, _ in api.calls) == 1
    assert git(repository, "status", "--porcelain") == ""
    assert "rerun the Release workflow" in capsys.readouterr().out


@pytest.mark.parametrize("race", [False, True], ids=["before-checks", "during-merge"])
def test_main_advancement_updates_expected_head_and_waits_for_fresh_checks(lifecycle, race):
    automation, api = lifecycle
    pull = automation.prepare_pull()
    original_head = pull["head"]["sha"]
    if race:
        api.merge_error = beta.GitHubError(
            "PUT",
            "pulls/7/merge",
            subprocess.CalledProcessError(1, ["gh"], stderr="gh: rejected (HTTP 405)"),
        )
        api.advance_on_rejection = True
        api.checks = deque(["pass", "pending", "pass"])
    else:
        api.advance_main()
        api.checks = deque(["pending", "pass"])
    merged = automation.merge(7, timeout=20)
    assert merged["merged"]
    updates = [payload for method, path, payload in api.calls if path.endswith("/update-branch")]
    assert updates == [{"expected_head_sha": original_head}]
    new_head = merged["head"]["sha"]
    assert new_head != original_head and api.check_heads[-2:] == [new_head, new_head]
    assert (
        git(
            api.history.repository, "show", f"{merged['merge_commit_sha']}:src/solin/application.py"
        )
        == "VALUE = 2"
    )


def test_version_change_on_main_stops_stale_preparation_without_update_or_merge(lifecycle):
    automation, api = lifecycle
    automation.prepare_pull()
    api.advance_main(version_change=True)
    with pytest.raises(ValueError, match="another release version"):
        automation.merge(7)
    assert not any(method == "PUT" for method, _, _ in api.calls)
    assert not api.check_heads


@pytest.mark.parametrize("status,advance", [(403, True), (405, False), (409, False)])
def test_merge_rejection_is_terminal_without_recoverable_base_change(lifecycle, status, advance):
    automation, api = lifecycle
    automation.prepare_pull()
    error = beta.GitHubError(
        "PUT",
        "pulls/7/merge",
        subprocess.CalledProcessError(1, ["gh"], stderr=f"gh: rejected (HTTP {status})"),
    )
    api.merge_error, api.advance_on_rejection = error, advance
    with pytest.raises(RuntimeError) as raised:
        automation.merge(7, timeout=20)
    assert raised.value is error
    assert not api.pull["merged"]


@pytest.mark.parametrize("annotated", [False, True])
def test_existing_tag_at_wrong_commit_is_never_moved(lifecycle, annotated):
    automation, api = lifecycle
    pull = automation.merge(automation.prepare_pull()["number"])
    repository = api.history.repository
    wrong = git(repository, "rev-parse", f"{pull['merge_commit_sha']}^")
    arguments = ["--annotate", "--message", "Existing tag"] if annotated else []
    git(repository, "tag", *arguments, "26.32.0-beta.1", wrong)
    git(repository, "push", "origin", "refs/tags/26.32.0-beta.1")
    before = git(api.remote, "rev-parse", "refs/tags/26.32.0-beta.1")
    with pytest.raises(ValueError, match="refusing to move"):
        automation.tag(pull)
    assert git(api.remote, "rev-parse", "refs/tags/26.32.0-beta.1") == before
    assert git(api.remote, "rev-parse", "refs/tags/26.32.0-beta.1^{commit}") == wrong


def test_closed_unmerged_preparation_is_not_recreated_or_tagged(lifecycle):
    automation, api = lifecycle
    automation.prepare_pull()
    api.pull["state"] = "closed"
    git(api.remote, "update-ref", "-d", f"refs/heads/{automation.branch}")
    with pytest.raises(ValueError, match="closed without merging"):
        automation.run()
    assert sum(method == "POST" for method, _, _ in api.calls) == 1
    assert not git(api.remote, "for-each-ref", "refs/tags")


def test_push_succeeded_but_pr_creation_failed_resumes_same_branch(lifecycle, monkeypatch):
    automation, api = lifecycle

    def unavailable(path, *, method="GET", payload=None):
        if method == "POST":
            raise RuntimeError("GitHub unavailable after branch push")
        return api(path, method=method, payload=payload)

    monkeypatch.setattr(automation, "api", unavailable)
    with pytest.raises(RuntimeError, match="unavailable"):
        automation.prepare_pull()
    pushed_head = git(api.remote, "rev-parse", f"refs/heads/{automation.branch}")
    # Re-execution starts in a fresh checkout of main, as the workflow does.
    git(api.history.repository, "checkout", "main")
    monkeypatch.setattr(automation, "api", api)
    pull = automation.prepare_pull()
    assert pull["head"]["sha"] == pushed_head
    assert beta.version_at(pushed_head).canonical == "26.32.0b1"
    assert sum(method == "POST" for method, _, _ in api.calls) == 1


@pytest.mark.parametrize("orphan", [False, True], ids=["open-pr", "orphan-branch"])
def test_another_dispatch_cannot_start_while_preparation_is_unfinished(
    lifecycle, monkeypatch, orphan
):
    automation, api = lifecycle
    if orphan:
        git(api.history.repository, "push", "origin", "main:refs/heads/automation/beta/999")
    else:
        automation.prepare_pull()
    other = beta.BetaPreparation("456", automation.bot)
    monkeypatch.setattr(other, "api", api)
    with pytest.raises(ValueError, match="Unfinished preparation"):
        other.prepare_pull()
    assert not git(api.remote, "for-each-ref", f"refs/heads/{other.branch}")
    assert sum(method == "POST" for method, _, _ in api.calls) == (0 if orphan else 1)


def test_next_dispatch_requires_current_beta_to_be_tagged(lifecycle, monkeypatch):
    automation, api = lifecycle
    automation.merge(automation.prepare_pull()["number"])
    other = beta.BetaPreparation("456", automation.bot)
    monkeypatch.setattr(other, "api", api)
    with pytest.raises(ValueError, match="not tagged"):
        other.prepare_pull()
    assert not git(api.remote, "for-each-ref", f"refs/heads/{other.branch}")
    assert sum(method == "POST" for method, _, _ in api.calls) == 1


def test_next_dispatch_tags_merged_fix_independently_of_publication(lifecycle, monkeypatch):
    automation, api = lifecycle
    automation.run()
    first_sha = api.pull["merge_commit_sha"]
    api.advance_main()
    base = git(api.remote, "rev-parse", "refs/heads/main")
    other = beta.BetaPreparation("456", automation.bot)
    next_api = GitHubFixture(api.history, api.remote, other)
    monkeypatch.setattr(other, "api", next_api)

    def publication_unavailable():
        pytest.fail("Beta preparation must not depend on publication state or availability")

    monkeypatch.setattr(beta.release, "published_releases", publication_unavailable)
    other.run()

    second_sha = next_api.pull["merge_commit_sha"]
    assert next_api.pull["merged"] and second_sha != first_sha
    assert git(api.remote, "rev-parse", "refs/heads/main") == second_sha
    assert git(api.remote, "rev-parse", "refs/tags/26.32.0-beta.1^{commit}") == first_sha
    assert git(api.remote, "rev-parse", "refs/tags/26.32.0-beta.2^{commit}") == second_sha
    assert git(api.history.repository, "rev-parse", f"{second_sha}^") == base
    assert beta.version_at(second_sha).canonical == "26.32.0b2"
    assert (
        git(api.history.repository, "show", f"{second_sha}:src/solin/application.py")
        == "VALUE = 2"
    )


@pytest.mark.parametrize("tag", ["26.31.1", "26.32.0-beta.1"])
@pytest.mark.parametrize("annotated", [False, True], ids=["lightweight", "annotated"])
def test_dispatch_does_not_prepare_when_main_has_release_tag(lifecycle, capsys, tag, annotated):
    automation, api = lifecycle
    repository = api.history.repository
    version = beta.release.ReleaseVersion.from_tag(tag)
    main = api.history.commit({VERSION_PATH: ("100644", version_source(version.canonical))})
    git(repository, "reset", "--hard", main)
    git(repository, "push", "origin", f"{main}:refs/heads/main")
    arguments = ["--annotate", "--message", tag] if annotated else []
    git(repository, "tag", *arguments, tag, main)
    git(repository, "push", "origin", f"refs/tags/{tag}")
    before = git(api.remote, "for-each-ref", "refs/heads", "refs/tags")

    automation.run()

    assert git(api.remote, "for-each-ref", "refs/heads", "refs/tags") == before
    assert api.pull is None and not any(method != "GET" for method, _, _ in api.calls)
    assert git(repository, "status", "--porcelain") == ""
    assert "main already has a release tag" in capsys.readouterr().out


def test_pending_checks_timeout_leaves_existing_pr_resumable(lifecycle):
    automation, api = lifecycle
    pull = automation.prepare_pull()
    api.checks = deque(["pending"])
    with pytest.raises(TimeoutError, match="resume the same PR"):
        automation.merge(7, timeout=4)
    assert not api.pull["merged"]
    assert automation.prepare_pull()["head"]["sha"] == pull["head"]["sha"]
    assert not any(path.endswith("/merge") for _, path, _ in api.calls)
    assert sum(method == "POST" for method, _, _ in api.calls) == 1
