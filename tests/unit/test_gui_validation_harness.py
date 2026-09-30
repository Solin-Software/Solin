"""Unit tests for the on-GUI validation harness's parent-side logic.

Covers result/config serialization, capability gating + aggregation (with an
injected runner, no subprocess), capability/loopback detection, and report
rendering. The libobs checks themselves run only on real hardware.
"""
from __future__ import annotations

import json


from solin.tools.gui_validation import checks as _checks  # noqa: F401 - populate registry
from solin.tools.gui_validation import report as report_mod
from solin.tools.gui_validation.harness import (
    CAP_DISPLAY,
    CAP_V4L2LOOPBACK,
    CheckResult,
    CheckSpec,
    CheckStatus,
    HarnessConfig,
    all_checks,
    detect_capabilities,
    get_check,
    loopback_devices,
    run_selected,
)


def _spec(name="demo", requires=()):
    return CheckSpec(name=name, category="cat", description="d", requires=tuple(requires),
                     fn=lambda ctx: ctx.result(CheckStatus.PASS, "ok"))


# ── serialization ─────────────────────────────────────────────────────────────


def test_check_result_roundtrips():
    result = CheckResult(name="n", category="c", status=CheckStatus.FAIL,
                         summary="s", details={"k": 1}, duration_s=2.5, artifact="/x.png")
    again = CheckResult.from_dict(result.to_dict())
    assert again == result
    assert again.status is CheckStatus.FAIL


def test_config_roundtrips_and_ignores_unknown_keys():
    config = HarnessConfig(width=800, height=450, fps=25, clip_path="/c.mp4")
    data = config.to_dict()
    data["surprise"] = "ignored"
    assert HarnessConfig.from_dict(data) == config


# ── registry ──────────────────────────────────────────────────────────────────


def test_all_expected_checks_are_registered():
    names = {spec.name for spec in all_checks()}
    assert {"runtime_boot", "scene_composite", "media_playback", "routed_metadata",
            "preview_egress", "program_egress", "recording", "virtual_camera",
            "window_output", "subprocess_engine"} <= names


def test_window_and_recording_declare_requirements():
    assert CAP_DISPLAY in get_check("window_output").requires
    assert "ffprobe" in get_check("recording").requires


# ── gating + aggregation ──────────────────────────────────────────────────────


def test_missing_capability_skips_without_running():
    ran = []

    def runner(spec, config):
        ran.append(spec.name)
        return CheckResult(name=spec.name, category=spec.category, status=CheckStatus.PASS)

    results = run_selected(HarnessConfig(), [_spec("needs_disp", requires=(CAP_DISPLAY,))],
                           capabilities=set(), runner=runner)
    assert results[0].status is CheckStatus.SKIP
    assert "missing_capabilities" in results[0].details
    assert ran == []  # gated before the runner


def test_met_capability_runs_via_runner():
    def runner(spec, config):
        return CheckResult(name=spec.name, category=spec.category,
                           status=CheckStatus.PASS, summary="ran")

    results = run_selected(HarnessConfig(), [_spec("needs_disp", requires=(CAP_DISPLAY,))],
                           capabilities={CAP_DISPLAY}, runner=runner)
    assert results[0].status is CheckStatus.PASS and results[0].summary == "ran"


def test_mixed_selection_gates_each_independently():
    def runner(spec, config):
        return CheckResult(name=spec.name, category=spec.category, status=CheckStatus.PASS)

    specs = [_spec("a"), _spec("b", requires=(CAP_V4L2LOOPBACK,))]
    results = run_selected(HarnessConfig(), specs, capabilities=set(), runner=runner)
    assert results[0].status is CheckStatus.PASS   # no requirement
    assert results[1].status is CheckStatus.SKIP   # loopback missing


# ── capability detection ──────────────────────────────────────────────────────


def test_detect_capabilities_reads_tools_and_display(monkeypatch):
    monkeypatch.setattr("solin.tools.gui_validation.harness.shutil.which",
                        lambda name: "/usr/bin/" + name if name in ("ffprobe", "ffmpeg") else None)
    monkeypatch.setattr("solin.tools.gui_validation.harness._has_display", lambda: True)
    monkeypatch.setattr("solin.tools.gui_validation.harness.loopback_devices", lambda: [])
    caps = detect_capabilities(HarnessConfig())
    assert caps == {CAP_DISPLAY, "ffprobe", "ffmpeg"}


def test_loopback_devices_empty_off_linux(monkeypatch):
    monkeypatch.setattr("solin.tools.gui_validation.harness.sys.platform", "win32")
    assert loopback_devices() == []


# ── reporting ─────────────────────────────────────────────────────────────────


def _results():
    return [
        CheckResult(name="a", category="scene", status=CheckStatus.PASS, summary="good"),
        CheckResult(name="b", category="vcam", status=CheckStatus.SKIP, summary="no device"),
        CheckResult(name="c", category="media", status=CheckStatus.FAIL, summary="wrong colour"),
    ]


def test_counts_and_exit_code():
    results = _results()
    assert report_mod.counts(results) == {
        "pass": 1, "fail": 1, "skip": 1, "manual": 0, "error": 0}
    assert report_mod.exit_code(results) == 1
    assert report_mod.exit_code([r for r in results if r.status is CheckStatus.PASS]) == 0


def test_console_report_lists_every_check():
    text = report_mod.render_console(_results(), color=False)
    assert "PASS" in text and "FAIL" in text and "skip" in text
    for name in ("a", "b", "c"):
        assert name in text
    assert "3 checks" in text


def test_json_report_is_valid_and_complete():
    payload = json.loads(report_mod.render_json(_results(), meta={"platform": "linux"}))
    assert payload["counts"]["fail"] == 1
    assert payload["meta"]["platform"] == "linux"
    assert [r["name"] for r in payload["results"]] == ["a", "b", "c"]


def test_html_report_embeds_png_artifact(tmp_path):
    from PySide6.QtGui import QImage

    png = tmp_path / "shot.png"
    img = QImage(2, 2, QImage.Format.Format_RGB32)
    img.fill(0xFF00AA00)
    assert img.save(str(png), "PNG")

    results = [CheckResult(name="shot", category="scene", status=CheckStatus.PASS,
                           summary="captured", artifact=str(png))]
    html = report_mod.render_html(results, meta={})
    assert "data:image/png;base64," in html
    assert "badge pass" in html
    assert "shot" in html
