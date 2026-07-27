from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence


def main(arguments: Sequence[str] | None = None) -> int:
    extra_arguments = list(arguments if arguments is not None else sys.argv[1:])
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pyright",
            "--pythonpath",
            sys.executable,
            *extra_arguments,
        ],
        check=False,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
