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
    from solin.bootstrap.media import MediaComposition
    from solin.core.jw.catalog import JWMediaCatalogCachePaths
    from solin.core.jw.songs import JWSongsStore
    from solin.core.meetings.publications import JwpubChecksumStore
    from solin.core.onboarding.application import OnboardingService
    from solin.core.profiles.application import ProfileService
    from solin.core.profiles.infrastructure import ProfileRuntimeContextFactory
    from solin.core.rendering.fonts import FontManager

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ApplicationContainer:
    app: Any
    config: AppConfig
    runtime_paths: RuntimePaths
    global_settings: GlobalSettingsStore
    media: MediaComposition
    font_manager: FontManager
    jw_catalog_cache_paths: JWMediaCatalogCachePaths
    jw_songs_store: JWSongsStore
    jwpub_checksum_store: JwpubChecksumStore
    profile_service: ProfileService
    profile_runtime: ProfileRuntimeContextFactory
    onboarding_service: OnboardingService
    lifecycle: ApplicationLifecycle
    window_ref: list[Any | None] = field(default_factory=lambda: [None])


def initialize_application_container(app, config: AppConfig) -> ApplicationContainer:
    from solin.core.foundation.logging_config import configure_logging
    from solin.bootstrap.media import MediaComposition
    from solin.core.jw.catalog import JWMediaCatalogCachePaths
    from solin.core.jw.songs import JWSongsStore
    from solin.core.meetings.publications import JwpubChecksumStore
    from solin.core.profiles.infrastructure import create_local_profile_service
    from solin.core.onboarding.application import OnboardingService
    from solin.core.onboarding.infrastructure import QSettingsOnboardingSettings
    from solin.core.profiles.infrastructure import ProfileRuntimeContextFactory
    from solin.core.rendering.fonts import FontManager

    runtime_paths = RuntimePaths.from_standard_locations()
    runtime_paths.ensure_dirs()

    configure_logging(os.fspath(runtime_paths.log_dir))
    log.info("Starting %s %s", config.display_name, config.version)

    global_settings = GlobalSettingsStore.create()
    profile_service = create_local_profile_service(
        os.fspath(runtime_paths.data_dir),
        cache_dir=os.fspath(runtime_paths.cache_dir),
        global_settings=global_settings,
    )
    profile_runtime = ProfileRuntimeContextFactory(
        runtime_paths.data_dir,
        runtime_paths.cache_dir,
    )
    onboarding_service = OnboardingService(
        profile_service,
        QSettingsOnboardingSettings(),
    )
    media = MediaComposition(
        app,
        runtime_paths.media_cache_dir,
        runtime_paths.thumb_cache_dir,
    )
    font_manager = FontManager(runtime_paths.cache_dir)
    jw_catalog_cache_paths = JWMediaCatalogCachePaths(
        runtime_paths.cache_dir,
        runtime_paths.thumb_cache_dir,
    )
    jw_songs_store = JWSongsStore(runtime_paths.cache_dir)
    jwpub_checksum_store = JwpubChecksumStore(
        runtime_paths.jwpub_cache_dir / "checksums.json"
    )

    lifecycle = ApplicationLifecycle(app)
    lifecycle.install()
    lifecycle.register_cleanup(media.cache_manager.cancel_all)
    lifecycle.register_cleanup(font_manager.shutdown)
    lifecycle.register_cleanup(jw_songs_store.shutdown)

    return ApplicationContainer(
        app=app,
        config=config,
        runtime_paths=runtime_paths,
        global_settings=global_settings,
        media=media,
        font_manager=font_manager,
        jw_catalog_cache_paths=jw_catalog_cache_paths,
        jw_songs_store=jw_songs_store,
        jwpub_checksum_store=jwpub_checksum_store,
        profile_service=profile_service,
        profile_runtime=profile_runtime,
        onboarding_service=onboarding_service,
        lifecycle=lifecycle,
    )
