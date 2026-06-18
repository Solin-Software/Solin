from __future__ import annotations

from dataclasses import dataclass

from solin.core.foundation.constants import (
    APP_VERSION,
    DISPLAY_APP_NAME,
    QT_APPLICATION_NAME,
    QT_ORGANIZATION_NAME,
)


@dataclass(frozen=True, slots=True)
class AppConfig:
    display_name: str
    qt_application_name: str
    qt_organization_name: str
    version: str

    def apply_to(self, app) -> None:
        app.setApplicationName(self.qt_application_name)
        app.setOrganizationName(self.qt_organization_name)
        app.setApplicationVersion(self.version)


def default_app_config() -> AppConfig:
    return AppConfig(
        display_name=DISPLAY_APP_NAME,
        qt_application_name=QT_APPLICATION_NAME,
        qt_organization_name=QT_ORGANIZATION_NAME,
        version=APP_VERSION,
    )
