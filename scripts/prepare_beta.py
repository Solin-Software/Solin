"""Orchestrate a resumable beta preparation PR without bypassing repository rules."""

from __future__ import annotations

import argparse
import ast
from collections.abc import Mapping
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import release  # noqa: E402
from scripts.classify_quality_changes import VERSION_PATH, classify_changes  # noqa: E402

PREFIX = "automation/beta/"
SHA = re.compile(r"[a-f0-9]{40}")
LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")


class GitHubError(RuntimeError):
    def __init__(self, method: str, path: str, error: subprocess.CalledProcessError):
        detail = error.stderr or str(error)
        match = re.search(r"\(HTTP ([0-9]{3})\)", detail)
        self.status = int(match[1]) if match else None
        super().__init__(f"GitHub {method} {path} failed: {detail}")


def authorize(environment: Mapping[str, str]) -> None:
    if (environment.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or environment.get("GITHUB_REF") != "refs/heads/main"):
        raise ValueError("Generate beta must be dispatched from main")
    actors = re.split(r"[\s,]+", environment.get("BETA_RELEASE_ACTORS", "").strip())
    if not actors or any(LOGIN.fullmatch(actor) is None for actor in actors):
        raise ValueError("Configure BETA_RELEASE_ACTORS with the allowed GitHub usernames")
    allowed = {actor.casefold() for actor in actors}
    for key in ("GITHUB_ACTOR", "BETA_RELEASE_TRIGGERING_ACTOR"):
        actor = environment.get(key, "")
        if not actor or actor.casefold() not in allowed:
            raise ValueError(f"{key} is not authorized to generate betas")


def command(*arguments: str) -> str:
    return subprocess.check_output(arguments, cwd=ROOT, text=True, encoding="utf-8").strip()


def git(*arguments: str) -> str:
    return command("git", *arguments)


def version_at(revision: str) -> release.ReleaseVersion:
    """Read remote version data without executing recovered branch code."""
    tree = ast.parse(git("show", f"{revision}:{VERSION_PATH}"))
    assignments = [
        node.value for node in tree.body if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets)
    ]
    if len(assignments) != 1:
        raise ValueError("Expected one canonical VERSION assignment")
    value = ast.literal_eval(assignments[0])
    version = release.ReleaseVersion.parse(value) if isinstance(value, str) else None
    if version is None or version.canonical != value:
        raise ValueError("Invalid canonical VERSION")
    return version


def validate_preparation(base: str, head: str) -> release.ReleaseVersion:
    for revision in (base, head):
        if SHA.fullmatch(revision) is None:
            raise ValueError("Invalid preparation commit")
    version = version_at(head)
    previous = version_at(base)
    if not version.is_beta or release.calculate_next(previous, "beta", version.base[0]) != version:
        raise ValueError("Preparation must contain exactly the next beta")
    notes = f"docs/release-notes/{version.tag}/en.md"
    paths = set(git("diff", "--no-renames", "--name-only", base, head).splitlines())
    changes = classify_changes(base, head, "branch", repository=ROOT)
    if paths != {VERSION_PATH, notes} or not changes.release_preparation or changes.application_required:
        raise ValueError("Beta preparation contains changes outside its version and notes")
    entries = git("ls-tree", head, "--", notes).split()
    if len(entries) != 4 or entries[:2] != ["100644", "blob"]:
        raise ValueError("Beta notes must be a regular file")
    # Check blob size, not stripped text: automatic preparation creates a zero-byte file.
    if git("cat-file", "-s", f"{head}:{notes}") != "0":
        raise ValueError("Automatic beta preparation requires empty English notes")
    if git("ls-tree", base, "--", notes):
        raise ValueError("Beta notes already existed before preparation")
    return version


class BetaPreparation:
    def __init__(self, run_id: str, bot: str):
        if not re.fullmatch(r"[1-9][0-9]*", run_id) or not re.fullmatch(
            r"[A-Za-z0-9-]+\[bot\]", bot,
        ):
            raise ValueError("Invalid workflow run or App identity")
        self.branch = PREFIX + run_id
        self.bot = bot
        self.repository = release.RELEASE_REPOSITORY
        self.endpoint = f"repos/{self.repository}"
        self.marker = f"<!-- solin-beta-run:{run_id} -->"

    def api(self, path: str, *, method: str = "GET", payload: dict | None = None):
        try:
            return release.gh_api(f"{self.endpoint}/{path}", method=method, payload=payload)
        except subprocess.CalledProcessError as error:
            raise GitHubError(method, path, error) from error

    def pulls(self, branch: str) -> list[dict]:
        query = urlencode({"state": "all", "head": f"{self.repository.split('/')[0]}:{branch}",
                           "per_page": 100})
        pulls = self.api(f"pulls?{query}")
        if not isinstance(pulls, list) or len(pulls) > 1:
            raise ValueError("Expected at most one PR for the preparation branch")
        return pulls

    def refresh_main(self) -> str:
        git("fetch", "origin", "+refs/heads/main:refs/remotes/origin/main", "--tags")
        return git("rev-parse", "origin/main")

    def validate_pull(self, pull: dict) -> None:
        if (pull["head"]["ref"] != self.branch
                or pull["head"]["repo"]["full_name"] != self.repository
                or pull["base"]["repo"]["full_name"] != self.repository
                or pull["base"]["ref"] != "main" or pull["user"]["login"] != self.bot
                or self.marker not in (pull.get("body") or "") or pull["draft"]):
            raise ValueError("PR does not belong to this automated beta preparation")

    def needs_preparation(self, main: str) -> bool:
        branches = git("ls-remote", "--heads", "origin", f"refs/heads/{PREFIX}*")
        for line in branches.splitlines():
            branch = line.split("refs/heads/", 1)[1]
            if branch != self.branch:
                pulls = self.pulls(branch)
                if not pulls or pulls[0]["state"] == "open":
                    raise ValueError(
                        f"Unfinished preparation on {branch}; rerun its Generate beta workflow first"
                    )
        remote = git("ls-remote", "--tags", "origin")
        refs = dict(line.split()[::-1] for line in remote.splitlines())
        current = version_at(main)
        if current.is_beta and f"refs/tags/{current.tag}" not in refs:
            raise ValueError("The current beta is not tagged; finish its existing release preparation")
        return not any(
            target == main and release.ReleaseVersion.from_tag(
                ref.removeprefix("refs/tags/").removesuffix("^{}")
            ) is not None
            for ref, target in refs.items()
        )

    def prepare_pull(self) -> dict | None:
        existing = self.pulls(self.branch)
        if existing:
            self.validate_pull(existing[0])
            return existing[0]
        main = self.refresh_main()
        needed = self.needs_preparation(main)
        remote = git("ls-remote", "--heads", "origin", f"refs/heads/{self.branch}")
        if remote:
            git("fetch", "origin", f"refs/heads/{self.branch}")
            head = git("rev-parse", "FETCH_HEAD")
            base = git("merge-base", main, head)
            validate_preparation(base, head)
            if version_at(main) != version_at(base):
                raise ValueError("main has another release version; abandon the stale preparation")
        else:
            if not needed:
                return None
            git("checkout", "-b", self.branch, main)
            release.prepare("beta")
            version = release.current_version()
            git("add", "--", VERSION_PATH, f"docs/release-notes/{version.tag}/en.md")
            git("commit", "-m", f"chore(release): prepare {version.tag}")
            head = git("rev-parse", "HEAD")
            validate_preparation(main, head)
            git("push", "origin", f"HEAD:refs/heads/{self.branch}")
        version = version_at(head)
        pull = self.api("pulls", method="POST", payload={
            "title": f"chore(release): prepare {version.tag}", "head": self.branch, "base": "main",
            "body": (
                f"Prepare `{version.tag}` with automatic PR and issue notes.\n\n"
                "The Generate beta workflow waits for required checks, squash-merges this PR, "
                "and tags its exact merge commit. The existing Release workflow validates, "
                "builds, and publishes all platforms.\n\n" + self.marker
            ),
        })
        self.validate_pull(pull)
        return pull

    def required_checks(self, number: int) -> bool:
        result = subprocess.run(
            ["gh", "pr", "checks", str(number), "--repo", self.repository,
             "--required", "--json", "name,bucket,workflow,event"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=False,
        )
        if (result.returncode == 1 and not result.stdout.strip()
                and re.match(r"no (?:required )?checks reported\b", result.stderr.strip(), re.I)):
            return False  # GitHub may not have registered the new revision's checks yet.
        if result.returncode not in (0, 1, 8) or not result.stdout.strip():
            raise RuntimeError(result.stderr.strip() or "Could not read required PR checks")
        checks = json.loads(result.stdout)
        quality = [item for item in checks if item["name"] == "Quality Gate"
                   and item["workflow"] == "Quality" and item["event"] == "pull_request"]
        if any(item["bucket"] in {"fail", "cancel"} for item in checks):
            raise ValueError("Required PR checks failed; inspect the PR and rerun after resolving them")
        return (len(quality) == 1 and quality[0]["bucket"] == "pass"
                and all(item["bucket"] in {"pass", "skipping"} for item in checks))

    def merge(self, number: int, *, timeout: float = 1800) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pull = self.api(f"pulls/{number}")
            self.validate_pull(pull)
            if pull["merged"]:
                return pull
            if pull["state"] != "open":
                raise ValueError("The beta PR was closed without merging")
            main = self.refresh_main()
            git("fetch", "origin", f"refs/pull/{number}/head")
            head = git("rev-parse", "FETCH_HEAD")
            if head != pull["head"]["sha"]:
                raise ValueError("Preparation head changed while reading the PR; rerun the workflow")
            base = git("merge-base", main, head)
            version = validate_preparation(base, head)
            if pull["title"] != f"chore(release): prepare {version.tag}":
                raise ValueError("PR title no longer matches the prepared beta")
            if version_at(main) != version_at(base):
                raise ValueError("main has another release version; close this stale beta PR")
            if main != base:
                self.api(f"pulls/{number}/update-branch", method="PUT",
                         payload={"expected_head_sha": head})
            elif self.required_checks(number) and pull["mergeable_state"] == "clean":
                # The expected head and strict branch rules reject concurrent changes.
                try:
                    result = self.api(f"pulls/{number}/merge", method="PUT", payload={
                        "sha": head, "merge_method": "squash", "commit_title": pull["title"],
                        "commit_message": f"Automated beta preparation (#{number}).",
                    })
                except GitHubError as error:
                    if error.status not in {405, 409}:
                        raise
                    latest = self.api(f"pulls/{number}")
                    if latest["merged"] or self.refresh_main() != main:
                        continue  # Revalidate and update after a concurrent merge/base advance.
                    raise
                if not result.get("merged"):
                    raise ValueError("GitHub did not merge the beta preparation")
                continue
            print(f"Waiting for PR #{number}: required checks and merge requirements", flush=True)
            time.sleep(15)
        raise TimeoutError("Beta preparation timed out; rerun this workflow to resume the same PR")

    def tag(self, pull: dict) -> tuple[release.ReleaseVersion, str, bool]:
        self.validate_pull(pull)
        if not pull["merged"]:
            raise ValueError("Only a merged beta preparation can be tagged")
        sha = pull["merge_commit_sha"]
        if not isinstance(sha, str) or SHA.fullmatch(sha) is None:
            raise ValueError("Invalid merge commit")
        self.refresh_main()
        git("merge-base", "--is-ancestor", sha, "origin/main")
        parents = git("rev-list", "--parents", "-n", "1", sha).split()
        if len(parents) != 2:
            raise ValueError("Expected a squash merge with one parent")
        version = validate_preparation(parents[1], sha)
        if pull["title"] != f"chore(release): prepare {version.tag}":
            raise ValueError("PR title no longer matches the prepared beta")
        remote = git("ls-remote", "--tags", "origin", f"refs/tags/{version.tag}",
                     f"refs/tags/{version.tag}^{{}}")
        if remote:
            refs = dict(line.split()[::-1] for line in remote.splitlines())
            target = refs.get(f"refs/tags/{version.tag}^{{}}", refs.get(f"refs/tags/{version.tag}"))
            if target != sha:
                raise ValueError("Existing beta tag points to another commit; refusing to move it")
            return version, sha, False
        git("tag", "--annotate", version.tag, sha, "--message", version.tag)
        git("push", "origin", f"refs/tags/{version.tag}:refs/tags/{version.tag}")
        return version, sha, True

    def run(self) -> None:
        pull = self.prepare_pull()
        if pull is None:
            summary = (
                "No beta prepared: main already has a release tag. "
                "Merge new changes before starting another preparation.\n"
            )
        else:
            pull = self.merge(pull["number"])
            version, sha, created = self.tag(pull)
            url = f"https://github.com/{self.repository}/pull/{pull['number']}"
            status = ("Tag pushed; follow the Release workflow for build and publication results."
                      if created else "Tag already exists. If publication failed, rerun the Release workflow.")
            summary = f"### {version.tag}\n\nPreparation: {url}\n\nCommit: `{sha}`\n\n{status}\n"
        print(summary)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
                stream.write(summary)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("authorize", "run"))
    args = parser.parse_args()
    authorize(os.environ)
    if args.command == "run":
        if os.environ.get("GITHUB_REPOSITORY") != release.RELEASE_REPOSITORY:
            raise ValueError("Beta automation is restricted to the release repository")
        automation = BetaPreparation(os.environ["GITHUB_RUN_ID"], os.environ["BETA_RELEASE_BOT"])
        identity = release.gh_api(f"users/{automation.bot}")
        git("config", "user.name", automation.bot)
        git("config", "user.email", f"{identity['id']}+{automation.bot}@users.noreply.github.com")
        command("gh", "auth", "setup-git")
        automation.run()


if __name__ == "__main__":
    main()
