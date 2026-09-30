"""CLI entry point for the on-GUI validation harness.

Parent mode (default) selects checks, runs each in a child process, and renders
the results. Child mode (``--run-check``) executes exactly one check against a
live libobs runtime and writes its result file — this is what the parent spawns.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Importing checks populates the registry.
from solin.tools.gui_validation import checks as _checks  # noqa: F401
from solin.tools.gui_validation import report as report_mod
from solin.tools.gui_validation.harness import (
    HarnessConfig,
    all_checks,
    detect_capabilities,
    execute_check,
    get_check,
    run_selected,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m solin.tools.gui_validation",
        description="Validate the libobs scene/media engine on real GUI hardware.")
    p.add_argument("--list", action="store_true", help="list the available checks and exit")
    p.add_argument("--only", default="", help="comma-separated check names to run")
    p.add_argument("--skip", default="", help="comma-separated check names to skip")
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--clip", default="", help="a media file for the playback check")
    p.add_argument("--out-dir", default="", help="where assets/artifacts/reports land")
    p.add_argument("--hold", type=float, default=0.0,
                   help="seconds to hold each visual composite so you can watch it")
    p.add_argument("--interactive", action="store_true",
                   help="prompt for manual confirmation on visual checks")
    p.add_argument("--json", default="", help="write a JSON report to this path")
    p.add_argument("--html", default="", help="write a standalone HTML report to this path")
    p.add_argument("--no-color", action="store_true", help="disable ANSI colour in the table")
    # Child mode (spawned by the parent; not for direct use).
    p.add_argument("--run-check", default="", help=argparse.SUPPRESS)
    p.add_argument("--config-file", default="", help=argparse.SUPPRESS)
    p.add_argument("--result-file", default="", help=argparse.SUPPRESS)
    return p


def _config(args) -> HarnessConfig:
    return HarnessConfig(
        width=args.width, height=args.height, fps=args.fps,
        clip_path=args.clip, out_dir=args.out_dir,
        hold_seconds=args.hold, interactive=args.interactive)


def _run_child(args) -> int:
    import json

    config = HarnessConfig.from_dict(json.loads(Path(args.config_file).read_text()))
    execute_check(get_check(args.run_check), config, Path(args.result_file))
    return 0


def _print_list() -> None:
    by_category: dict[str, list] = {}
    for spec in all_checks():
        by_category.setdefault(spec.category, []).append(spec)
    for category in sorted(by_category):
        print(f"\n{category}")
        for spec in by_category[category]:
            reqs = f"  [needs: {', '.join(spec.requires)}]" if spec.requires else ""
            print(f"  {spec.name:<18} {spec.description}{reqs}")


def _select(args) -> list:
    only = {n.strip() for n in args.only.split(",") if n.strip()}
    skip = {n.strip() for n in args.skip.split(",") if n.strip()}
    specs = all_checks()
    if only:
        specs = [s for s in specs if s.name in only]
    return [s for s in specs if s.name not in skip]


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.run_check:
        return _run_child(args)

    if args.list:
        _print_list()
        return 0

    config = _config(args)
    selected = _select(args)
    if not selected:
        print("no checks selected", file=sys.stderr)
        return 2

    capabilities = detect_capabilities(config)
    print(f"Solin libobs on-GUI validation — {len(selected)} check(s)")
    print(f"capabilities: {', '.join(sorted(capabilities)) or 'none detected'}\n")

    started = time.strftime("%Y-%m-%d %H:%M:%S")
    results = run_selected(config, selected, capabilities=capabilities)
    print(report_mod.render_console(results, color=not args.no_color))

    meta = {"started": started, "platform": sys.platform,
            "resolution": f"{config.width}x{config.height}",
            "capabilities": ", ".join(sorted(capabilities)) or "none"}
    if args.json:
        Path(args.json).write_text(report_mod.render_json(results, meta=meta))
        print(f"\nJSON report: {args.json}")
    if args.html:
        Path(args.html).write_text(report_mod.render_html(results, meta=meta))
        print(f"HTML report: {args.html}")

    return report_mod.exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
