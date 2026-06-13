from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from solin.bootstrap.config import AppConfig
from solin.bootstrap.lifecycle import ApplicationLifecycle
from solin.core.foundation.runtime_paths import RuntimePaths
from solin.core.foundation.settings_store import GlobalSettingsStore

if TYPE_CHECKING:
    from solin.core.profiles.manager import ProfileManager

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ApplicationContainer:
    app: Any
    config: AppConfig
    runtime_paths: RuntimePaths
    global_settings: GlobalSettingsStore
    profile_manager: ProfileManager
    lifecycle: ApplicationLifecycle
    window_ref: list[Any | None] = field(default_factory=lambda: [None])


def initialize_application_container(app, config: AppConfig) -> ApplicationContainer:
    from solin.core.foundation import paths as legacy_paths
    from solin.core.foundation.logging_config import configure_logging
    from solin.core.profiles.manager import ProfileManager

    legacy_paths.init()
    runtime_paths = RuntimePaths.from_legacy_globals()
    runtime_paths.ensure_dirs()

    configure_logging(os.fspath(runtime_paths.log_dir))
    log.info("Starting %s %s", config.display_name, config.version)

    profile_manager = ProfileManager(
        os.fspath(runtime_paths.data_dir),
        cache_dir=os.fspath(runtime_paths.cache_dir),
    )
    global_settings = GlobalSettingsStore.create()

    lifecycle = ApplicationLifecycle(app)
    lifecycle.install()

    return ApplicationContainer(
        app=app,
        config=config,
        runtime_paths=runtime_paths,
        global_settings=global_settings,
        profile_manager=profile_manager,
        lifecycle=lifecycle,
    )
