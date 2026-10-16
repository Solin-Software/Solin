"""GitHub contributor credits for an explicit, verified release commit range."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
import html
import json
from pathlib import Path
import re
import subprocess
from typing import Protocol

from solin.core.releases.version import ReleaseVersion

# Last externally distributed stable release before GitHub publication began.
HISTORICAL_BASE = "2209281ebe91c9f8872132d4f3aafdf1ba0c012b"
START = "<!-- solin-contributors:start -->"
END = "<!-- solin-contributors:end -->"
_SHA = re.compile(r"[0-9a-f]{40}")
_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]+")
_PREFIX = re.compile(r"^[a-z][a-z0-9]*(?:\([^()\r\n]*\))?!?:\s*")
_REFERENCE = re.compile(
    rf"https?://[^\s<>()\"'`\[\]]+|(?<![\w/&#])(?:(?P<repo>{_REPOSITORY.pattern}))?"
    r"#(?P<number>[1-9][0-9]{0,9})(?![\w/])",
    re.IGNORECASE,
)
_ISSUE_URL = re.compile(
    rf"https://github\.com/(?P<repo>{_REPOSITORY.pattern})/issues/"
    r"(?P<number>[1-9][0-9]{0,9})(?:[?#].*)?",
    re.IGNORECASE,
)
_PAGE = "pageInfo { hasNextPage endCursor }"
_LABELS_PATH = Path(__file__).resolve().parents[1] / ".github" / "labels.json"
_FIELDS = {
    "authors": "nodes { user { login __typename } }",
    "associatedPullRequests": (
        "nodes { number title merged mergeCommit { oid } author { login __typename } "
        "repository { nameWithOwner } }"
    ),
}
_PULL_FIELDS = {
    "closingIssuesReferences": "nodes { number repository { nameWithOwner } }",
    "labels": "nodes { name }",
}


def _release_rules() -> tuple[dict[str, str], frozenset[str]]:
    """Read category priority and unconditional exclusions from the shared catalog.

    The shared catalog defines priority (bug > enhancement > github_actions >
    dependencies > translations > documentation) and section order. Unmatched
    PRs and direct commits belong to Other changes.
    """
    catalog = json.loads(_LABELS_PATH.read_text(encoding="utf-8"))
    if not isinstance(catalog, list):
        raise ValueError("Invalid release label catalog")
    groups: dict[str, str] = {}
    excluded: set[str] = set()
    names: set[str] = set()
    for entry in catalog:
        if not isinstance(entry, dict):
            raise ValueError("Invalid release label catalog entry")
        name = entry.get("name")
        heading = entry.get("release_heading")
        if not isinstance(name, str) or not name.strip() or name.casefold() in names:
            raise ValueError("Invalid or duplicate release label name")
        names.add(name.casefold())
        exclude = entry.get("release_exclude", False)
        if not isinstance(exclude, bool) or (exclude and heading is not None):
            raise ValueError("Invalid release label exclusion")
        if exclude:
            excluded.add(name.casefold())
            continue
        if heading is None:
            continue
        if not isinstance(heading, str) or not heading.strip() or len(heading.splitlines()) != 1:
            raise ValueError("Invalid release label heading")
        groups[name.casefold()] = heading
    return groups, frozenset(excluded)


class GitHubAPI(Protocol):
    def __call__(
        self, endpoint: str, *, method: str = "GET", payload: dict | None = None
    ) -> object: ...


@dataclass(frozen=True)
class ContributionRange:
    base: str
    head: str
    commits: tuple[str, ...]


@dataclass(frozen=True)
class RelatedIssue:
    reference: str
    url: str


@dataclass(frozen=True)
class Change:
    title: str
    reference: str
    url: str
    logins: tuple[str, ...]
    issues: tuple[RelatedIssue, ...] = ()
    labels: tuple[str, ...] = ()

    def markdown(self) -> str:
        title = _PREFIX.sub("", self.title).strip() or self.title.strip()
        title = " ".join(title.split())
        title = title[:1].upper() + title[1:]
        repository = "/".join(self.url.split("/")[3:5])
        mentioned = _references(title, repository)
        # Titles are untrusted prose, not Markdown or an account attribution source.
        title = html.escape(title, quote=False)
        title = re.sub(r"([\\`*_\[\]~])", r"\\\1", title)
        # GitHub links mentions even when '@' is an entity or Markdown-escaped.
        # Render title handles as literal code; only verified authors get mentions.
        title = re.sub(r"@([A-Za-z0-9][A-Za-z0-9_/-]*)", r"<code>&#64;\1</code>", title)
        text = f"- {title}: [{self.reference}]({self.url})"
        if self.logins:
            text += ", by " + " and ".join(f"@{login}" for login in self.logins)
        text += "."
        issues = tuple(
            issue
            for issue in self.issues
            if not mentioned.keys() & _references(issue.url, repository).keys()
        )
        if issues:
            text += " Related " + ("issue: " if len(issues) == 1 else "issues: ")
            text += ", ".join(f"[{issue.reference}]({issue.url})" for issue in issues) + "."
        return text


@dataclass(frozen=True)
class Credits:
    interval: ContributionRange
    changes: tuple[Change, ...]
    unlinked_commits: tuple[str, ...]

    @property
    def logins(self) -> tuple[str, ...]:
        accounts: dict[str, str] = {}
        for change in self.changes:
            for login in change.logins:
                accounts.setdefault(login.lower(), login)
        return tuple(accounts[key] for key in sorted(accounts))

    def markdown(self) -> str:
        text = ""
        if self.changes:
            groups, excluded = _release_rules()
            sections: dict[str, list[str]] = {heading: [] for heading in groups.values()}
            sections.setdefault("Other changes", [])
            for change in self.changes:
                labels = {label.casefold() for label in change.labels}
                if labels & excluded:
                    continue
                heading = next(
                    (heading for label, heading in groups.items() if label in labels), "Other changes"
                )
                sections[heading].append(change.markdown())
            if any(sections.values()):
                text = "### Related pull requests and issues\n\n" + "".join(
                    f"#### {heading}\n\n" + "\n".join(rows) + "\n\n"
                    for heading, rows in sections.items()
                    if rows
                )
        return (
            f"{START}\n{text}"
            f"<!-- contribution-range: {self.interval.base}..{self.interval.head} -->\n{END}"
        )


def contribution_range(
    version: ReleaseVersion,
    head: str,
    releases: list[dict],
    git: Callable[..., str],
    *,
    recorded_base: str | None = None,
) -> ContributionRange:
    if not _SHA.fullmatch(head):
        raise ValueError("Invalid contribution target SHA")
    # The migration boundary must be reachable; never fall back to all history.
    git("merge-base", "--is-ancestor", HISTORICAL_BASE, head)
    candidates = []
    for release in releases:
        previous = ReleaseVersion.from_tag(str(release.get("tag_name", "")))
        if (
            release.get("draft") is False
            and previous is not None
            and release.get("prerelease") is not previous.is_beta
        ):
            raise ValueError("Published release channel differs from its tag")
        if (
            release.get("draft") is False
            and previous is not None
            and previous < version
            and release.get("prerelease") is previous.is_beta
            and (version.is_beta or not previous.is_beta)
        ):
            candidates.append(previous)
    base = HISTORICAL_BASE
    if recorded_base is not None:
        base = recorded_base
    elif candidates:
        previous = max(candidates)
        base = git("rev-parse", f"refs/tags/{previous.tag}^{{commit}}")
    if base != HISTORICAL_BASE:
        if not _SHA.fullmatch(base):
            raise ValueError("Invalid previous release SHA")
        # An inconsistent history is an error, not a reason to guess a range.
        git("merge-base", "--is-ancestor", HISTORICAL_BASE, base)
        git("merge-base", "--is-ancestor", base, head)
    commits = tuple(git("rev-list", "--reverse", f"{base}..{head}").splitlines())
    if any(not _SHA.fullmatch(sha) for sha in commits) or len(set(commits)) != len(commits):
        raise ValueError("Invalid contribution commit range")
    return ContributionRange(base, head, commits)


def _repository_data(api: GitHubAPI, repository: str, fields: str) -> dict:
    owner, name = repository.split("/", 1)
    query = "query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { "
    result = api(
        "graphql",
        method="POST",
        payload={
            "query": query + fields + " } }",
            "variables": {"owner": owner, "name": name},
        },
    )
    if not isinstance(result, dict) or result.get("errors"):
        raise ValueError("GitHub contributor query failed")
    data = result.get("data")
    repo = data.get("repository") if isinstance(data, dict) else None
    if not isinstance(repo, dict):
        raise ValueError("Missing GitHub contributor repository")
    return repo


def _connection_nodes(connection: object, fetch_page: Callable[[str], object]) -> Iterator[dict]:
    cursors: set[str] = set()
    while True:
        if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
            raise ValueError("Invalid GitHub contributor connection")
        for node in connection["nodes"]:
            if not isinstance(node, dict):
                raise ValueError("Invalid GitHub contributor node")
            yield node
        page = connection.get("pageInfo")
        if not isinstance(page, dict) or not isinstance(page.get("hasNextPage"), bool):
            raise ValueError("Invalid GitHub contributor pagination")
        if not page["hasNextPage"]:
            return
        cursor = page.get("endCursor")
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise ValueError("GitHub contributor pagination did not advance")
        cursors.add(cursor)
        connection = fetch_page(cursor)


def _nodes(
    api: GitHubAPI, repository: str, sha: str, field: str, connection: object
) -> Iterator[dict]:
    def fetch_page(cursor: str) -> object:
        fields = (
            f'commit: object(expression: "{sha}") {{ ... on Commit {{ oid '
            f"{field}(first: 100, after: {json.dumps(cursor)}) "
            f"{{ {_FIELDS[field]} {_PAGE} }} }} }}"
        )
        commit = _repository_data(api, repository, fields).get("commit")
        if not isinstance(commit, dict) or commit.get("oid") != sha:
            raise ValueError("GitHub contributor commit identity mismatch")
        return commit.get(field)

    return _connection_nodes(connection, fetch_page)


def _human_login(account: object) -> str | None:
    if account is None:
        return None
    if not isinstance(account, dict):
        raise ValueError("Invalid GitHub contributor account")
    login = account.get("login")
    kind = account.get("__typename")
    if kind == "Bot" or (isinstance(login, str) and login.lower().endswith("[bot]")):
        return None
    if kind != "User" or not isinstance(login, str) or not _LOGIN.fullmatch(login):
        raise ValueError("Invalid GitHub contributor login")
    return login


def _repository_name(value: object) -> str:
    name = value.get("nameWithOwner") if isinstance(value, dict) else None
    if (
        not isinstance(name, str)
        or not _REPOSITORY.fullmatch(name)
        or name.split("/", 1)[1] in {".", ".."}
    ):
        raise ValueError("Invalid GitHub reference repository")
    return name


def _number(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("Invalid GitHub reference number")
    return value


def _title(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Invalid GitHub change title")
    return value


def _references(text: str, repository: str) -> dict[tuple[str, int], str]:
    references: dict[tuple[str, int], str] = {}
    for match in _REFERENCE.finditer(text):
        if match["number"] is None:
            match = _ISSUE_URL.fullmatch(match[0].rstrip(".,;:!?"))
            if match is None:
                continue
        repo = match["repo"] or repository
        if repo.split("/", 1)[1] not in {".", ".."}:
            references.setdefault((repo.lower(), int(match["number"])), repo)
    return references


def _referenced_issue(api: GitHubAPI, repository: str, number: int) -> RelatedIssue | None:
    try:
        issue = api(f"repos/{repository}/issues/{number}")
    except subprocess.CalledProcessError as error:
        # A stale reference is harmless; authentication and transport failures are not.
        try:
            response = json.loads(error.stdout or "null")
        except (ValueError, TypeError):
            raise error from None
        if isinstance(response, dict) and str(response.get("status")) == "404":
            return None
        raise
    if not isinstance(issue, dict):
        raise ValueError("GitHub referenced issue identity mismatch")
    number = _number(issue.get("number"))
    # GitHub issues and pull requests share the same number namespace and REST endpoint.
    if "pull_request" in issue:
        return None
    url = issue.get("html_url")
    if not isinstance(url, str):
        raise ValueError("GitHub referenced issue URL mismatch")
    canonical = _ISSUE_URL.fullmatch(url)
    if canonical is None or canonical["number"] != str(number):
        raise ValueError("GitHub referenced issue URL mismatch")
    repository = _repository_name({"nameWithOwner": canonical["repo"]})
    if url.lower() != f"https://github.com/{repository}/issues/{number}".lower():
        raise ValueError("GitHub referenced issue URL mismatch")
    # The issue endpoint follows transfers; use GitHub's canonical repository and number.
    return RelatedIssue(f"{repository}#{number}", url)


@dataclass(frozen=True)
class _Pull:
    number: int
    title: str
    sha: str
    login: str | None


def _pull_details(
    api: GitHubAPI, repository: str, pull: _Pull
) -> tuple[tuple[RelatedIssue, ...], tuple[str, ...]]:
    # Separate connections keep worst-case GraphQL node counts bounded. Nested
    # commit -> PR -> issue pagination exceeds GitHub's node limit.
    references = _references(pull.title, repository)

    def fetch_page(field: str | None = None, cursor: str | None = None) -> dict:
        after = f", after: {json.dumps(cursor)}" if cursor is not None else ""
        connections = " ".join(
            f"{name}(first: 100{after}) {{ {nodes} {_PAGE} }}"
            for name, nodes in _PULL_FIELDS.items()
            if field is None or field == name
        )
        fields = (
            f"pullRequest(number: {pull.number}) {{ number body merged mergeCommit {{ oid }} "
            f"{connections} }}"
        )
        node = _repository_data(api, repository, fields).get("pullRequest")
        merge = node.get("mergeCommit") if isinstance(node, dict) else None
        if (
            not isinstance(node, dict)
            or _number(node.get("number")) != pull.number
            or node.get("merged") is not True
            or not isinstance(merge, dict)
            or merge.get("oid") != pull.sha
        ):
            raise ValueError("GitHub related pull request identity mismatch")
        body = node.get("body")
        if not isinstance(body, str):
            raise ValueError("Invalid GitHub pull request body")
        references.update(_references(body, repository))
        return node

    initial = fetch_page()

    def nodes(field: str) -> Iterator[dict]:
        return _connection_nodes(
            initial.get(field), lambda cursor: fetch_page(field, cursor).get(field)
        )

    result: dict[tuple[str, int], RelatedIssue] = {}
    for issue in nodes("closingIssuesReferences"):
        number = _number(issue.get("number"))
        issue_repo = _repository_name(issue.get("repository"))
        key = (issue_repo.lower(), number)
        if key in result:
            continue
        reference = (
            f"#{number}" if issue_repo.lower() == repository.lower() else f"{issue_repo}#{number}"
        )
        result[key] = RelatedIssue(reference, f"https://github.com/{issue_repo}/issues/{number}")
    labels: set[str] = set()
    for label in nodes("labels"):
        name = label.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Invalid GitHub pull request label name")
        labels.add(name.casefold())
    for key, issue_repo in references.items():
        if key in result:
            continue
        issue = _referenced_issue(api, issue_repo, key[1])
        if issue is not None:
            canonical_key = next(iter(_references(issue.url, repository)))
            reference = (
                f"#{canonical_key[1]}"
                if canonical_key[0] == repository.lower()
                else issue.reference
            )
            result.setdefault(canonical_key, RelatedIssue(reference, issue.url))
    return tuple(result[key] for key in sorted(result)), tuple(sorted(labels))


def collect_credits(interval: ContributionRange, repository: str, api: GitHubAPI) -> Credits:
    included = set(interval.commits)
    unlinked: set[str] = set()
    commits: dict[str, tuple[str, dict[str, str]]] = {}
    pulls: dict[int, _Pull] = {}
    associations: dict[int, set[str]] = {}
    # Batch commit lookups, without guessing GitHub identities from email addresses.
    for offset in range(0, len(interval.commits), 20):
        batch = interval.commits[offset : offset + 20]
        fields = " ".join(
            f'c{index}: object(expression: "{sha}") {{ ... on Commit {{ oid messageHeadline '
            + " ".join(
                f"{field}(first: 100) {{ {nodes} {_PAGE} }}" for field, nodes in _FIELDS.items()
            )
            + " } }"
            for index, sha in enumerate(batch)
        )
        data = _repository_data(api, repository, fields)
        for index, sha in enumerate(batch):
            commit = data.get(f"c{index}")
            if not isinstance(commit, dict) or commit.get("oid") != sha:
                raise ValueError("GitHub contributor commit identity mismatch")
            title = _title(commit.get("messageHeadline"))
            authors: dict[str, str] = {}
            for author in _nodes(api, repository, sha, "authors", commit.get("authors")):
                if "user" not in author:
                    raise ValueError("Missing GitHub commit author")
                if author["user"] is None:
                    unlinked.add(sha)
                if login := _human_login(author["user"]):
                    authors.setdefault(login.lower(), login)
            commits[sha] = (title, authors)
            for pull in _nodes(
                api, repository, sha, "associatedPullRequests", commit.get("associatedPullRequests")
            ):
                merge = pull.get("mergeCommit")
                pull_repo = pull.get("repository")
                repo_name = pull_repo.get("nameWithOwner") if isinstance(pull_repo, dict) else None
                if (
                    type(pull.get("merged")) is not bool
                    or not isinstance(repo_name, str)
                    or not repo_name
                    or "author" not in pull
                    or "mergeCommit" not in pull
                ):
                    raise ValueError("Invalid GitHub pull request attribution")
                merge_sha = merge.get("oid") if isinstance(merge, dict) else None
                if (
                    merge is not None
                    and (not isinstance(merge_sha, str) or not _SHA.fullmatch(merge_sha))
                ) or (pull["merged"] and merge_sha is None):
                    raise ValueError("Invalid GitHub pull request merge identity")
                # A PR merely associated with a commit may be open or merged elsewhere.
                if (
                    pull.get("merged") is True
                    and merge_sha in included
                    and repo_name.lower() == repository.lower()
                ):
                    number = _number(pull.get("number"))
                    candidate = _Pull(
                        number, _title(pull.get("title")), merge_sha, _human_login(pull["author"])
                    )
                    if number in pulls and pulls[number] != candidate:
                        raise ValueError("GitHub pull request changed during contributor lookup")
                    pulls[number] = candidate
                    associations.setdefault(number, set()).add(sha)

    changes: list[Change] = []
    associated = set().union(*associations.values()) if associations else set()
    for sha in interval.commits:
        for pull in sorted(
            (pull for pull in pulls.values() if pull.sha == sha), key=lambda pull: pull.number
        ):
            authors = {}
            if pull.login is not None:
                authors[pull.login.lower()] = pull.login
            # Include authors of every included commit associated with this PR,
            # not just its merge commit (merge and rebase histories preserve them).
            for commit_sha in interval.commits:
                if commit_sha in associations[pull.number] or commit_sha == pull.sha:
                    for key, login in commits[commit_sha][1].items():
                        authors.setdefault(key, login)
            issues, labels = _pull_details(api, repository, pull)
            changes.append(
                Change(
                    pull.title,
                    f"PR #{pull.number}",
                    f"https://github.com/{repository}/pull/{pull.number}",
                    tuple(authors.values()),
                    issues,
                    labels,
                )
            )
        if sha not in associated and not any(pull.sha == sha for pull in pulls.values()):
            title, authors = commits[sha]
            changes.append(
                Change(
                    title,
                    f"Commit {sha[:7]}",
                    f"https://github.com/{repository}/commit/{sha}",
                    tuple(authors.values()),
                )
            )
    return Credits(interval, tuple(changes), tuple(sorted(unlinked)))


def _block_bounds(body: str) -> tuple[int, int] | None:
    start_count, end_count = body.count(START), body.count(END)
    if (start_count, end_count) == (0, 0):
        return None
    if (start_count, end_count) != (1, 1) or body.index(START) > body.index(END):
        raise ValueError("Malformed managed contributor block")
    return body.index(START), body.index(END) + len(END)


def recorded_base(body: str, head: str) -> str | None:
    bounds = _block_bounds(body)
    if bounds is None:
        return None
    block = body[bounds[0] : bounds[1]]
    ranges = re.findall(r"<!-- contribution-range: ([a-f0-9]{40})\.\.([a-f0-9]{40}) -->", block)
    if len(ranges) != 1 or block.count("<!-- contribution-range:") != 1 or ranges[0][1] != head:
        raise ValueError("Managed contribution range is missing or differs from the target")
    return ranges[0][0]


def update_body(body: str, credits: Credits) -> str:
    """Replace only the owned block; preserve all other release text byte for byte."""
    bounds = _block_bounds(body)
    if bounds is None:
        separator = "\n\n" if not body.endswith("\n") else "\n"
        return body + separator + credits.markdown() + "\n"
    start, end = bounds
    return body[:start] + credits.markdown() + body[end:]
