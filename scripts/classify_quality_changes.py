"""Select Quality suites from committed changes without executing version files."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from solin.core.releases.version import ReleaseVersion  # noqa: E402

VERSION_PATH = "src/solin/version.py"
_SHA = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True, slots=True)
class QualityChanges:
    application_required: bool = True
    release_preparation: bool = False


def _git(repository: Path, *arguments: str) -> bytes:
    return subprocess.check_output(["git", *arguments], cwd=repository)


def _version_source(repository: Path, revision: str) -> bytes | None:
    entry = _git(repository, "ls-tree", "-z", revision, "--", VERSION_PATH)
    if not entry:
        return None
    metadata, path = entry.removesuffix(b"\0").split(b"\t", 1)
    mode, kind, _object_id = metadata.split()
    if mode != b"100644" or kind != b"blob" or path != VERSION_PATH.encode():
        return None
    return _git(repository, "show", f"{revision}:{VERSION_PATH}")


def _version_literal(source: bytes) -> tuple[ReleaseVersion, bytes] | None:
    try:
        tree = ast.parse(source.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError, ValueError):
        return None
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets)
    ]
    if len(assignments) != 1 or len(assignments[0].targets) != 1:
        return None
    value = assignments[0].value
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        return None
    version = ReleaseVersion.parse(value.value)
    if version is None or version.canonical != value.value:
        return None
    try:
        _ = version.windows_version, version.macos_version, version.macos_build
    except ValueError:
        return None
    # AST columns are UTF-8 byte offsets, matching the original Git blob.
    lines = source.splitlines(keepends=True)
    assert value.end_lineno is not None and value.end_col_offset is not None
    start = sum(map(len, lines[: value.lineno - 1])) + value.col_offset
    end = sum(map(len, lines[: value.end_lineno - 1])) + value.end_col_offset
    return version, source[:start] + b"<VERSION>" + source[end:]


def _only_version_increased(repository: Path, base: str, head: str) -> bool:
    before = _version_source(repository, base)
    after = _version_source(repository, head)
    if before is None or after is None:
        return False
    old = _version_literal(before)
    new = _version_literal(after)
    return old is not None and new is not None and new[0] > old[0] and new[1] == old[1]


def classify_changes(
    base: str,
    head: str,
    ref_type: str,
    *,
    repository: Path = ROOT,
) -> QualityChanges:
    if (
        ref_type != "branch"
        or _SHA.fullmatch(base) is None
        or _SHA.fullmatch(head) is None
        or base == "0" * 40
        or head == "0" * 40
    ):
        return QualityChanges()
    # NUL separation preserves unusual filenames; Git failures must fail the job.
    paths = _git(repository, "diff", "--no-renames", "--name-only", "-z", base, head)
    changed = set(paths.split(b"\0")) - {b""}
    version_changed = VERSION_PATH.encode() in changed
    if any(path != VERSION_PATH.encode() and not path.endswith(b".md") for path in changed):
        return QualityChanges()
    if version_changed and not _only_version_increased(repository, base, head):
        return QualityChanges()
    return QualityChanges(application_required=False, release_preparation=version_changed)


def main() -> None:
    changes = classify_changes(
        os.environ.get("BASE_SHA", ""),
        os.environ.get("HEAD_SHA", ""),
        os.environ.get("REF_TYPE", ""),
    )
    output = (
        f"application_required={str(changes.application_required).lower()}\n"
        f"release_preparation={str(changes.release_preparation).lower()}\n"
    )
    print(output, end="")
    if destination := os.environ.get("GITHUB_OUTPUT"):
        with Path(destination).open("a", encoding="utf-8") as stream:
            stream.write(output)


if __name__ == "__main__":
    main()
