"""On-GUI validation harness for the libobs scene/media engine.

Most of the libobs re-platform was verified headless (unit tests + xvfb smokes),
but a set of paths only exercise real GPU/display/device behaviour and so stayed
"on-GUI unvalidated": cross-process window binding, live media playback +
projection, editor-preview and program-mirror egress, recording, the virtual
camera on a real loopback/DShow consumer, audio capture, and routed cover-art
timing on the real media backend.

This package consolidates the ad-hoc per-stage smokes into one structured,
repeatable harness the operator runs on real hardware:

    python -m solin.tools.gui_validation --list
    python -m solin.tools.gui_validation                # run everything applicable
    python -m solin.tools.gui_validation --only vcam,recording
    python -m solin.tools.gui_validation --html report.html

Each check runs in its own subprocess (fresh libobs runtime — the runtime is a
process-wide singleton, and isolation keeps one check's crash or teardown from
poisoning the next), reports a structured :class:`~.harness.CheckResult`, and the
parent renders a console table plus optional JSON/HTML reports.
"""

from solin.tools.gui_validation.harness import (
    CheckResult,
    CheckSpec,
    CheckStatus,
    HarnessConfig,
    all_checks,
    detect_capabilities,
    register_check,
    run_selected,
)

__all__ = [
    "CheckResult",
    "CheckSpec",
    "CheckStatus",
    "HarnessConfig",
    "all_checks",
    "detect_capabilities",
    "register_check",
    "run_selected",
]
