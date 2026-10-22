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


def test_beta_dispatch_authorizes_before_environment_and_app_credentials():
    configuration = workflow("prepare-beta.yml")
    assert configuration["name"] == "Generate beta"
    assert set(configuration["on"]) == {"workflow_dispatch"}
    assert configuration["permissions"] == {"contents": "read"}
    assert configuration["concurrency"] == {
        "group": "solin-beta-preparation", "cancel-in-progress": "false",
    }
    assert configuration["env"] == {
        "BETA_RELEASE_ACTORS": "${{ vars.BETA_RELEASE_ACTORS }}",
        "BETA_RELEASE_TRIGGERING_ACTOR": "${{ github.triggering_actor }}",
    }
    jobs = configuration["jobs"]
    assert set(jobs) == {"authorize", "beta"}
    assert "environment" not in jobs["authorize"]
    assert jobs["beta"]["needs"] == "authorize"
    assert jobs["beta"]["environment"] == "beta-release"
    assert jobs["beta"]["if"] == "github.ref == 'refs/heads/main'"
    assert int(jobs["beta"]["timeout-minutes"]) < 60  # Installation token lifetime.
    for job in jobs.values():
        steps = job["steps"]
        checkout = next(step for step in steps if "actions/checkout@" in step.get("uses", ""))
        assert checkout["with"]["persist-credentials"] == "false"
        authorization = next(i for i, step in enumerate(steps)
                             if step.get("run") == "python scripts/prepare_beta.py authorize")
        app = next((i for i, step in enumerate(steps) if step.get("id") == "app"), None)
        if app is not None:
            assert authorization < app
            assert steps[app]["with"] == {
                "app-id": "${{ vars.BETA_RELEASE_APP_ID }}",
                "private-key": "${{ secrets.BETA_RELEASE_APP_PRIVATE_KEY }}",
                "permission-contents": "write", "permission-pull-requests": "write",
                "permission-checks": "read", "permission-statuses": "read",
                "permission-actions": "read",
                "permission-workflows": "write",
            }
            assert checkout["with"]["ref"] == "main"
            assert checkout["with"]["fetch-depth"] == "0"
            execution = steps[-1]
            assert execution["run"] == "python scripts/prepare_beta.py run"
            assert execution["env"]["GH_TOKEN"] == "${{ steps.app.outputs.token }}"
        else:
            assert not any("secrets." in str(step) for step in steps)
        assert all("${{" not in step.get("run", "") for step in steps)


def test_main_cache_builds_are_trusted_serialized_and_skip_release_preparation():
    configuration = workflow("main-build-cache.yml")
    assert set(configuration["on"]) == {"push", "workflow_dispatch"}
    assert configuration["on"]["push"] == {"branches": ["main"]}
    assert configuration["permissions"] == {"contents": "read"}
    assert "concurrency" not in configuration  # Classify before entering a build queue.
    assert set(configuration["jobs"]) == {"changes", "windows", "linux", "macos"}
    changes = configuration["jobs"]["changes"]
    assert changes["if"] == "github.ref == 'refs/heads/main'"
    checkout = next(step for step in changes["steps"] if "actions/checkout@" in step.get("uses", ""))
    assert checkout["with"]["fetch-depth"] == "0"
    quality = workflow("quality.yml")["jobs"]["changes"]
    classifier = next(step for step in changes["steps"] if step.get("id") == "classify")
    quality_classifier = next(step for step in quality["steps"] if step.get("id") == "classify")
    assert classifier["run"] == quality_classifier["run"]
    assert classifier["env"] == {
        "BASE_SHA": "${{ github.event.before }}",
        "HEAD_SHA": "${{ github.sha }}",
        "REF_TYPE": "${{ github.ref_type }}",
    }  # Manual dispatch has no base, so the classifier requests a full build.
    assert changes["outputs"]["application_required"] == quality["outputs"]["application_required"]
    for platform in ("windows", "linux", "macos"):
        job = configuration["jobs"][platform]
        assert job["needs"] == "changes"
        assert job["if"] == "needs.changes.outputs.application_required == 'true'"
        assert job["uses"] == f"./.github/workflows/build-solin-{platform}.yml"
        assert job["with"] == {"release": "false", "retention_days": "1"}
        assert "secrets" not in job
        concurrency = workflow(f"build-solin-{platform}.yml")["concurrency"]
        assert concurrency["cancel-in-progress"] == "false"
        assert "github.ref" in concurrency["group"]


@pytest.mark.parametrize("platform", ["windows", "linux", "macos"])
def test_compilation_snapshots_share_a_path_and_restore_only_compatible_inputs(platform):
    configuration = workflow(f"build-solin-{platform}.yml")
    assert configuration["env"]["NUITKA_CACHE_DIR"] == "${{ github.workspace }}/.cache/nuitka"
    caches = [
        step["with"] for step in configuration["jobs"]["build"]["steps"]
        if "actions/cache@" in step.get("uses", "")
        and "-pip-" not in step["with"]["key"]
    ]
    assert len(caches) == 1  # Nuitka owns the C compiler cache under the same root.
    cache = caches[0]
    assert cache["path"] == "${{ env.NUITKA_CACHE_DIR }}"
    prefix = cache["restore-keys"].strip()
    assert cache["key"] == prefix + "${{ github.sha }}"
    assert "\n" not in prefix  # No fallback across Python or dependency/tool versions.
    assert all(component in prefix for component in (
        "${{ runner.os }}", "${{ runner.arch }}", "${{ env.PYTHON_VERSION }}",
        "hashFiles('requirements*.txt', 'pyproject.toml')",
    ))
    assert "github.ref" not in prefix  # Main snapshots remain discoverable from tags.


@pytest.mark.parametrize("name,job_name,contents", [
    ("release.yml", "publish", "write"),
    ("release-contributors.yml", None, "read"),
    ("release-contributors.yml", "contributors", "write"),
])
def test_related_release_notes_have_explicit_pr_and_issue_read_permissions(
    name, job_name, contents,
):
    configuration = workflow(name)
    scope = configuration["jobs"][job_name] if job_name else configuration
    assert scope["permissions"] == {
        "contents": contents, "pull-requests": "read", "issues": "read",
    }
    if name == "release.yml":
        assert configuration["permissions"] == {"contents": "read"}


def test_contributor_maintenance_is_manual_body_only_and_serialized_with_publication():
    release = workflow("release.yml")
    manual = workflow("release-contributors.yml")
    assert set(manual["on"]) == {"workflow_dispatch"}
    inputs = manual["on"]["workflow_dispatch"]["inputs"]
    assert inputs["tag"]["required"] == "true" and inputs["tag"]["type"] == "string"
    assert inputs["apply"]["type"] == "boolean" and inputs["apply"]["default"] == "false"
    assert set(manual["jobs"]) == {"contributors"}
    job = manual["jobs"]["contributors"]
    assert job["concurrency"] == release["jobs"]["publish"]["concurrency"]
    checkout = next(step for step in job["steps"] if "actions/checkout@" in step.get("uses", ""))
    assert checkout["with"]["fetch-depth"] == "0"
    assert checkout["with"]["ref"] == "main"
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "git fetch origin main --tags" in commands
    assert "scripts/release.py contributors" in commands
    assert "scripts/release.py publish" not in commands
    assert "${{" not in commands  # Inputs enter through env, never shell interpolation.
    credits = job["steps"][-1]
    assert credits["env"]["RELEASE_TAG"] == "${{ inputs.tag }}"
    assert credits["env"]["APPLY_CREDITS"] == "${{ inputs.apply }}"
    assert credits["env"]["GH_TOKEN"] == "${{ secrets.RELEASE_NOTES_TOKEN || github.token }}"


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
    assert steps["Virtual-camera transport tests"]["if"] == "runner.os == 'Linux'"
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


@pytest.mark.parametrize("notes", ["reviewed", "empty", "missing", "outdated", "placeholder"])
def test_reduced_preparation_accepts_empty_beta_notes_but_requires_matching_files(
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
        content = {"placeholder": "<!-- TODO: Review notes. -->", "empty": ""}.get(notes, "Reviewed changes")
        (directory / "en.md").write_text(content, encoding="utf-8")
    monkeypatch.setattr(release, "ROOT", tmp_path)
    monkeypatch.setattr(release, "NOTES", note_root)
    preparation = next(
        step for step in workflow("quality.yml")["jobs"]["static"]["steps"]
        if step.get("name") == "Validate release preparation"
    )
    script = preparation["run"].split("python - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    if notes in {"reviewed", "empty"}:
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
    # The intentional fatal exit must not wait for Windows crash reporting.
    # Configure only this child; preserve the parent suite's crash diagnostics.
    child.write_text(
        "import os\n"
        "if os.name == 'nt':\n"
        "    import ctypes\n"
        "    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)\n"
        "    kernel32.GetErrorMode.restype = ctypes.c_uint\n"
        "    kernel32.SetErrorMode.argtypes = [ctypes.c_uint]\n"
        "    kernel32.SetErrorMode.restype = ctypes.c_uint\n"
        "    SEM_NOGPFAULTERRORBOX = 0x0002\n"
        "    kernel32.SetErrorMode(kernel32.GetErrorMode() | SEM_NOGPFAULTERRORBOX)\n"
        "elif os.name == 'posix':\n"
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
        for job in workflow(path.name)["jobs"].values():
            for entry in (job, *job.get("steps", [])):
                if action := entry.get("uses"):
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


@pytest.mark.parametrize("exit_code", [0, 7])
def test_windows_zoom_qualification_waits_for_process_and_propagates_failure(
    tmp_path, monkeypatch, exit_code,
):
    monkeypatch.setenv("PYTHONFAULTHANDLER", "1")
    steps = workflow("build-solin-windows.yml")["jobs"]["build"]["steps"]
    step = next(item for item in steps if item.get("name") == "Qualify packaged Zoom automation runtime")
    assert "if" not in step  # Both manual and release builds must qualify the artifact.
    assert step["shell"] == "python"
    build_dir = tmp_path / "build staging"
    monkeypatch.setenv("BUILD_DIR", str(build_dir))
    completed = tmp_path / "completed"
    interpreter = (
        Path(sys.executable).with_name("pythonw.exe") if os.name == "nt" else Path(sys.executable)
    )
    child = (
        "import faulthandler; from pathlib import Path; "
        "assert faulthandler.is_enabled(); "
        f"Path({str(completed)!r}).touch(); "
        f"raise SystemExit({exit_code})"
    )
    run_process = subprocess.run

    def launch(command, **kwargs):
        assert command == [str(build_dir / "main.dist" / "Solin.exe"), "--verify-zoom-runtime"]
        assert kwargs["check"] is True
        assert 0 < kwargs["timeout"] <= 60
        # Use a real GUI-subsystem process on Windows, without opening a window.
        # Explicit streams let pythonw initialize CI's inherited faulthandler.
        return run_process([str(interpreter), "-c", child], capture_output=True, text=True, **kwargs)

    monkeypatch.setattr(subprocess, "run", launch)
    if exit_code:
        with pytest.raises(subprocess.CalledProcessError) as failure:
            exec(step["run"], {})
        assert failure.value.returncode == exit_code, failure.value.stderr
    else:
        exec(step["run"], {})
    assert completed.exists()


def test_linux_smoke_rejects_clean_early_exit(monkeypatch):
    from tests.e2e._packaged_app import assert_process_survives_startup

    text = (REPO_ROOT / ".github/workflows/build-solin-linux.yml").read_text()
    assert "test_packaged_app_smoke.py::test_packaged_app_survives_startup_window" in text
    monkeypatch.setenv("SOLIN_E2E_STARTUP_SECONDS", "2")
    with pytest.raises(pytest.fail.Exception, match="exited during startup with 0"):
        assert_process_survives_startup(Path(sys.executable), args=("-c", "raise SystemExit(0)"))


@pytest.mark.parametrize("platform", ["windows", "macos", "linux"])
def test_packaged_smoke_selection_respects_platform_with_strict_skips(platform):
    configuration = workflow(f"build-solin-{platform}.yml")
    assert configuration["env"]["SOLIN_E2E_STRICT"] == "1"
    steps = configuration["jobs"]["build"]["steps"]
    step_name = "Run AppImage startup smoke" if platform == "linux" else "Run packaged startup smoke"
    commands = next(step["run"] for step in steps if step.get("name") == step_name)
    # Workflow paths must also be collectable on hosts with POSIX separators.
    targets = [
        target.replace("\\", "/")
        for target in re.findall(r"tests[/\\]e2e[/\\][\w]+\.py(?:::[\w]+)?", commands)
    ]
    assert targets
    # Collect the actual workflow targets. Stub artifact checks, while retaining
    # pytest's real skip handling and the release suite's strict reporting hook.
    script = '''
import pytest

def windows_gate(mark):
    return mark.name == "skipif" and mark.kwargs.get("reason") == "Windows UI Automation runtime"

class ArtifactChecks:
    def pytest_collection_modifyitems(self, items):
        windows_items = [item for item in items if any(windows_gate(m) for m in item.iter_markers())]
        assert items
        if PLATFORM == "windows":
            assert windows_items, "Windows runtime qualification was not selected"
        # Apply the target platform's guard rather than the host's guard.
        for item in items:
            for node in item.listchain():
                node.own_markers[:] = [m for m in node.own_markers if not windows_gate(m)]
            item.obj = lambda **fixtures: None
        if PLATFORM != "windows":
            for item in windows_items:
                item.add_marker(pytest.mark.skip(reason="Windows UI Automation runtime"))

raise SystemExit(pytest.main(TARGETS + ["-q"], plugins=[ArtifactChecks()]))
'''
    environment = os.environ.copy()
    environment["SOLIN_E2E_STRICT"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", f"PLATFORM = {platform!r}\nTARGETS = {targets!r}\n" + script],
        cwd=REPO_ROOT, env=environment, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("outcome", ["pass", "skip", "fail"])
def test_strict_packaged_acceptance_rejects_missing_or_failed_checks(tmp_path, outcome):
    child = tmp_path / "test_acceptance.py"
    body = {
        "pass": "pass",
        "skip": "pytest.skip('required artifact is missing')",
        "fail": "pytest.fail('artifact qualification failed')",
    }[outcome]
    child.write_text(f"import pytest\ndef test_artifact():\n    {body}\n", encoding="utf-8")
    environment = os.environ.copy()
    environment["SOLIN_E2E_STRICT"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(child), "-q", "-p", "tests.e2e.conftest",
         "-c", str(REPO_ROOT / "pyproject.toml"), f"--confcutdir={tmp_path}"],
        cwd=REPO_ROOT, env=environment, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == (0 if outcome == "pass" else 1), result.stdout + result.stderr
    if outcome == "skip":
        assert "Required release test skipped" in result.stdout


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
