"""Committed-change boundaries for the reduced release-preparation Quality path."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from collections.abc import Mapping

import pytest

from scripts import classify_quality_changes
from scripts.classify_quality_changes import QualityChanges, classify_changes

VERSION_PATH = "src/solin/version.py"
VERSION_SOURCE = (
    '"""Canonical CalVer identity."""\n\nVERSION = "26.32.0b3"\n__version__ = VERSION\n'
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

    def commit(self, changes: Mapping[str, tuple[str, bytes] | None]) -> str:
        for path, entry in changes.items():
            if entry is None:
                git(self.repository, "update-index", "--force-remove", "--", path)
            else:
                mode, source = entry
                object_id = git(self.repository, "hash-object", "-w", "--stdin", data=source)
                git(self.repository, "update-index", "--add", "--cacheinfo", mode, object_id, path)
        tree = git(self.repository, "write-tree")
        parents = ["-p", self.head] if self.head else []
        self.head = git(self.repository, "commit-tree", tree, *parents, data=b"Quality fixture\n")
        return self.head


@pytest.fixture
def history(tmp_path: Path) -> History:
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "--quiet")
    git(repository, "config", "user.name", "Quality Fixture")
    git(repository, "config", "user.email", "quality@example.invalid")
    git(repository, "config", "commit.gpgSign", "false")
    result = History(repository)
    result.commit({VERSION_PATH: ("100644", VERSION_SOURCE), "README.md": ("100644", b"Docs\n")})
    return result


def classify(history: History, base: str, ref_type: str = "branch") -> QualityChanges:
    return classify_changes(base, history.head, ref_type, repository=history.repository)


@pytest.mark.parametrize("version", ["26.32.0b4", "26.32.0", "26.33.0b1", "27.1.0b1"])
@pytest.mark.parametrize("with_notes", [False, True])
def test_only_an_increased_version_with_optional_documentation_reduces_quality(
    history: History,
    version: str,
    with_notes: bool,
) -> None:
    base = history.head
    changes = {VERSION_PATH: ("100644", VERSION_SOURCE.replace(b"26.32.0b3", version.encode()))}
    if with_notes:
        changes["docs/release-notes/en.md"] = ("100644", b"Reviewed notes\n")
    history.commit(changes)
    assert classify(history, base) == QualityChanges(False, True)


@pytest.mark.parametrize(
    "path", ["README.md", "docs/release-notes/pt_BR.md", "docs/Notas de versão.md"]
)
def test_documentation_only_changes_keep_the_existing_reduced_path(
    history: History, path: str
) -> None:
    base = history.head
    history.commit({path: ("100644", b"Changed documentation\n")})
    assert classify(history, base) == QualityChanges(False, False)


@pytest.mark.parametrize(
    "version",
    [
        "26.32.0b2",
        "26.31.1",
        "26.032.0b4",
        "26.32.0b04",
        "9.32.0",
        "26.65536.0",
        "26.32.65536",
        "26.32.0b65535",
        "26.32.0b0",
        "26.32.0.4",
        "",
    ],
)
def test_invalid_noncanonical_or_older_versions_require_full_quality(
    history: History, version: str
) -> None:
    base = history.head
    history.commit(
        {VERSION_PATH: ("100644", VERSION_SOURCE.replace(b"26.32.0b3", version.encode()))}
    )
    assert classify(history, base) == QualityChanges()


@pytest.mark.parametrize(
    "source",
    [
        VERSION_SOURCE.replace(b"__version__ = VERSION", b'__version__ = "26.32.0b4"'),
        VERSION_SOURCE.replace(b"Canonical CalVer identity", b"Changed module documentation"),
        VERSION_SOURCE + b"extra = True\n",
        VERSION_SOURCE + b'VERSION = "26.32.0b5"\n',
        VERSION_SOURCE.replace(b"VERSION =", b"VERSION = OTHER ="),
        VERSION_SOURCE.replace(b'"26.32.0b3"', b"str(123)"),
        VERSION_SOURCE.replace(b'"26.32.0b3"', b"123"),
        VERSION_SOURCE.replace(b'"26.32.0b3"', b'"26.32.0b3'),
        VERSION_SOURCE + b"\x00",
    ],
)
def test_changes_outside_the_version_value_or_nonliteral_assignments_require_full_quality(
    history: History,
    source: bytes,
) -> None:
    base = history.head
    history.commit({VERSION_PATH: ("100644", source.replace(b"26.32.0b3", b"26.32.0b4"))})
    assert classify(history, base) == QualityChanges()


def test_changing_quotes_without_increasing_the_version_requires_full_quality(
    history: History,
) -> None:
    base = history.head
    source = VERSION_SOURCE.replace(b'"26.32.0b3"', b"'26.32.0b3'")
    history.commit({VERSION_PATH: ("100644", source)})
    assert classify(history, base) == QualityChanges()


@pytest.mark.parametrize("line_ending", [b"\n", b"\r\n"])
def test_literal_offsets_preserve_unicode_and_line_endings(
    history: History, line_ending: bytes
) -> None:
    source = '"""Versão canônica."""\n\nVERSION = "26.32.0b3"\n__version__ = VERSION\n'.encode()
    source = source.replace(b"\n", line_ending)
    base = history.commit({VERSION_PATH: ("100644", source)})
    history.commit({VERSION_PATH: ("100644", source.replace(b"26.32.0b3", b"26.32.0b4"))})
    assert classify(history, base) == QualityChanges(False, True)


def test_changed_line_endings_are_not_mistaken_for_a_version_only_change(history: History) -> None:
    base = history.head
    source = VERSION_SOURCE.replace(b"26.32.0b3", b"26.32.0b4").replace(b"\n", b"\r\n")
    history.commit({VERSION_PATH: ("100644", source)})
    assert classify(history, base) == QualityChanges()


@pytest.mark.parametrize(
    "path",
    [
        "src/solin/main_window.py",
        ".github/workflows/quality.yml",
        "requirements.txt",
        "src/source with spaces.py",
        "src/renamed-version.md.py",
    ],
)
def test_other_code_dependencies_workflows_and_unusual_paths_require_full_quality(
    history: History,
    path: str,
) -> None:
    base = history.head
    history.commit(
        {
            VERSION_PATH: ("100644", VERSION_SOURCE.replace(b"26.32.0b3", b"26.32.0b4")),
            path: ("100644", b"Changed application input\n"),
        }
    )
    assert classify(history, base) == QualityChanges()


@pytest.mark.parametrize("entry", [None, ("100755", VERSION_SOURCE), ("120000", b"../other.py")])
def test_removed_version_files_and_changed_file_modes_require_full_quality(
    history: History,
    entry: tuple[str, bytes] | None,
) -> None:
    base = history.head
    history.commit({VERSION_PATH: entry})
    assert classify(history, base) == QualityChanges()


def test_a_new_version_file_cannot_use_the_reduced_path(history: History) -> None:
    base = history.commit({VERSION_PATH: None})
    history.commit({VERSION_PATH: ("100644", VERSION_SOURCE)})
    assert classify(history, base) == QualityChanges()


def test_version_code_is_never_executed_during_classification(
    history: History, tmp_path: Path
) -> None:
    marker = tmp_path / "executed"
    source = f'VERSION = __import__("pathlib").Path({str(marker)!r}).touch()\n'.encode()
    base = history.head
    history.commit({VERSION_PATH: ("100644", source)})
    assert classify(history, base) == QualityChanges()
    assert not marker.exists()


@pytest.mark.parametrize("ref_type", ["tag", "", "unknown"])
def test_tags_and_unknown_contexts_always_require_full_quality(
    history: History, ref_type: str
) -> None:
    base = history.head
    history.commit({VERSION_PATH: ("100644", VERSION_SOURCE.replace(b"26.32.0b3", b"26.32.0b4"))})
    assert classify(history, base, ref_type) == QualityChanges()


@pytest.mark.parametrize(
    "base,head",
    [
        ("", "a" * 40),
        ("0" * 40, "a" * 40),
        ("not-a-sha", "a" * 40),
        ("a" * 40, ""),
        ("a" * 40, "--help"),
        ("a" * 40, "0" * 40),
    ],
)
def test_missing_or_invalid_comparison_refs_require_full_quality(base: str, head: str) -> None:
    assert classify_changes(base, head, "branch") == QualityChanges()


def test_unavailable_git_history_fails_instead_of_skipping_platforms(history: History) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        classify(history, "f" * 40)


def test_nul_separation_does_not_split_a_filename_into_fake_documentation(monkeypatch) -> None:
    # Windows cannot stage this filename; Git on POSIX may return it as one path.
    monkeypatch.setattr(
        classify_quality_changes.subprocess,
        "check_output",
        lambda *args, **kwargs: b"README.md\napplication.py\0",
    )
    assert classify_changes("a" * 40, "b" * 40, "branch") == QualityChanges()
