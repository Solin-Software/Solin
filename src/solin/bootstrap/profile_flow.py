from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QProcess

from solin.bootstrap import single_instance
from solin.core.foundation.settings_store import GlobalSettingsStore
from solin.core.profiles.manager import ProfileManager

log = logging.getLogger(__name__)


def _log_ignored_exception(message: str, *, warning: bool = False) -> None:
    if warning:
        log.warning(message, exc_info=True)
    else:
        log.debug(message, exc_info=True)


def relaunch_with_profile(
    app,
    profile_id: str,
    window,
    profile_manager: ProfileManager,
) -> None:
    """
    Switch profiles across a process boundary.

    The old window is closed in the current profile context and a new process is
    started with --profile <id>, avoiding native browser resource reuse.
    """
    if not profile_manager.get_profile(profile_id):
        return

    GlobalSettingsStore.create().set_last_active_profile(profile_id)

    ipc_server = single_instance.stop_process_ipc(app, window)

    program, args, working_directory = detached_launch_command(
        ["--profile", profile_id]
    )

    if not QProcess.startDetached(program, args, working_directory):
        try:
            if window is not None:
                window.setEnabled(True)
                single_instance.restart_process_ipc(app, ipc_server, window)
                window.show()
        except Exception:  # noqa: BLE001 - Qt relaunch recovery boundary
            _log_ignored_exception("Could not restore window after profile relaunch failure", warning=True)
        return

    try:
        if window is not None:
            window.close()
    except Exception:  # noqa: BLE001 - Qt window lifecycle boundary
        _log_ignored_exception("Could not close old window after profile relaunch")
    app.processEvents()
    app.quit()


def relaunch_to_profile_creator(
    app,
    window,
    profile_manager: ProfileManager,
) -> None:
    """Reopen Solin directly in onboarding for creating an additional profile."""
    try:
        current_lang = (
            profile_manager.settings_for().app_settings().app_language()
            if profile_manager.active_id
            else ""
        )
        if current_lang:
            GlobalSettingsStore.create().set_bootstrap_language(current_lang)
    except Exception:  # noqa: BLE001 - Qt settings adapter boundary
        _log_ignored_exception("Could not persist bootstrap language for profile creation")

    ipc_server = single_instance.stop_process_ipc(app, window)

    program, args, working_directory = detached_launch_command(
        ["--create-profile"]
    )

    if not QProcess.startDetached(program, args, working_directory):
        try:
            if window is not None:
                window.setEnabled(True)
                single_instance.restart_process_ipc(app, ipc_server, window)
                window.show()
        except Exception:  # noqa: BLE001 - Qt relaunch recovery boundary
            _log_ignored_exception("Could not restore window after profile-creator relaunch failure", warning=True)
        return

    try:
        if window is not None:
            window.close()
    except Exception:  # noqa: BLE001 - Qt window lifecycle boundary
        _log_ignored_exception("Could not close old window after profile-creator relaunch")
    app.processEvents()
    app.quit()


def detached_launch_command(runtime_args: list[str]) -> tuple[str, list[str], str]:
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        program = QCoreApplication.applicationFilePath() or sys.executable
        working_directory = (
            QCoreApplication.applicationDirPath() or str(Path.cwd())
        )
        return program, runtime_args, working_directory

    project_root = Path(__file__).resolve().parents[3]
    return (
        sys.executable,
        [str(project_root / "main.py"), *runtime_args],
        str(project_root),
    )


def wire_profile_switch(
    app,
    window_ref,
    profile_manager: ProfileManager,
):
    """
    Connect MainWindow's switch request signal to the profile-selection overlay.
    """
    window = window_ref[0]
    if not hasattr(window, "switch_profile_requested"):
        return

    def _on_switch():
        from solin.ui.profile_switch_overlay import ProfileSwitchOverlay

        current_id = (
            profile_manager.active_profile.id
            if profile_manager.active_profile
            else ""
        )

        old_win = window_ref[0]
        if old_win is None:
            return

        overlay = ProfileSwitchOverlay(
            old_win,
            current_id,
            profile_manager=profile_manager,
        )
        overlay.show()
        overlay.raise_()

        def _on_cancelled():
            try:
                old_win.removeEventFilter(overlay)
            except Exception:  # noqa: BLE001 - Qt event-filter cleanup boundary
                _log_ignored_exception("Could not remove profile switch event filter on cancel")
            overlay.deleteLater()

        def _on_profile_selected(profile_id: str):
            try:
                old_win.removeEventFilter(overlay)
            except Exception:  # noqa: BLE001 - Qt event-filter cleanup boundary
                _log_ignored_exception("Could not remove profile switch event filter before relaunch")
            overlay.deleteLater()

            old_win.setEnabled(False)
            relaunch_with_profile(
                app,
                profile_id,
                old_win,
                profile_manager,
            )

        def _on_create_profile_requested():
            try:
                old_win.removeEventFilter(overlay)
            except Exception:  # noqa: BLE001 - Qt event-filter cleanup boundary
                _log_ignored_exception("Could not remove profile switch event filter before profile creation")
            overlay.deleteLater()

            old_win.setEnabled(False)
            relaunch_to_profile_creator(app, old_win, profile_manager)

        overlay.cancelled.connect(_on_cancelled)
        overlay.profile_selected.connect(_on_profile_selected)
        overlay.create_profile_requested.connect(_on_create_profile_requested)

    window.switch_profile_requested.connect(_on_switch)
