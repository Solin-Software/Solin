from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCALES_DIR = PROJECT_ROOT / "resources" / "translations" / "locales"
DEFAULT_SOURCE_DIR = PROJECT_ROOT / "src" / "solin"
STRING_FALLBACK_RE = re.compile(r"""["']([^"']+)["']""")


def _configure_output_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def flatten_mapping(data: Mapping[str, object], parent_key: str = "") -> dict[str, object]:
    items: dict[str, object] = {}
    for key, value in data.items():
        full_key = f"{parent_key}.{key}" if parent_key else key
        if isinstance(value, Mapping):
            items.update(flatten_mapping(value, full_key))
        else:
            items[full_key] = value
    return items


def load_locale_keys(locale_files: Sequence[Path]) -> tuple[dict[str, set[str]], set[str]]:
    file_keys: dict[str, set[str]] = {}
    all_keys: set[str] = set()

    for locale_file in locale_files:
        try:
            data = json.loads(locale_file.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Could not read locale file {locale_file}: {exc}") from exc

        if not isinstance(data, dict):
            raise SystemExit(f"Locale file must contain a JSON object: {locale_file}")

        keys = set(flatten_mapping(data).keys())
        file_keys[locale_file.name] = keys
        all_keys.update(keys)

    return file_keys, all_keys


def report_missing_locale_keys(file_keys: Mapping[str, set[str]], all_keys: set[str]) -> bool:
    has_missing_keys = False
    for filename, keys in sorted(file_keys.items()):
        missing = all_keys - keys
        if not missing:
            continue

        has_missing_keys = True
        print(f"{filename} is missing {len(missing)} locale key(s):")
        for key in sorted(missing):
            print(f"  - {key}")
        print()

    return has_missing_keys


def _python_files(source_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in source_dir.rglob("*.py")
        if "__pycache__" not in path.parts and not any(part.startswith(".") for part in path.parts)
    )


def _strings_from_ast(source: str, path: Path) -> list[tuple[str, int]]:
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return [
            (match.group(1), line_number)
            for line_number, line in enumerate(source.splitlines(), start=1)
            for match in STRING_FALLBACK_RE.finditer(line)
        ]

    strings: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.append((node.value, node.lineno))
    return strings


def collect_python_string_locations(source_dir: Path) -> dict[str, list[tuple[Path, int]]]:
    if not source_dir.is_dir():
        raise SystemExit(f"Source directory does not exist: {source_dir}")

    string_locations: dict[str, list[tuple[Path, int]]] = {}
    python_files = _python_files(source_dir)
    if not python_files:
        raise SystemExit(f"No Python files found in source directory: {source_dir}")

    for path in python_files:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SystemExit(f"Could not read source file {path}: {exc}") from exc

        for value, line_number in _strings_from_ast(source, path):
            string_locations.setdefault(value, []).append((path, line_number))

    return string_locations


def matching_source_literal(locale_key: str, source_literals: set[str]) -> str | None:
    parts = locale_key.split(".")
    suffix = ".".join(parts[1:]) if len(parts) > 1 else locale_key

    candidates = [locale_key, suffix]
    if locale_key.startswith("meta."):
        candidates.append(locale_key.removeprefix("meta."))

    return next((candidate for candidate in candidates if candidate in source_literals), None)


def report_unused_locale_keys(
    all_keys: set[str],
    string_locations: Mapping[str, list[tuple[Path, int]]],
    *,
    details: bool,
    source_dir: Path,
) -> bool:
    source_literals = set(string_locations)
    unused = [
        key
        for key in sorted(all_keys)
        if matching_source_literal(key, source_literals) is None
    ]

    if unused:
        print(f"Found {len(unused)} locale key(s) that are not referenced in Python source:")
        for key in unused:
            print(f"  - {key}")
        print("\nKeys assembled dynamically may need an explicit source literal to stay verifiable.")
        return True

    print("All locale keys are referenced in Python source.")

    if details:
        print("\nLocale key references:")
        for key in sorted(all_keys):
            matched = matching_source_literal(key, source_literals)
            locations = string_locations.get(matched or "", [])
            print(f"\n  {key}")
            for path, line_number in locations:
                print(f"    {path.relative_to(source_dir.parent)}:{line_number}")

    return False


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate locale JSON key parity and Python source usage."
    )
    parser.add_argument("--locales-dir", type=Path, default=DEFAULT_LOCALES_DIR)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--details", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    _configure_output_encoding()
    args = parse_args(sys.argv[1:] if argv is None else argv)

    locales_dir = args.locales_dir.resolve()
    source_dir = args.source_dir.resolve()
    locale_files = sorted(locales_dir.glob("*.json"))
    if not locale_files:
        raise SystemExit(f"No locale JSON files found in: {locales_dir}")

    print(f"Checking {len(locale_files)} locale file(s) in {locales_dir}")
    file_keys, all_keys = load_locale_keys(locale_files)

    if report_missing_locale_keys(file_keys, all_keys):
        print("Locale usage check skipped until missing keys are fixed.")
        return 1

    print("All locale JSON files expose the same key set.")
    string_locations = collect_python_string_locations(source_dir)
    if report_unused_locale_keys(
        all_keys,
        string_locations,
        details=args.details,
        source_dir=source_dir,
    ):
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
