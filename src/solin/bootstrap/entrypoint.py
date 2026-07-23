from __future__ import annotations

from .startup_timeline import startup_timeline


startup_timeline().mark("entrypoint")


def run() -> None:
    """Import the application only after the startup clock has begun."""

    from .application import run as run_application

    startup_timeline().mark("application_imported")
    run_application()


__all__ = ["run"]
