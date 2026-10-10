"""Cross-workflow guarantees required before a distribution can be published."""
from pathlib import Path
import os
import re
import shutil
import subprocess
import sys

import pytest
import yaml

from tests._paths import REPO_ROOT


def workflow(name: str) -> dict:
    return yaml.load((REPO_ROOT / ".github" / "workflows" / name).read_text(), Loader=yaml.BaseLoader)


def test_publication_requires_all_platforms_and_quality():
    release = workflow("release.yml")
    assert set(release["jobs"]["publish"]["needs"]) == {"quality", "windows", "linux", "macos"}
    assert release["jobs"]["publish"]["concurrency"]["cancel-in-progress"] == "false"
    for platform in ("windows", "linux", "macos"):
        assert release["jobs"][platform]["with"]["release"] == "true"
        build = workflow(f"build-solin-{platform}.yml")
        assert "workflow_call" in build["on"]
        assert "workflow_dispatch" in build["on"]
        assert build["permissions"]["contents"] == "read"
    mac = workflow("build-solin-macos.yml")["jobs"]["build"]
    assert mac["strategy"]["fail-fast"] == "false"
    assert {row["architecture"] for row in mac["strategy"]["matrix"]["include"]} == {"arm64", "x86_64"}


def test_quality_gate_waits_for_every_platform():
    quality_workflow = workflow("quality.yml")
    quality = quality_workflow["jobs"]
    assert quality["gate"]["name"] == "Quality Gate"
    assert quality["gate"]["if"] == "always()"
    assert set(quality["gate"]["needs"]) == {"changes", "static", "platforms"}
    assert quality["platforms"]["needs"] == "changes"
    assert quality["platforms"]["if"] == (
        "needs.changes.outputs.application_required == 'true'"
    )
    classifier = next(step for step in quality["changes"]["steps"] if step.get("id") == "classify")
    assert classifier["run"] == "python scripts/classify_quality_changes.py"
    assert classifier["env"]["REF_TYPE"] == "${{ github.ref_type }}"
    assert quality["static"]["needs"] == "changes"
    preparation = next(
        step for step in quality["static"]["steps"]
        if step.get("name") == "Validate release preparation"
    )
    assert preparation["if"] == "needs.changes.outputs.release_preparation == 'true'"
    assert "--noconftest tests/unit/test_release_metadata.py tests/unit/test_quality_changes.py" in preparation["run"]
    assert "load_notes(current_version())" in preparation["run"]
    assert len(quality["platforms"]["strategy"]["matrix"]["include"]) == 4
    assert quality_workflow["env"]["QT_QPA_PLATFORM"] == "offscreen"
    for target in quality["platforms"]["strategy"]["matrix"]["include"]:
        assert target["qt_platform"] == ("cocoa" if target["platform"] == "Darwin" else "offscreen")
    steps = {step.get("name"): step for step in quality["platforms"]["steps"]}
    assert steps["Native protocol and benchmarks"]["if"] == "runner.os == 'Linux'"
    windows = steps["Python suites on Windows"]
    assert windows["if"] == "runner.os == 'Windows'"
    assert "pytest tests/contract --durations=20" in windows["run"]
    assert "pytest tests/integration --durations=20" in windows["run"]
    assert "pytest tests/unit --durations=20" in windows["run"]
    macos = steps["Python suites on macOS"]
    assert macos["if"] == "runner.os == 'macOS'"
    assert "pytest tests/contract --durations=20" in macos["run"]
    assert "pytest tests/integration --durations=20" in macos["run"]
    assert "pytest tests/unit --durations=20" in macos["run"]


@pytest.mark.parametrize("application_required,platform_result,accepted", [
    ("true", "success", True), ("false", "skipped", True),
    ("true", "skipped", False), ("true", "failure", False),
    ("false", "failure", False), ("false", "success", False),
    ("", "skipped", False), ("unknown", "skipped", False),
])
def test_quality_gate_requires_an_explicit_valid_classification(
    application_required, platform_result, accepted,
):
    git_executable = shutil.which("git")
    git_bash = (
        Path(git_executable).parents[1] / "bin" / "bash.exe"
        if sys.platform == "win32" and git_executable else None
    )
    executable = str(git_bash) if git_bash is not None and git_bash.is_file() else shutil.which("bash")
    if executable is None:
        pytest.skip("Bash is not installed")
    commands = workflow("quality.yml")["jobs"]["gate"]["steps"][0]["run"]
    result = subprocess.run(
        [executable, "--noprofile", "--norc", "-e"], input=commands,
        env={
            **os.environ, "CHANGES_RESULT": "success", "STATIC_RESULT": "success",
            "APPLICATION_REQUIRED": application_required, "PLATFORM_RESULT": platform_result,
        },
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr


@pytest.mark.parametrize("notes", ["reviewed", "missing", "outdated", "placeholder"])
def test_reduced_preparation_requires_reviewed_notes_for_the_checked_out_version(
    notes, tmp_path, monkeypatch,
):
    from scripts import release

    source = tmp_path / "src" / "solin"
    source.mkdir(parents=True)
    (source / "version.py").write_text('VERSION = "26.32.0b4"\n', encoding="utf-8")
    locales = source / "resources" / "translations" / "locales"
    locales.mkdir(parents=True)
    (locales / "en.json").write_text("{}", encoding="utf-8")
    note_root = tmp_path / "docs" / "release-notes"
    if notes != "missing":
        tag = "26.32.0-beta.3" if notes == "outdated" else "26.32.0-beta.4"
        directory = note_root / tag
        directory.mkdir(parents=True)
        content = "<!-- TODO: Review notes. -->" if notes == "placeholder" else "Reviewed changes"
        (directory / "en.md").write_text(content, encoding="utf-8")
    monkeypatch.setattr(release, "ROOT", tmp_path)
    monkeypatch.setattr(release, "NOTES", note_root)
    preparation = next(
        step for step in workflow("quality.yml")["jobs"]["static"]["steps"]
        if step.get("name") == "Validate release preparation"
    )
    script = preparation["run"].split("python - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    if notes == "reviewed":
        exec(compile(script, "release-preparation-notes", "exec"), {})
    else:
        with pytest.raises(ValueError):
            exec(compile(script, "release-preparation-notes", "exec"), {})


def test_macos_quality_preserves_crash_evidence_without_interactive_alerts():
    steps = workflow("quality.yml")["jobs"]["platforms"]["steps"]
    suites = next(step for step in steps if step.get("name") == "Python suites on macOS")
    commands = suites["run"]
    configuration = "defaults write com.apple.CrashReporter DialogType -string server"
    assert configuration in commands
    assert commands.index(configuration) < commands.index("python -m pytest")
    assert "ulimit -c unlimited" in commands
    report = next(step for step in steps if step.get("name") == "Report macOS native crashes")
    assert report["if"] == "failure() && runner.os == 'macOS'"
    assert '"thread backtrace all"' in report["run"]
    assert '"Library/Logs/DiagnosticReports"' in report["run"]


def test_python_suite_preserves_failure_evidence_before_abrupt_exit(tmp_path):
    child = tmp_path / "test_failure_then_exit.py"
    child.write_text(
        "import os\n"
        "def test_assertion():\n"
        "    assert False, 'failure evidence before process termination'\n"
        "def test_exit():\n"
        "    os._exit(23)\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(child), "-q", "-p", "tests.conftest",
         "-c", str(REPO_ROOT / "pyproject.toml"), f"--confcutdir={tmp_path}",
         f"--basetemp={tmp_path / 'child-tmp'}"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 23, result.stdout + result.stderr
    assert "failure evidence before process termination" in result.stderr
    assert "test_assertion [call]" in result.stderr
    assert "short test summary" not in result.stdout


@pytest.mark.parametrize("failure", ["timeout", "unavailable"])
def test_macos_crash_report_collection_survives_debugger_failure(
    failure, monkeypatch, tmp_path, capsys,
):
    steps = workflow("quality.yml")["jobs"]["platforms"]["steps"]
    report = next(step for step in steps if step.get("name") == "Report macOS native crashes")
    script = report["run"].split("python - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    (tmp_path / "solin-core.42").touch()
    reports = tmp_path / "Library/Logs/DiagnosticReports"
    reports.mkdir(parents=True)
    (reports / "Python-native.ips").write_text("native crash evidence", encoding="utf-8")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    def unavailable_debugger(command, *, timeout, check):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, timeout)
        raise OSError("debugger unavailable")

    monkeypatch.setattr(subprocess, "run", unavailable_debugger)
    exec(compile(script, "macos-crash-report-collection", "exec"), {})
    output = capsys.readouterr().out
    assert "native crash evidence" in output
    assert "::warning::Could not inspect native core" in output
    assert (tmp_path / "solin-native-crashes/Python-native.ips").read_text() == "native crash evidence"


def test_python_suite_preserves_fatal_qt_destruction_diagnostic(tmp_path):
    child = tmp_path / "test_deferred_fatal.py"
    child.write_text(
        "import os\n"
        "if os.name == 'posix':\n"
        "    import resource\n"
        "    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n"
        "from PySide6.QtCore import QObject, qFatal\n"
        "def test_deferred_destruction():\n"
        "    obj = QObject()\n"
        "    obj.destroyed.connect(lambda: qFatal('deferred Qt destruction failure'))\n"
        "    obj.deleteLater()\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(child), "-q", "-p", "tests.conftest",
         "-c", str(REPO_ROOT / "pyproject.toml"), f"--confcutdir={tmp_path}",
         f"--basetemp={tmp_path / 'child-tmp'}"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "deferred Qt destruction failure" in result.stderr


def test_all_external_actions_are_pinned_to_commits():
    for path in (REPO_ROOT / ".github/workflows").glob("*.yml"):
        for action in re.findall(r"uses: ([^\s#]+)", path.read_text()):
            assert action.startswith("./") or re.search(r"@[a-f0-9]{40}$", action), action


@pytest.mark.parametrize("failed_suite", ["contract", "integration", "unit", None])
def test_windows_quality_preserves_suite_failure_and_stops_later_suites(failed_suite):
    executable = shutil.which("pwsh")
    if executable is None:
        pytest.skip("PowerShell is not installed")
    steps = workflow("quality.yml")["jobs"]["platforms"]["steps"]
    commands = next(step["run"] for step in steps if step.get("name") == "Python suites on Windows")
    failure_path = f"tests/{failed_suite}" if failed_suite else ""
    # Simulate Python's native exit status while executing the actual CI script.
    script = (
        "function python {\n"
        "    $suite = $args[2]\n"
        "    Write-Output $suite\n"
        f"    $global:LASTEXITCODE = if ($suite -eq '{failure_path}') {{ 7 }} else {{ 0 }}\n"
        "}\n"
        + commands
        + "\nexit $LASTEXITCODE\n"
    )
    result = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == (7 if failed_suite else 0), result.stdout + result.stderr
    suites = ["contract", "integration", "unit"]
    expected = suites[:suites.index(failed_suite) + 1] if failed_suite else suites
    assert result.stdout.splitlines() == [f"tests/{suite}" for suite in expected]


def test_linux_smoke_rejects_clean_early_exit(monkeypatch):
    from tests.e2e._packaged_app import assert_process_survives_startup

    text = (REPO_ROOT / ".github/workflows/build-solin-linux.yml").read_text()
    assert "test_packaged_app_smoke.py::test_packaged_app_survives_startup_window" in text
    monkeypatch.setenv("SOLIN_E2E_STARTUP_SECONDS", "2")
    with pytest.raises(pytest.fail.Exception, match="exited during startup with 0"):
        assert_process_survives_startup(Path(sys.executable), args=("-c", "raise SystemExit(0)"))


def test_windows_distribution_contains_only_the_full_installer():
    text = (REPO_ROOT / ".github/workflows/build-solin-windows.yml").read_text()
    upload = text.split("- name: Upload Windows packages", 1)[1]
    assert "dist/Solin-${{ env.APP_VERSION }}-windows-x86_64.exe" in upload
    assert "steps.standalone.outputs.path" not in upload
    assert "Package standalone zip" not in text
    assert "SOLIN_SIGNING_CERTIFICATE" not in text
    assert "sign_windows_artifacts" not in text
    assert "&& '' || '-unsigned-diagnostic'" not in text


def test_platform_builders_share_pinned_packaging_tools():
    requirements = (REPO_ROOT / "requirements-build.txt").read_text().splitlines()
    assert requirements == ["Nuitka==4.1", "ordered-set==4.1.0", "zstandard==0.25.0"]
    for platform in ("windows", "linux", "macos"):
        text = (REPO_ROOT / f".github/workflows/build-solin-{platform}.yml").read_text()
        assert "requirements-build.txt" in text
        assert "Nuitka==4.1 ordered-set==4.1.0" not in text
