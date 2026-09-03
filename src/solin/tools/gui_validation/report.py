"""Rendering for harness results: console table, JSON, and a standalone HTML page."""

from __future__ import annotations

import base64
import html
import json
import mimetypes
from pathlib import Path

from solin.tools.gui_validation.harness import CheckResult, CheckStatus

_ICON = {
    CheckStatus.PASS: "PASS",
    CheckStatus.FAIL: "FAIL",
    CheckStatus.SKIP: "skip",
    CheckStatus.MANUAL: "MANUAL",
    CheckStatus.ERROR: "ERROR",
}

_ANSI = {
    CheckStatus.PASS: "\033[32m",
    CheckStatus.FAIL: "\033[31m",
    CheckStatus.SKIP: "\033[90m",
    CheckStatus.MANUAL: "\033[33m",
    CheckStatus.ERROR: "\033[35m",
}
_RESET = "\033[0m"


def counts(results: list[CheckResult]) -> dict[str, int]:
    tally = {status.value: 0 for status in CheckStatus}
    for result in results:
        tally[result.status.value] += 1
    return tally


def exit_code(results: list[CheckResult]) -> int:
    """Non-zero if any check failed or errored (skips/manual do not fail the run)."""
    return 1 if any(r.status.is_problem for r in results) else 0


def render_console(results: list[CheckResult], *, color: bool = True) -> str:
    name_w = max((len(r.name) for r in results), default=4)
    cat_w = max((len(r.category) for r in results), default=8)
    lines = []
    for r in results:
        icon = _ICON[r.status]
        tag = f"{_ANSI[r.status]}{icon:<6}{_RESET}" if color else f"{icon:<6}"
        lines.append(
            f"  {tag} {r.name:<{name_w}}  {r.category:<{cat_w}}  "
            f"{r.duration_s:5.1f}s  {r.summary}")
    tally = counts(results)
    total = len(results)
    summary = (f"{total} checks — {tally['pass']} pass, {tally['fail']} fail, "
               f"{tally['error']} error, {tally['skip']} skip, {tally['manual']} manual")
    return "\n".join(lines + ["", summary])


def render_json(results: list[CheckResult], *, meta: dict | None = None) -> str:
    return json.dumps({
        "meta": meta or {},
        "counts": counts(results),
        "results": [r.to_dict() for r in results],
    }, indent=2)


def _artifact_data_uri(path_str: str | None) -> str | None:
    if not path_str:
        return None
    path = Path(path_str)
    if not path.exists() or path.stat().st_size > 8_000_000:
        return None
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if not mime.startswith("image/"):
        return None  # only inline images; leave video/mp4 as a path reference
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def render_html(results: list[CheckResult], *, meta: dict | None = None) -> str:
    tally = counts(results)
    meta = meta or {}
    rows = []
    for r in results:
        badge_class = f"badge {r.status.value}"
        details = html.escape(json.dumps(r.details)) if r.details else ""
        uri = _artifact_data_uri(r.artifact)
        art = f'<img src="{uri}" alt="{html.escape(r.name)}">' if uri else (
            f'<code>{html.escape(r.artifact)}</code>' if r.artifact else "")
        rows.append(f"""      <tr class="{r.status.value}">
        <td><span class="{badge_class}">{_ICON[r.status]}</span></td>
        <td class="name">{html.escape(r.name)}</td>
        <td>{html.escape(r.category)}</td>
        <td>{html.escape(r.summary)}</td>
        <td class="num">{r.duration_s:.1f}s</td>
        <td class="art">{art}</td>
        <td><details><summary>details</summary><pre>{details}</pre></details></td>
      </tr>""")
    meta_rows = "".join(
        f"<span><b>{html.escape(str(k))}:</b> {html.escape(str(v))}</span>"
        for k, v in meta.items())
    return _HTML_TEMPLATE.format(
        summary=(f"{tally['pass']} pass · {tally['fail']} fail · {tally['error']} error · "
                 f"{tally['skip']} skip · {tally['manual']} manual"),
        meta=meta_rows, rows="\n".join(rows))


_HTML_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Solin libobs GUI validation</title>
<style>
  :root {{ color-scheme: light dark; font-family: system-ui, sans-serif; }}
  body {{ margin: 2rem auto; max-width: 1100px; padding: 0 1rem; }}
  h1 {{ font-size: 1.4rem; }}
  .meta {{ display: flex; flex-wrap: wrap; gap: 1rem; color: #888; font-size: .85rem; margin-bottom: 1rem; }}
  .summary {{ font-size: 1.1rem; margin: .5rem 0 1.5rem; }}
  table {{ border-collapse: collapse; width: 100%; font-size: .9rem; }}
  th, td {{ text-align: left; padding: .5rem .6rem; border-bottom: 1px solid #8883; vertical-align: top; }}
  td.name {{ font-weight: 600; }}
  td.num {{ text-align: right; white-space: nowrap; }}
  td.art img {{ max-width: 160px; max-height: 90px; border: 1px solid #8884; border-radius: 4px; }}
  pre {{ white-space: pre-wrap; word-break: break-word; margin: .3rem 0 0; font-size: .8rem; }}
  .badge {{ display: inline-block; padding: .1rem .5rem; border-radius: 4px; font-weight: 700;
            font-size: .75rem; color: #fff; }}
  .badge.pass {{ background: #2e7d32; }} .badge.fail {{ background: #c62828; }}
  .badge.error {{ background: #6a1b9a; }} .badge.skip {{ background: #616161; }}
  .badge.manual {{ background: #e65100; }}
  tr.fail, tr.error {{ background: #c6282814; }}
</style></head><body>
  <h1>Solin — libobs engine on-GUI validation</h1>
  <div class="meta">{meta}</div>
  <div class="summary">{summary}</div>
  <table>
    <thead><tr><th>Status</th><th>Check</th><th>Category</th><th>Result</th>
    <th>Time</th><th>Artifact</th><th></th></tr></thead>
    <tbody>
{rows}
    </tbody>
  </table>
</body></html>
"""
