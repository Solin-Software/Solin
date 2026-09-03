#!/usr/bin/env python3
"""Run the libobs engine on-GUI validation harness.

Thin launcher for :mod:`solin.tools.gui_validation`; see that package for the
full description. Run this on real GUI hardware (a machine with a display, GPU,
and — for the virtual-camera check — a v4l2loopback device on Linux):

    python scripts/validate_gui.py --list
    python scripts/validate_gui.py                       # everything applicable
    python scripts/validate_gui.py --only vcam,recording --html report.html

Exits non-zero if any check fails or errors (skips and manual checks do not fail
the run).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from solin.tools.gui_validation.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
