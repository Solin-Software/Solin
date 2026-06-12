from __future__ import annotations

import ast
import json
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BASELINE_PATH = Path(__file__).with_name("exception_policy_baseline.json")
_BROAD_EXCEPTION_NOQA = "noqa: BLE001"


def _python_sources() -> list[Path]:
    return [
        _REPO_ROOT / "main.py",
        *sorted((_REPO_ROOT / "app").rglob("*.py")),
    ]


def _broad_exception_counts() -> tuple[dict[str, int], list[str]]:
    counts: Counter[str] = Counter()
    invalid_suppressions: list[str] = []

    for path in _python_sources():
        source = path.read_text(encoding="utf-8-sig")
        lines = source.splitlines()
        tree = ast.parse(source, filename=str(path))
        relative_path = path.relative_to(_REPO_ROOT).as_posix()

        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if not (isinstance(node.type, ast.Name) and node.type.id == "Exception"):
                continue

            handler_line = lines[node.lineno - 1]
            if _BROAD_EXCEPTION_NOQA in handler_line:
                justification = handler_line.partition(_BROAD_EXCEPTION_NOQA)[2].strip()
                if not justification.startswith("- ") or len(justification) <= 4:
                    invalid_suppressions.append(f"{relative_path}:{node.lineno}")
                continue

            counts[relative_path] += 1

    return dict(sorted(counts.items())), invalid_suppressions


def test_app_layers_do_not_silently_swallow_exception_pass_blocks():
    offenders: list[str] = []

    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        relative_path = path.relative_to(_REPO_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            exception_type = ast.unparse(node.type) if node.type else ""
            if (
                exception_type == "Exception"
                and len(node.body) == 1
                and isinstance(node.body[0], ast.Pass)
            ):
                offenders.append(f"{relative_path}:{node.lineno}")

    assert offenders == []


def test_broad_exception_debt_does_not_grow():
    expected = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
    observed, invalid_suppressions = _broad_exception_counts()

    assert invalid_suppressions == [], (
        "BLE001 suppressions require an inline justification after '- ': "
        f"{invalid_suppressions}"
    )
    assert observed == expected, (
        "Broad-exception baseline changed. Replace new catches with specific exception "
        "types. When intentionally removing existing broad catches, update "
        f"{_BASELINE_PATH.name} to the new lower counts.\n"
        f"Expected: {expected}\nObserved: {observed}"
    )
