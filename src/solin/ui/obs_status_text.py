"""Translated UI text for OBS connection states and known probe errors."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication

from solin.core.integrations.automation.obs import OBSConnectionState


def _disconnected_text(source: str) -> str:
    if source == "Not connected":
        return QCoreApplication.translate("OBSConnectionStatus", "Not connected")
    return QCoreApplication.translate("OBSConnectionStatus", "Disconnected")


def translated_obs_error_message(message: str) -> str:
    """Return a localized message for known OBS errors, preserving unknown details."""

    source = message.strip()
    if not source:
        return ""
    if source == "Incorrect password." or (
        "authentication failed" in source.casefold()
        and "password" in source.casefold()
    ):
        return QCoreApplication.translate("OBSConnectionStatus", "Incorrect password.")
    if source == "OBS requires a password but none was provided.":
        return QCoreApplication.translate(
            "OBSConnectionStatus",
            "OBS requires a password but none was provided.",
        )
    return source


def translated_obs_status_text(
    state: OBSConnectionState,
    message: str = "",
    *,
    disconnected_source: str = "Disconnected",
    include_error_prefix: bool = True,
) -> str:
    """Return localized, user-facing OBS status text."""

    if state is OBSConnectionState.DISCONNECTED:
        return _disconnected_text(disconnected_source)
    if state is OBSConnectionState.CONNECTING:
        return QCoreApplication.translate("OBSConnectionStatus", "Connecting…")
    if state is OBSConnectionState.CONNECTED:
        return QCoreApplication.translate(
            "OBSConnectionStatus",
            "Connected to OBS Studio",
        )
    if state is OBSConnectionState.ERROR:
        detail = translated_obs_error_message(message)
        if detail:
            if include_error_prefix:
                return QCoreApplication.translate(
                    "OBSConnectionStatus",
                    "Error: {msg}",
                ).replace("{msg}", detail)
            return detail
        return QCoreApplication.translate("OBSConnectionStatus", "Connection error")
    return translated_obs_error_message(message) or _disconnected_text(
        disconnected_source
    )


__all__ = ["translated_obs_error_message", "translated_obs_status_text"]
