"""Build real archives offline and verify installed resources and runtime defaults."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
from zipfile import ZipFile

import pytest

from tests._paths import REPO_ROOT


pytestmark = pytest.mark.integration


def _run_python(directory: Path, script: str, *arguments: Path) -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.update(PIP_NO_INDEX="1", PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [sys.executable, "-c", script, *(str(argument) for argument in arguments)],
        cwd=directory,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout


def test_sdist_to_wheel_contains_current_catalogs_and_runs_without_compilation(tmp_path):
    repository = tmp_path / "repository"
    shutil.copytree(
        REPO_ROOT / "src",
        repository / "src",
        ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "*.egg-info", "*.qm", ".solin-translations*",
        ),
    )
    for filename in ("pyproject.toml", "setup.py", "MANIFEST.in", "README.md", "LICENSE", "NOTICE"):
        shutil.copy2(REPO_ROOT / filename, repository / filename)
    (repository / "scripts").mkdir()
    for filename in ("__init__.py", "setuptools_commands.py", "compile_translations.py"):
        shutil.copy2(REPO_ROOT / "scripts" / filename, repository / "scripts" / filename)

    source = repository / "src" / "solin" / "resources" / "translations"
    catalogs = {path.stem for path in source.glob("*.ts")}
    locales = {path.stem for path in (source / "locales").glob("*.json")}
    assert len(catalogs) == len(locales) == 18
    assert catalogs == {f"solin_{locale}" for locale in locales}
    # Even accidental source-tree build artifacts must stay out of both archives.
    (source / "solin_en.qm").write_bytes(b"untrusted stale catalog")
    (source / ".solin-translations.json").write_text("{}", encoding="utf-8")
    (source / ".solin-translations.lock").write_bytes(b"\0")

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    _run_python(
        repository,
        "import sys; from setuptools import build_meta; build_meta.build_sdist(sys.argv[1])",
        artifacts,
    )
    [sdist] = artifacts.glob("*.tar.gz")
    extracted = tmp_path / "extracted"
    with tarfile.open(sdist) as archive:
        files = {PurePosixPath(member.name) for member in archive.getmembers() if member.isfile()}
        source_catalogs = [path for path in files if path.suffix == ".ts"]
        assert len(source_catalogs) == 18
        assert {path.stem for path in source_catalogs} == catalogs
        assert not any(path.suffix == ".qm" for path in files)
        assert not any(part.startswith(".solin-translations") for path in files for part in path.parts)
        archive.extractall(extracted, filter="data")

    [sdist_root] = extracted.iterdir()
    # The unpacked sdist is the only project input to this backend invocation.
    _run_python(
        sdist_root,
        "import sys; from setuptools import build_meta; build_meta.build_wheel(sys.argv[1])",
        artifacts,
    )
    [wheel] = artifacts.glob("*.whl")
    installed = tmp_path / "site-packages"
    with ZipFile(wheel) as archive:
        files = {PurePosixPath(name) for name in archive.namelist() if not name.endswith("/")}
        translation_root = PurePosixPath("solin/resources/translations")
        payload = {path.relative_to(translation_root) for path in files if path.is_relative_to(translation_root)}
        expected = {PurePosixPath(f"{catalog}.qm") for catalog in catalogs}
        expected.update(PurePosixPath("locales") / f"{locale}.json" for locale in locales)
        assert payload == expected
        assert not any(path.suffix == ".ts" for path in files)
        assert not any(part.startswith(".solin-translations") for path in files for part in path.parts)
        archive.extractall(installed)

    before = {path.relative_to(installed) for path in installed.rglob("*")}
    _run_python(
        tmp_path,
        """
from pathlib import Path
import logging
import os
import sys
from unittest.mock import patch
from urllib.parse import urlsplit

installed = Path(sys.argv[1])
sys.path.insert(0, str(installed))
# Fail immediately if installed resource preparation tries to import the compiler.
sys.modules["solin.core.i18n.compiler"] = None
from solin.bootstrap.translations import prepare_application_catalogs
from solin.bootstrap.config import default_app_config
from solin.core.foundation import constants
from solin.core.foundation.logging_config import LOG_HANDLER_NAME, configure_logging
from solin.core.foundation.resources import application_translation_root, package_root
from solin.core.foundation.settings_store import GlobalSettingsStore, InstallationSettingsStore
from solin.core.profiles.settings import ProfileSettings
from solin.core.scenes import camera_discovery_process, libobs_engine
from PySide6.QtCore import QCoreApplication, QTranslator

assert package_root() == installed / "solin"
assert constants.IS_DEV is False
assert constants.IS_COMPILED is False
assert camera_discovery_process._command() == [
    sys.executable, "-m", "solin.core.scenes.camera_discovery_process",
]
assert constants.QT_APPLICATION_NAME == constants.QT_ORGANIZATION_NAME == "Solin"
assert constants.QSETTINGS_ORG_NAME == "Solin"
assert constants.QSETTINGS_PROFILE_ORG_PREFIX == "Solin_"
assert GlobalSettingsStore.create().settings.namespace.organization == "Solin"
assert InstallationSettingsStore.create().settings.namespace.organization == "Solin"
profile = ProfileSettings.for_profile_id("main_hall")
assert profile.organization == profile.app_settings().settings.namespace.organization == "Solin_main_hall"
assert constants.IPC_SERVER_NAME == "Solin_IPC_v1"
notification_url = urlsplit(constants.NOTIFICATION_API_URL)
assert notification_url.scheme == "https"
assert notification_url.hostname == "solinav.vercel.app"
assert notification_url.path == "/v1/notifications"

# Inspect the factory boundary without starting a supervisor or native runtime.
with patch.object(libobs_engine, "SubprocessSceneEngine", autospec=True) as supervisor:
    assert libobs_engine.create_libobs_scene_engine() is supervisor.return_value
    supervisor.assert_called_once()
    process_config = supervisor.call_args.args[0]
    assert process_config.executable == Path(sys.executable)
    assert process_config.arguments == ("-m", "solin.core.scenes.libobs_sidecar")
    supervisor.return_value.start.assert_not_called()

os.environ.pop("SOLIN_LOG_LEVEL", None)
try:
    configure_logging(installed.parent / "logs")
    assert logging.getLogger().level == logging.INFO
    [handler] = [
        handler for handler in logging.getLogger().handlers
        if handler.name == LOG_HANDLER_NAME
    ]
    assert handler.level == logging.INFO
finally:
    logging.shutdown()

expected = installed / "solin" / "resources" / "translations"
assert application_translation_root() == prepare_application_catalogs() == expected
assert len(list(expected.glob("*.qm"))) == 18
assert len(list((expected / "locales").glob("*.json"))) == 18
app = QCoreApplication([])
default_app_config().apply_to(app)
assert app.applicationName() == app.organizationName() == "Solin"
for catalog in expected.glob("*.qm"):
    translator = QTranslator()
    assert translator.load(str(catalog)), catalog.name
    assert not translator.isEmpty(), catalog.name
    del translator
assert sys.modules["solin.core.i18n.compiler"] is None
""",
        installed,
    )
    assert {path.relative_to(installed) for path in installed.rglob("*")} == before
