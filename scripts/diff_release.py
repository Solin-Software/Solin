from __future__ import annotations

import argparse
import hashlib
import shutil
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def file_hash(path: Path, algorithm: str = "sha256") -> str:
    hash_func = hashlib.new(algorithm)
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(8192), b""):
            hash_func.update(chunk)
    return hash_func.hexdigest()


def map_directory(root: Path) -> dict[Path, Path]:
    return {
        path.relative_to(root): path
        for path in root.rglob("*")
        if path.is_file()
    }


def should_ignore(relative_path: Path, ignored_extensions: Iterable[str]) -> bool:
    return any(relative_path.name.endswith(extension) for extension in ignored_extensions)


def changed_files(
    old_dir: Path,
    new_dir: Path,
    ignored_extensions: Sequence[str],
    workers: int,
) -> list[tuple[str, Path, Path]]:
    old_files = map_directory(old_dir)
    new_files = map_directory(new_dir)

    def process(relative_path: Path) -> tuple[str, Path, Path] | None:
        if should_ignore(relative_path, ignored_extensions):
            return None

        new_path = new_files[relative_path]
        old_path = old_files.get(relative_path)
        if old_path is None:
            return ("NEW", relative_path, new_path)

        if file_hash(new_path) != file_hash(old_path):
            return ("MODIFIED", relative_path, new_path)

        return None

    with ThreadPoolExecutor(max_workers=workers) as executor:
        return [result for result in executor.map(process, new_files) if result is not None]


def copy_changed_files(changes: Sequence[tuple[str, Path, Path]], output_dir: Path) -> None:
    for change_type, relative_path, source_path in changes:
        print(f"[{change_type}] {relative_path}")
        destination = output_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare release directories by hash and copy changed files."
    )
    parser.add_argument("--old", type=Path, required=True, help="Previous release directory")
    parser.add_argument("--new", type=Path, required=True, help="New release directory")
    parser.add_argument("--out", type=Path, required=True, help="Output diff directory")
    parser.add_argument(
        "--ignore-ext",
        nargs="*",
        default=[],
        help="File suffixes to ignore, for example .log .tmp",
    )
    parser.add_argument("--workers", type=int, default=4, help="Hash worker count")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    old_dir = args.old.resolve()
    new_dir = args.new.resolve()
    output_dir = args.out.resolve()

    if not old_dir.is_dir():
        print(f"Previous release directory does not exist: {old_dir}")
        return 1

    if not new_dir.is_dir():
        print(f"New release directory does not exist: {new_dir}")
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)
    changes = changed_files(old_dir, new_dir, args.ignore_ext, args.workers)
    copy_changed_files(changes, output_dir)
    print(f"\nCopied {len(changes)} changed file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
