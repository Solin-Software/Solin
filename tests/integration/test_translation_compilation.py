from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from PySide6.QtCore import QTranslator

from solin.core.i18n.compiler import compile_catalogs
from tests._paths import REPO_ROOT


def _write_catalog(path: Path, translation: str) -> None:
    path.write_text(
        '<TS version="2.1" language="pt_BR"><context><name>Test</name>'
        f'<message><source>Camera</source><translation>{translation}</translation></message>'
        '</context></TS>',
        encoding="utf-8",
    )


def test_rebuild_can_replace_a_catalog_that_qt_has_already_loaded(tmp_path):
    source, output = tmp_path / "source", tmp_path / "output"
    source.mkdir()
    ts = source / "solin_pt_BR.ts"
    _write_catalog(ts, "Before")
    compile_catalogs(source, output)
    existing = QTranslator()
    assert existing.load(str(output / "solin_pt_BR.qm"))
    assert existing.translate("Test", "Camera") == "Before"

    _write_catalog(ts, "After")
    compile_catalogs(source, output)

    updated = QTranslator()
    assert updated.load(str(output / "solin_pt_BR.qm"))
    assert updated.translate("Test", "Camera") == "After"
    assert existing.translate("Test", "Camera") == "Before"


def test_independent_processes_prepare_the_same_directory_without_importing_qt(tmp_path):
    source, output = tmp_path / "sources with spaces", tmp_path / "compiled with spaces"
    source.mkdir()
    _write_catalog(source / "solin_pt_BR.ts", "Current")
    code = (
        "from pathlib import Path; import sys; "
        "from solin.core.i18n.compiler import compile_catalogs; "
        "compile_catalogs(Path(sys.argv[1]), Path(sys.argv[2])); "
        "assert 'PySide6.QtCore' not in sys.modules; print('prepared')"
    )
    environment = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    processes = []
    try:
        for _ in range(3):
            processes.append(subprocess.Popen(
                [sys.executable, "-c", code, str(source), str(output)],
                cwd=tmp_path,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ))
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, stderr
            assert stdout.strip() == "prepared"
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate()
    translator = QTranslator()
    assert translator.load(str(output / "solin_pt_BR.qm"))
    assert translator.translate("Test", "Camera") == "Current"
    assert not list(output.glob(".solin-translations-*/"))
