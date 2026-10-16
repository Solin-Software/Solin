"""Compile application catalogs with the active environment's Qt tools."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from solin.core.i18n.compiler import TranslationCompilationError, compile_catalogs  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=ROOT / "src/solin/resources/translations")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        outputs = compile_catalogs(args.source_dir, args.output_dir)
    except TranslationCompilationError as exc:
        parser.exit(1, f"error: {exc}\n")
    print(f"Prepared {len(outputs)} translation catalogs in {args.output_dir}")


if __name__ == "__main__":
    main()
