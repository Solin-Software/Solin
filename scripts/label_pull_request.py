"""Classify pull requests while preserving explicit label decisions."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import quote

CATALOG = Path(__file__).resolve().parents[1] / ".github" / "labels.json"
AUTOMATION_LOGIN = "github-actions[bot]"
_TITLE = re.compile(r"^(?P<type>[a-z][a-z0-9]*)(?:\((?P<scope>[^()\r\n]+)\))?!?:[^\S\r\n]+\S")
_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]+")


def suggested_labels(title: str, files: list[dict], rules: list[dict]) -> set[str]:
    prefix = _TITLE.match(title)
    kind = prefix["type"] if prefix else None
    scope = prefix["scope"] if prefix else None
    paths = {
        path
        for entry in files
        for path in (entry["filename"], entry.get("previous_filename"))
        if path is not None
    }
    return {
        rule["name"]
        for rule in rules
        if kind in rule["title_types"]
        or (rule["name"] == "dependencies" and scope in {"deps", "deps-dev"})
        or any(fnmatchcase(path, pattern) for path in paths for pattern in rule["files"])
    }


@dataclass(frozen=True)
class LabelUpdate:
    add: tuple[str, ...]
    remove: tuple[str, ...]


def manual_decisions(events: list[dict], managed: set[str]) -> dict[str, bool]:
    decisions: dict[str, bool] = {}
    for event in events:
        if event["event"] in {"labeled", "unlabeled"}:
            name = event["label"]["name"].casefold()
            if name in managed and (event.get("actor") or {}).get("login") != AUTOMATION_LOGIN:
                decisions[name] = event["event"] == "labeled"
    return decisions


def label_update(
    suggested: set[str], current: set[str], events: list[dict], managed: set[str],
) -> LabelUpdate:
    suggested = {name.casefold() for name in suggested}
    current = {name.casefold() for name in current}
    managed = {name.casefold() for name in managed}
    manual = manual_decisions(events, managed).keys()
    automatic: set[str] = set()
    for event in events:
        if event["event"] not in {"labeled", "unlabeled"}:
            continue
        name = event["label"]["name"].casefold()
        if name not in managed:
            continue
        if (event.get("actor") or {}).get("login") != AUTOMATION_LOGIN:
            continue
        if event["event"] == "labeled":
            automatic.add(name)
        else:
            automatic.discard(name)
    # A human or another integration owns any label it has explicitly touched.
    return LabelUpdate(
        tuple(sorted(suggested - current - manual)),
        tuple(sorted((current & automatic) - suggested - manual)),
    )


def restore_concurrent_decisions(api: Callable, root: str, number: int, touched: set[str]) -> set[str]:
    """Restore manual choices made between the last read and a label mutation."""
    for attempt in range(4):
        decisions = manual_decisions(list(pages(api, f"{root}/issues/{number}/events")), touched)
        if not decisions:
            return set()
        pull = api(f"{root}/pulls/{number}")
        current = {label["name"].casefold() for label in pull["labels"]}
        add = sorted(name for name, present in decisions.items() if present and name not in current)
        remove = sorted(name for name, present in decisions.items() if not present and name in current)
        if not add and not remove:
            return set(decisions)
        if attempt == 3:
            raise RuntimeError("Labels kept changing during reconciliation; retry the workflow")
        if add:
            api(f"{root}/issues/{number}/labels", method="POST", payload={"labels": add})
        for name in remove:
            api(f"{root}/issues/{number}/labels/{quote(name, safe='')}", method="DELETE")
    raise AssertionError("Unreachable reconciliation state")


def gh_api(endpoint: str, *, method: str = "GET", payload: dict | None = None) -> object:
    command = ["gh", "api", endpoint, "--method", method,
               "-H", "Accept: application/vnd.github+json"]
    if payload is not None:
        command += ["--input", "-"]
    result = subprocess.run(
        command, input=json.dumps(payload) if payload is not None else None,
        capture_output=True, text=True, encoding="utf-8", check=True, timeout=60,
    )
    return json.loads(result.stdout) if result.stdout.strip() else None


def pages(api: Callable, endpoint: str) -> Iterator[dict]:
    page = 1
    while True:
        batch = api(f"{endpoint}?per_page=100&page={page}")
        if not isinstance(batch, list) or any(not isinstance(item, dict) for item in batch):
            raise ValueError(f"Invalid GitHub listing: {endpoint}")
        yield from batch
        if len(batch) < 100:
            return
        page += 1


def apply_labels(repository: str, number: int, rules: list[dict], api: Callable = gh_api) -> LabelUpdate:
    if _REPOSITORY.fullmatch(repository) is None or number <= 0:
        raise ValueError("Invalid pull request identity")
    root = f"repos/{repository}"
    pull = api(f"{root}/pulls/{number}")
    if not isinstance(pull, dict):
        raise ValueError("Invalid GitHub pull request")
    if pull["state"] != "open":
        return LabelUpdate((), ())
    files = list(pages(api, f"{root}/pulls/{number}/files"))
    if len(files) != pull["changed_files"]:
        raise ValueError("Incomplete pull request file listing; labels were not changed")
    events = list(pages(api, f"{root}/issues/{number}/events"))
    suggested = suggested_labels(pull["title"], files, rules)
    managed = {rule["name"] for rule in rules}
    update = label_update(
        suggested, {label["name"] for label in pull["labels"]}, events, managed,
    )
    if update.add:
        available = {label["name"].casefold() for label in pages(api, f"{root}/labels")}
        for rule in rules:
            if rule["name"].casefold() in update.add and rule["name"].casefold() not in available:
                try:
                    api(f"{root}/labels", method="POST", payload={
                        key: rule[key] for key in ("name", "color", "description")
                    })
                except subprocess.CalledProcessError as error:
                    # Different PRs can need the same new label concurrently.
                    # Only accept a validation conflict if the label now exists.
                    if "HTTP 422" not in (error.stderr or ""):
                        raise
                    existing = api(f"{root}/labels/{quote(rule['name'], safe='')}")
                    if (not isinstance(existing, dict)
                            or existing.get("name", "").casefold() != rule["name"].casefold()):
                        raise error
    if not update.add and not update.remove:
        # Also repair a prior run interrupted after a concurrent human edit.
        restore_concurrent_decisions(api, root, number, managed)
        return update
    # Repository label discovery/creation may take time. Recheck human choices
    # immediately before mutations; the final pass handles the remaining API race.
    current = api(f"{root}/pulls/{number}")
    if current["state"] != "open":
        return LabelUpdate((), ())
    events = list(pages(api, f"{root}/issues/{number}/events"))
    update = label_update(suggested, {label["name"] for label in current["labels"]}, events, managed)
    if update.add:
        api(f"{root}/issues/{number}/labels", method="POST", payload={"labels": list(update.add)})
    for name in update.remove:
        api(f"{root}/issues/{number}/labels/{quote(name, safe='')}", method="DELETE")
    manual = restore_concurrent_decisions(api, root, number, managed)
    return LabelUpdate(tuple(name for name in update.add if name not in manual),
                       tuple(name for name in update.remove if name not in manual))


def main() -> None:
    rules = json.loads(CATALOG.read_text(encoding="utf-8"))
    update = apply_labels(os.environ["GITHUB_REPOSITORY"], int(os.environ["PR_NUMBER"]), rules)
    print(json.dumps({"added": update.add, "removed": update.remove}))


if __name__ == "__main__":
    main()
