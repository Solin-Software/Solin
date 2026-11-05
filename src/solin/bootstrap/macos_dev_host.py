"""Run editable Cocoa applications in a signed bundle with native permissions."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import platform
import plistlib
import shutil
import site
import subprocess
import sys
import sysconfig
import tempfile

from .macos_bundle import capture_usage_metadata


def _c_string(value: str) -> str:
    """Represent UTF-8 bytes without relying on JSON's different escape rules."""
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise RuntimeError("Development app paths must be valid UTF-8.") from error
    return '"' + "".join(f"\\{byte:03o}" for byte in encoded) + '"'


def _host_configuration(executable: Path) -> bytes:
    stdlib = Path(sysconfig.get_path("stdlib"))
    dynamic_modules = sysconfig.get_config_var("DESTSHARED")
    if not dynamic_modules:
        raise RuntimeError(
            "The Python installation does not declare its extension module directory."
        )
    values = {
        "SOLIN_PYTHON": sys.executable,
        "SOLIN_HOST": str(executable),
        "SOLIN_BASE_PYTHON": getattr(sys, "_base_executable", sys.executable),
        "SOLIN_PREFIX": sys.prefix,
        "SOLIN_EXEC_PREFIX": sys.exec_prefix,
        "SOLIN_BASE_PREFIX": sys.base_prefix,
        "SOLIN_BASE_EXEC_PREFIX": sys.base_exec_prefix,
    }
    lines = [f"#define {name} {_c_string(value)}" for name, value in values.items()]
    lines.append(f"#define SOLIN_USER_SITE {int(bool(site.ENABLE_USER_SITE))}")
    paths = [
        stdlib.parent / f"python{sys.version_info.major}{sys.version_info.minor}.zip",
        stdlib,
        Path(dynamic_modules),
        *(Path(path) for path in site.getsitepackages()),
    ]
    if not sys.flags.ignore_environment and (python_path := os.environ.get("PYTHONPATH")):
        paths[:0] = [Path(path or os.curdir).resolve() for path in python_path.split(os.pathsep)]
    lines.append(
        "#define SOLIN_STDLIB_PATHS {" + ", ".join(_c_string(str(path)) for path in paths) + "}"
    )
    return ("\n".join(lines) + "\n").encode()


def prepare_development_bundle(repository: Path) -> Path:
    """Build an isolated Python host, retaining the active environment's paths.

    The host uses CPython's initialization API; neither the interpreter's own
    bundle nor the environment's installed packages are modified.
    """
    if sys.platform != "darwin" or not sysconfig.get_config_var("PYTHONFRAMEWORK"):
        raise RuntimeError("macOS source development requires a framework build of Python.")
    source = repository / "native" / "dev_host" / "macos.c"
    include = Path(sysconfig.get_path("include"))
    framework = Path(str(sysconfig.get_config_var("PYTHONFRAMEWORKPREFIX"))) / str(
        sysconfig.get_config_var("LDLIBRARY")
    )
    if not source.is_file() or not (include / "Python.h").is_file() or not framework.is_file():
        raise RuntimeError(
            "The development checkout or framework Python headers/library are incomplete."
        )
    architecture = platform.machine()
    if architecture not in {"x86_64", "arm64"}:
        raise RuntimeError(f"Unsupported macOS development architecture: {architecture}")
    root = repository / "build" / "dev"
    bundle = root / "SolinDev.app"
    executable = bundle / "Contents" / "MacOS" / "SolinDev"
    configuration = _host_configuration(executable)
    metadata = plistlib.dumps(
        {
            "CFBundleExecutable": "SolinDev",
            "CFBundleIdentifier": "com.solin.SolinDev",
            "CFBundleName": "SolinDev",
            "CFBundleDisplayName": "Solin Dev",
            "CFBundlePackageType": "APPL",
            "CFBundleVersion": "1",
            "LSMinimumSystemVersion": "13.0",
            "NSHighResolutionCapable": True,
            **capture_usage_metadata(),
        }
    )
    fingerprint = hashlib.sha256(
        source.read_bytes()
        + configuration
        + metadata
        + sys.version.encode()
        + architecture.encode()
    ).hexdigest()
    root.mkdir(parents=True, exist_ok=True)
    # A second development launch must not replace a bundle while it is built.
    import fcntl

    with (root / ".host.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        stamp = root / "host.sha256"
        if executable.is_file() and stamp.is_file() and stamp.read_text() == fingerprint:
            return executable
        compiler = shutil.which("clang")
        signer = shutil.which("codesign")
        if compiler is None or signer is None:
            raise RuntimeError(
                "macOS source development requires the Xcode Command Line Tools (xcode-select --install)."
            )
        with tempfile.TemporaryDirectory(prefix=".host-", dir=root) as temporary:
            staging = Path(temporary)
            (staging / "host_config.h").write_bytes(configuration)
            staged_bundle = staging / "SolinDev.app"
            contents = staged_bundle / "Contents"
            (contents / "MacOS").mkdir(parents=True)
            (contents / "Info.plist").write_bytes(metadata)
            commands = (
                [
                    compiler,
                    "-arch",
                    architecture,
                    "-mmacosx-version-min=13.0",
                    str(source),
                    "-I",
                    str(include),
                    "-I",
                    str(staging),
                    str(framework),
                    "-o",
                    str(contents / "MacOS" / "SolinDev"),
                ],
                [signer, "--force", "--sign", "-", str(staged_bundle)],
            )
            for command in commands:
                result = subprocess.run(command, capture_output=True, text=True, check=False)
                if result.returncode:
                    raise RuntimeError(
                        f"Cannot prepare the development app bundle: {result.stderr.strip()}"
                    )
            if bundle.exists():
                shutil.rmtree(bundle)
            staged_bundle.rename(bundle)
            stamp.write_text(fingerprint)
    return executable


def ensure_development_bundle() -> None:
    """Replace the source GUI process before Qt initializes native permissions."""
    if sys.platform != "darwin" or os.environ.get("QT_QPA_PLATFORM", "cocoa") != "cocoa":
        return
    repository = Path(__file__).resolve().parents[3]
    expected = repository / "build" / "dev" / "SolinDev.app" / "Contents" / "MacOS" / "SolinDev"
    if Path(sys.executable) == expected:
        return
    executable = prepare_development_bundle(repository)
    environment = dict(os.environ)
    environment.pop("__PYVENV_LAUNCHER__", None)
    os.execve(executable, [str(executable), *sys.orig_argv[1:]], environment)
