from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QCoreApplication, QObject, QProcess

from solin.bootstrap.profile_handoff import ProfileHandoff
from solin.core.profiles.application import ProfileService

if TYPE_CHECKING:
    from solin.bootstrap.application_window import ApplicationWindow

log = logging.getLogger(__name__)


class ProfileTransitionController(QObject):
    """Own selection and one recoverable process transition for a profile window."""

    def __init__(
        self,
        app,
        window: ApplicationWindow,
        profile_service: ProfileService,
        *,
        shutdown: Callable[[], None],
        notify_failure: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(window)
        self._app = app
        self._window = window
        self._profiles = profile_service
        self._shutdown = shutdown
        self._notify_failure = notify_failure
        self._overlay = None
        self._handoff: ProfileHandoff | None = None
        self._transitioning = False
        self._committing = False
        self._was_enabled = True
        self._paused_ipc = None
        window.switch_profile_requested.connect(self.open_selector)

    def open_selector(self) -> None:
        if self._transitioning or not self._window.profile_transition_available:
            return
        if self._overlay is not None:
            self._overlay.raise_()
            return
        from solin.ui.profile_switch_overlay import ProfileSwitchOverlay

        current = self._profiles.active_profile
        overlay = ProfileSwitchOverlay(
            self._window,
            current.id if current is not None else "",
            profiles=self._profiles.profiles,
        )
        self._overlay = overlay
        # Reject queued events from a selector after it has been dismissed.
        overlay.cancelled.connect(lambda: self._dispatch(overlay, self._dismiss_selector))
        overlay.profile_selected.connect(
            lambda profile_id: self._dispatch(overlay, lambda: self.select_profile(profile_id))
        )
        overlay.create_profile_requested.connect(
            lambda: self._dispatch(overlay, self.create_profile)
        )
        overlay.logout_requested.connect(lambda: self._dispatch(overlay, self.logout))
        overlay.show()
        overlay.raise_()

    def _dispatch(self, overlay, action: Callable[[], object]) -> None:
        if self._overlay is overlay and not self._transitioning:
            action()

    def _dismiss_selector(self) -> None:
        overlay = self._overlay
        self._overlay = None
        if overlay is not None:
            overlay.hide()
            overlay.deleteLater()

    def select_profile(self, profile_id: str) -> None:
        profile = self._profiles.get_profile(profile_id)
        if profile is None:
            return
        current = self._profiles.active_profile
        if current is not None and current.id == profile_id:
            self._dismiss_selector()
            return
        self._begin_transition(["--profile", profile_id])

    def create_profile(self) -> None:
        self._begin_transition(
            ["--create-profile"],
            prepare=self._profiles.prepare_profile_creation,
        )

    def logout(self) -> None:
        self._begin_transition(["--select-profile"])

    def _begin_transition(
        self,
        arguments: list[str],
        *,
        prepare: Callable[[], None] | None = None,
    ) -> None:
        if self._transitioning or not self._window.profile_transition_available:
            return
        self._transitioning = True
        self._was_enabled = self._window.isEnabled()
        try:
            # confirm_close owns recording and unsaved-editor guards. It may run
            # a nested event loop; hold the transition guard before calling it.
            if not self._window.prepare_profile_transition():
                self._transitioning = False
                return
            self._dismiss_selector()
            self._window.setEnabled(False)
            if prepare is not None:
                prepare()
            handoff = ProfileHandoff(self)
            self._handoff = handoff
            handoff.ready.connect(self._finish_transition)
            handoff.failed.connect(self._recover_transition)
            name = handoff.start()
            program, args, directory = detached_launch_command(
                [*arguments, "--profile-handoff", name]
            )
            started, _pid = QProcess.startDetached(program, args, directory)
            if not started:
                self._recover_transition("Could not launch the replacement process.")
        except Exception:  # noqa: BLE001 - recoverable Qt process/startup boundary
            log.exception("Could not prepare the profile transition")
            self._recover_transition("Profile transition preparation failed.")

    def _recover_transition(self, message: str) -> None:
        if self._committing or not self._transitioning:
            return
        log.warning("Profile transition cancelled: %s", message)
        handoff = self._handoff
        self._handoff = None
        if handoff is not None:
            handoff.close()
            handoff.deleteLater()
        if self._paused_ipc is not None:
            if not self._paused_ipc.start():
                log.error("Could not resume the current process IPC endpoint.")
            self._paused_ipc = None
        self._window.cancel_profile_transition()
        self._window.setEnabled(self._was_enabled)
        self._transitioning = False
        if self._notify_failure is not None:
            try:
                self._notify_failure()
            except Exception:  # noqa: BLE001 - notification failure must not undo recovery
                log.exception("Could not display the profile transition failure")

    def _finish_transition(self) -> None:
        if self._committing or not self._transitioning or self._handoff is None:
            return
        self._committing = True
        # Closing the last window must not run app cleanup before this handoff
        # releases its peer. All shutdown here is synchronous and ordered.
        quit_on_close = self._app.quitOnLastWindowClosed()
        self._app.setQuitOnLastWindowClosed(False)
        try:
            ipc = getattr(self._app, "_solin_single_instance_server", None)
            if ipc is not None:
                self._paused_ipc = ipc
                ipc.pause()
            if not self._window.close_for_profile_transition():
                self._committing = False
                self._recover_transition("Window closure was rejected.")
                return
            if ipc is not None:
                ipc.reserve_until_exit()
            self._paused_ipc = None
            self._shutdown()
            if not self._handoff.release(
                self._window.take_pending_files(),
                ipc.accepted_request_ids if ipc else [],
            ):
                log.error("Replacement process disconnected during profile shutdown.")
            self._handoff.close()
            self._app.quit()
        except Exception:  # noqa: BLE001 - irreversible shutdown must fail closed
            log.exception("Profile shutdown failed; replacement startup was blocked")
            self._handoff.close()
            self._app.quit()
        finally:
            self._app.setQuitOnLastWindowClosed(quit_on_close)


def detached_launch_command(runtime_args: list[str]) -> tuple[str, list[str], str]:
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        program = QCoreApplication.applicationFilePath() or sys.executable
        working_directory = QCoreApplication.applicationDirPath() or str(Path.cwd())
        return program, runtime_args, working_directory

    project_root = Path(__file__).resolve().parents[3]
    launcher = project_root / "main.py"
    if launcher.is_file() and (project_root / "pyproject.toml").is_file():
        return sys.executable, [str(launcher), *runtime_args], str(project_root)
    return sys.executable, ["-m", "solin.bootstrap.application", *runtime_args], str(Path.cwd())


def wire_profile_switch(
    app,
    window_ref,
    profile_service: ProfileService,
    *,
    shutdown: Callable[[], None],
    notify_failure: Callable[[], None] | None = None,
) -> ProfileTransitionController:
    return ProfileTransitionController(
        app,
        window_ref[0],
        profile_service,
        shutdown=shutdown,
        notify_failure=notify_failure,
    )
