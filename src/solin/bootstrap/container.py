from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from solin.bootstrap.config import AppConfig
from solin.bootstrap.lifecycle import ApplicationLifecycle
from solin.core.foundation.runtime_paths import RuntimePaths
from solin.core.foundation.settings_store import (
    GlobalSettingsStore,
    InstallationSettingsStore,
)

if TYPE_CHECKING:
    from solin.bootstrap.media import MediaComposition
    from solin.core.jw.catalog import JWMediaCatalogCachePaths
    from solin.core.jw.language_settings import JWLanguageSettingsStore
    from solin.core.jw.songs import JWSongsStore
    from solin.core.meetings.jwpub_cache import JwpubChecksumStore
    from solin.core.onboarding.application import OnboardingService
    from solin.core.profiles.application import ProfileService
    from solin.core.profiles.infrastructure import ProfileRuntimeContextFactory
    from solin.core.profiles.settings import ProfileSettings
    from solin.core.rendering.fonts import FontManager

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ApplicationContainer:
    app: Any
    config: AppConfig
    runtime_paths: RuntimePaths
    global_settings: GlobalSettingsStore
    installation_settings: InstallationSettingsStore
    media: MediaComposition
    font_manager: FontManager
    jw_catalog_cache_paths: JWMediaCatalogCachePaths
    jw_language_settings_store_factory: Callable[
        [ProfileSettings], JWLanguageSettingsStore
    ]
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
    from solin.core.jw.language_settings import JWLanguageSettingsStore
    from solin.core.jw.songs import JWSongsStore
    from solin.core.meetings.jwpub_cache import JwpubChecksumStore
    from solin.core.profiles.infrastructure import create_local_profile_service
    from solin.core.onboarding.application import OnboardingService
    from solin.core.integrations.automation.settings import (
        AutoShareSettingsStore,
        OBSSettingsStore,
    )
    from solin.core.media.settings import MediaSettingsStore
    from solin.core.onboarding.infrastructure import (
        OnboardingSettingsStores,
        QSettingsOnboardingSettings,
    )
    from solin.core.profiles.infrastructure import ProfileRuntimeContextFactory
    from solin.core.profiles.settings import ProfileSettings
    from solin.core.rendering.fonts import FontManager

    runtime_paths = RuntimePaths.from_standard_locations()
    runtime_paths.ensure_dirs()

    configure_logging(os.fspath(runtime_paths.log_dir))
    log.info("Starting %s %s", config.display_name, config.version)

    global_settings = GlobalSettingsStore.create()
    installation_settings = InstallationSettingsStore.create()
    profile_runtime = ProfileRuntimeContextFactory(
        runtime_paths.data_dir,
        runtime_paths.cache_dir,
        profile_settings_for=ProfileSettings.for_profile_id,
    )
    profile_service = create_local_profile_service(
        os.fspath(runtime_paths.data_dir),
        cache_dir=os.fspath(runtime_paths.cache_dir),
        global_settings=global_settings,
        profile_app_settings_for=lambda profile_id: (
            profile_runtime.create(profile_id).settings.app_settings()
        ),
    )

    def onboarding_settings_for_profile(profile_id: str) -> OnboardingSettingsStores:
        settings = profile_runtime.create(profile_id).settings
        return OnboardingSettingsStores(
            app_settings=settings.app_settings(),
            media_language_settings=JWLanguageSettingsStore.for_profile_settings(
                settings
            ),
            media_settings=MediaSettingsStore.for_profile_settings(settings),
            obs_settings=OBSSettingsStore.for_profile_settings(settings),
            auto_share_settings=AutoShareSettingsStore.for_profile_settings(settings),
        )

    onboarding_service = OnboardingService(
        profile_service,
        QSettingsOnboardingSettings(onboarding_settings_for_profile),
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
    lifecycle.register_cleanup(media.shutdown)
    lifecycle.register_cleanup(font_manager.shutdown)
    lifecycle.register_cleanup(jw_songs_store.shutdown)

    return ApplicationContainer(
        app=app,
        config=config,
        runtime_paths=runtime_paths,
        global_settings=global_settings,
        installation_settings=installation_settings,
        media=media,
        font_manager=font_manager,
        jw_catalog_cache_paths=jw_catalog_cache_paths,
        jw_language_settings_store_factory=(
            JWLanguageSettingsStore.for_profile_settings
        ),
        jw_songs_store=jw_songs_store,
        jwpub_checksum_store=jwpub_checksum_store,
        profile_service=profile_service,
        profile_runtime=profile_runtime,
        onboarding_service=onboarding_service,
        lifecycle=lifecycle,
    )
