from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from solin.bootstrap.macos_dev_host import prepare_development_bundle
from tests._paths import REPO_ROOT


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS framework Python host")
def test_native_bundle_retains_environment_and_child_bundle_identity():
    host = prepare_development_bundle(REPO_ROOT)
    before = host.stat().st_mtime_ns
    assert prepare_development_bundle(REPO_ROOT) == host
    assert host.stat().st_mtime_ns == before
    inspect = """
import json,sys,site
from Foundation import NSBundle
import PySide6,solin
bundle=NSBundle.mainBundle()
print(json.dumps({'prefix':sys.prefix,'executable':sys.executable,
 'bundle':str(bundle.bundlePath()),
 'camera':bool(bundle.objectForInfoDictionaryKey_('NSCameraUsageDescription')),
 'microphone':bool(bundle.objectForInfoDictionaryKey_('NSMicrophoneUsageDescription')),
 'qt':PySide6.__file__,'solin':solin.__file__,'user_site':site.ENABLE_USER_SITE}))
"""
    environment = dict(os.environ)
    environment.pop("__PYVENV_LAUNCHER__", None)
    environment["SOLIN_DEV_INSPECT"] = inspect
    result = subprocess.run(
        [
            str(host),
            "-c",
            "import os,subprocess,sys;exec(os.environ['SOLIN_DEV_INSPECT']);"
            "raise SystemExit(subprocess.call([sys.executable,'-c',os.environ['SOLIN_DEV_INSPECT']]))",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    parent, child = [json.loads(line) for line in result.stdout.splitlines()]
    for process in (parent, child):
        assert Path(process["prefix"]) == Path(sys.prefix)
        assert Path(process["executable"]) == host
        assert Path(process["bundle"]) == host.parents[2]
        assert process["camera"] and process["microphone"]
        import site

        assert process["user_site"] == site.ENABLE_USER_SITE
        assert Path(process["qt"]).is_relative_to(sys.prefix)
        assert Path(process["solin"]).is_relative_to(REPO_ROOT / "src")
    subprocess.run(["codesign", "--verify", "--strict", str(host.parents[2])], check=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS framework Python host")
def test_native_host_supports_non_ascii_and_escaped_checkout_paths(tmp_path):
    repository = tmp_path / 'Développement "camera" \\ host'
    source = repository / "native/dev_host/macos.c"
    source.parent.mkdir(parents=True)
    shutil.copyfile(REPO_ROOT / "native/dev_host/macos.c", source)
    host = prepare_development_bundle(repository)
    result = subprocess.run(
        [
            str(host),
            "-c",
            "import json,sys;from Foundation import NSBundle;"
            "print(json.dumps([sys.executable,str(NSBundle.mainBundle().bundlePath())]))",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    executable, bundle = json.loads(result.stdout)
    assert Path(executable) == host
    assert Path(bundle).samefile(host.parents[2])
