from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture_root", type=Path)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    fixture_root = args.fixture_root.resolve()

    from solin.bootstrap.startup_timeline import startup_timeline

    startup_timeline().mark("benchmark_child_started")
    from PySide6.QtCore import QSettings

    from solin.core.foundation.runtime_paths import RuntimePaths

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        str(fixture_root / "settings"),
    )
    RuntimePaths.from_standard_locations = classmethod(
        lambda cls: cls.from_roots(
            data_dir=fixture_root / "data",
            cache_dir=fixture_root / "cache",
        )
    )
    sys.argv = ["solin", "--profile", "startup_benchmark"]

    from solin.bootstrap.entrypoint import run

    run()


if __name__ == "__main__":
    main()
