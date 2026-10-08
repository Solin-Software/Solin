from __future__ import annotations

import sys

from solin.core.foundation.constants import LIBOBS_SIDECAR_ARGUMENT

from .startup_timeline import startup_timeline


startup_timeline().mark("entrypoint")


def run() -> None:
    """Dispatch the process role before importing application services or Qt."""

    arguments = sys.argv[1:]
    if any(argument.split("=", 1)[0] == "--verify-http-runtime" for argument in arguments):
        from .runtime_verification import main as verify_http_runtime

        raise SystemExit(verify_http_runtime(arguments))

    if LIBOBS_SIDECAR_ARGUMENT in arguments:
        if arguments != [LIBOBS_SIDECAR_ARGUMENT]:
            raise SystemExit("The scene engine sidecar accepts no application arguments.")
        from solin.core.scenes.libobs_sidecar import main as run_sidecar

        raise SystemExit(run_sidecar())

    from .application import run as run_application

    startup_timeline().mark("application_imported")
    run_application()


__all__ = ["run"]
