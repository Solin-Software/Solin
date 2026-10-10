"""
ZoomService — integration with Zoom Meetings via pywinauto (Windows only).

All pywinauto / UIA operations run in background threads to avoid blocking
the Qt event loop.  Communication back to the main thread is via Qt Signals.

Cross-platform safety
─────────────────────
  The module is safe to import on any OS.  ``ZoomService`` creates a no-op
  instance when ``sys.platform != "win32"`` or ``pywinauto`` is unavailable.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, Signal

from solin.core.integrations.automation.settings import ZoomParticipantSettings
from solin.core.integrations.automation.zoom.native import (
    initialize_com_for_current_thread,
    share_selection_dialog_open,
)

if TYPE_CHECKING:
    from .controls import ZoomSession

log = logging.getLogger(__name__)

# ── Conditional import ────────────────────────────────────────────────────────

import importlib.util

_HAS_ZOOM = False
if sys.platform == "win32":
    # Check whether the library exists without importing it on the main thread.
    _HAS_ZOOM = importlib.util.find_spec("pywinauto") is not None


# ── ZoomService ───────────────────────────────────────────────────────────────

class ZoomService(QObject):
    """
    High-level service that manages the connection to a running Zoom meeting,
    caches window/button handles, tracks participants, and automates screen
    sharing during media playback.

    All signals are emitted on the main thread (via QueuedConnection).
    """

    # ── Public signals ────────────────────────────────────────────────────
    connection_changed    = Signal(bool)         # True = connected to meeting
    participants_updated  = Signal(int, list)    # (people_count, name_list)
    sharing_state_changed = Signal(bool)         # True = currently sharing
    share_error           = Signal(str)          # non-modal error message

    # ── Internal bridge signals (worker thread → main thread) ─────────────
    _sig_connected = Signal(int, bool)
    _sig_participants = Signal(int, int, list)
    _sig_sharing = Signal(int, bool)
    _sig_share_error = Signal(int, str)
    _sig_worker_done = Signal(str, int)

    def __init__(
        self,
        settings: ZoomParticipantSettings,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._settings = settings

        self._connected: bool = False
        self._sharing:   bool = False
        self._participant_count: int = 0
        self._participant_names: list[str] = []

        self._stop_evt = threading.Event()
        self._operation_lock = threading.Lock()
        self._session_lock = threading.Lock()
        self._generation = 0
        self._session_generation = 0
        self._session: ZoomSession | None = None
        self._active = False
        self._inflight: set[str] = set()
        self._threads_lock = threading.Lock()
        self._threads: set[threading.Thread] = set()

        # ── Timers ────────────────────────────────────────────────────────
        # Connection poller: 5s until connected, then 20s keep-alive
        self._conn_timer = QTimer(self)
        self._conn_timer.timeout.connect(self._poll_connection)

        # Participant poller: 5s (only when enabled + connected)
        self._part_timer = QTimer(self)
        self._part_timer.timeout.connect(self._poll_participants)

        # Share-state poller: 3s (while sharing, checks if user stopped manually)
        self._share_timer = QTimer(self)
        self._share_timer.timeout.connect(self._poll_share_state)

        # Wire bridge signals → main-thread slots
        self._sig_connected.connect(self._on_connected_main)
        self._sig_participants.connect(self._on_participants_main)
        self._sig_sharing.connect(self._on_sharing_main)
        self._sig_share_error.connect(self._on_share_error_main)
        self._sig_worker_done.connect(self._on_worker_done)

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def is_available(self) -> bool:
        """True when pywinauto is available and we're on Windows."""
        return _HAS_ZOOM

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def is_sharing(self) -> bool:
        return self._sharing

    @property
    def participant_count(self) -> int:
        return self._participant_count

    @property
    def participant_names(self) -> list[str]:
        return list(self._participant_names)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self):
        """Start connection polling.  Safe to call on any platform."""
        if not _HAS_ZOOM:
            return
        if self._active:
            return
        with self._session_lock:
            self._generation += 1
            self._active = True
            self._stop_evt.clear()
        self._conn_timer.start(5000)
        self._poll_connection()  # immediate first attempt

    def stop(self, *, wait: bool = False, timeout: float = 8.0):
        """Stop all polling and disconnect."""
        with self._session_lock:
            self._active = False
            self._generation += 1
            self._stop_evt.set()
            session = self._session
            self._session = None
            self._session_generation = 0
        if session is not None:
            session.cancel()
        self._conn_timer.stop()
        self._part_timer.stop()
        self._share_timer.stop()
        if self._connected:
            self._connected = False
            self.connection_changed.emit(False)
        if self._sharing:
            self._sharing = False
            self.sharing_state_changed.emit(False)
        if wait:
            deadline = time.monotonic() + max(0.0, timeout)
            with self._threads_lock:
                threads = list(self._threads)
            for thread in threads:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if thread is not threading.current_thread():
                    thread.join(timeout=remaining)
            alive = [thread.name for thread in threads if thread.is_alive()]
            if alive:
                log.warning("Zoom workers still running during shutdown: %s", alive)

    def start_participant_polling(self):
        """Enable participant count updates (every 5s)."""
        if self._connected and not self._sharing:
            self._part_timer.start(5000)
            self._poll_participants()

    def stop_participant_polling(self):
        """Disable participant count updates."""
        self._part_timer.stop()

    # ── Actions (called from main thread, run in workers) ─────────────────

    def request_open_audio_for_all(self):
        """
        Leave computer audio + unmute all.
        Runs entirely in a background thread.
        """
        if not _HAS_ZOOM or not self._connected:
            return
        self._launch_worker("open-audio", self._worker_open_audio)

    def request_stop_share(self):
        """Click Zoom's Stop Share button. Works even if we don't know we're sharing."""
        if not _HAS_ZOOM or not self._connected:
            return
        self._launch_worker("stop-share", self._worker_stop_share)

    def _launch_worker(
        self,
        kind: str,
        worker: Callable[[int, ZoomSession], None],
    ) -> None:
        if not self._active or self._stop_evt.is_set() or kind in self._inflight:
            return
        generation = self._generation
        self._inflight.add(kind)

        def _run() -> None:
            try:
                with self._operation_lock:
                    session = self._session_for_generation(generation)
                    if session is not None:
                        worker(generation, session)
            finally:
                with self._threads_lock:
                    self._threads.discard(threading.current_thread())
                try:
                    self._sig_worker_done.emit(kind, generation)
                except RuntimeError:
                    return

        thread = threading.Thread(
            target=_run,
            daemon=True,
            name=f"zoom-{kind}",
        )
        with self._threads_lock:
            self._threads.add(thread)
        thread.start()

    def _session_for_generation(
        self,
        generation: int,
    ) -> ZoomSession | None:
        with self._session_lock:
            if (
                not self._active
                or self._stop_evt.is_set()
                or generation != self._generation
            ):
                return None
            if (
                self._session is None
                or self._session_generation != generation
            ):
                from .controls import ZoomSession

                self._session = ZoomSession()
                self._session_generation = generation
            return self._session

    def _is_current_generation(self, generation: int) -> bool:
        return (
            self._active
            and not self._stop_evt.is_set()
            and generation == self._generation
        )

    def _on_worker_done(self, kind: str, generation: int) -> None:
        self._inflight.discard(kind)
        if kind == "connection" and self._active and generation != self._generation:
            QTimer.singleShot(0, self._poll_connection)

    # ── Connection polling ────────────────────────────────────────────────

    def _poll_connection(self):
        if self._stop_evt.is_set() or not _HAS_ZOOM:
            return
        self._launch_worker("connection", self._worker_check_connection)

    def _worker_check_connection(
        self,
        generation: int,
        session: ZoomSession,
    ) -> None:
        from . import controls
        initialize_com_for_current_thread()
        try:
            windows, pids = controls._find_zoom_windows_fast(session)
            main, _ = controls._find_main_window(session, windows)
            connected = main is not None
        except Exception:  # noqa: BLE001 - Zoom UIA connection boundary
            connected = False
            windows = []
        self._sig_connected.emit(generation, connected)

        # Also detect sharing state (check for ZPFloatToolbarClass)
        if connected:
            try:
                is_sharing = any(
                    w["class_name"] == "ZPFloatToolbarClass" for w in windows
                )
                self._sig_sharing.emit(generation, is_sharing)
            except Exception:  # noqa: BLE001 - Zoom UIA state-probe boundary
                log.debug("Failed to detect Zoom sharing state", exc_info=True)

    def _on_connected_main(self, generation: int, connected: bool) -> None:
        if not self._is_current_generation(generation):
            return
        if connected == self._connected:
            return
        self._connected = connected
        self.connection_changed.emit(connected)
        if connected:
            # Slow down connection checks
            self._conn_timer.setInterval(20000)
            # Start participant polling if enabled
            show_parts = self._settings.show_participants()
            if show_parts:
                self.start_participant_polling()
        else:
            # Speed up reconnection attempts
            self._conn_timer.setInterval(5000)
            self._part_timer.stop()
            self._share_timer.stop()
            if self._sharing:
                self._sharing = False
                self.sharing_state_changed.emit(False)

    # ── Participant polling ───────────────────────────────────────────────

    def _poll_participants(self):
        if self._stop_evt.is_set() or not _HAS_ZOOM or not self._connected:
            return
        if self._sharing:
            return  # pause during sharing
        self._launch_worker("participants", self._worker_get_participants)

    def _worker_get_participants(
        self,
        generation: int,
        session: ZoomSession,
    ) -> None:
        """
        Get participant names from Zoom.
        Open the participant panel if closed and KEEP IT OPEN
        to avoid open-close flicker every 5 seconds.

        Optimized flow:
          1. _open_participants_panel() uses reliable detection (cache + structural)
             to avoid accidentally closing an already open panel.
          2. get_participant_names() detects the open panel and leaves it open.
          3. warm_toolbar_cache() with include_participants=True caches the toolbar,
             mute_all_btn, and more_btn in a single scan.
        """
        from . import controls
        initialize_com_for_current_thread()
        try:
            # Open the panel if necessary (reliable detection prevents accidental toggling).
            controls._open_participants_panel(session)
            time.sleep(0.1)

            # get_participant_names() detects the open panel and leaves it open.
            names = controls.get_participant_names(session)
            count = controls.count_people(names, exclude_host=True) if names else 0

            # Proactively cache the toolbar and participant panel buttons.
            try:
                controls.warm_toolbar_cache(
                    session,
                    {"btn_paticipants", "btn_muteAudio", "btn_audioMenu"},
                    reveal=True,
                    include_participants=True,
                )
            except Exception:  # noqa: BLE001 - Zoom UIA cache-warm boundary
                log.debug("Failed to warm Zoom toolbar cache during participant poll", exc_info=True)

            self._sig_participants.emit(generation, count, names)
        except Exception as exc:  # noqa: BLE001 - Zoom UIA polling boundary
            log.debug("Zoom participant poll failed: %s", exc)

    def _on_participants_main(self, generation: int, count: int, names: list) -> None:
        if not self._is_current_generation(generation):
            return
        self._participant_count = count
        self._participant_names = names
        self.participants_updated.emit(count, names)

    # ── Share state ─────────────────────────────────────────────────────────

    def _worker_stop_share(
        self,
        generation: int,
        session: ZoomSession,
    ) -> None:
        """
        Click Zoom's Stop Share button using the public API.
        Uses the fast Win32 FindWindowW check (~0ms) instead of full UIA scan.
        Cache-first strategy: if poller kept the cache warm, invoke is ~0.5ms.
        """
        from . import controls
        import time
        t_start = time.perf_counter()
        initialize_com_for_current_thread()
        try:
            t0 = time.perf_counter()
            is_sharing_fast = controls._find_float_toolbar_hwnd()
            log.info(
                "[ZStop] Floating-toolbar check took: %.1fms",
                (time.perf_counter() - t0) * 1000,
            )
            
            if not is_sharing_fast:
                self._sig_sharing.emit(generation, False)
                return

            # OPTIMISTIC: emit the stop signal NOW so the UI reacts immediately (as with start_share).
            self._sig_sharing.emit(generation, False)
            
            log.info("[ZStop] Calling stop_screen_share()...")
            t0 = time.perf_counter()
            controls.stop_screen_share(session)
            log.info(f"[ZStop] stop_screen_share() returned in {(time.perf_counter()-t0)*1000:.1f}ms")
            log.info(f"[ZStop] Total worker time: {(time.perf_counter()-t_start)*1000:.1f}ms")
            
        except RuntimeError as exc:
            log.warning("Zoom stop share failed: %s", exc)
            if not controls._find_float_toolbar_hwnd():
                return
            self._sig_share_error.emit(generation, str(exc))
            self._sig_sharing.emit(generation, False)
        except Exception as exc:  # noqa: BLE001 - Zoom UIA operation boundary
            log.warning("Zoom stop share failed: %s", exc)
            self._sig_share_error.emit(generation, str(exc))

    def _poll_share_state(self):
        """While sharing, check if user manually stopped or sharing ended."""
        if self._stop_evt.is_set() or not _HAS_ZOOM:
            return
        if not self._sharing:
            self._share_timer.stop()
            return
        self._launch_worker("share-state", self._worker_check_share)

    def _worker_check_share(
        self,
        generation: int,
        session: ZoomSession,
    ) -> None:
        """Check if sharing is still active. Uses a fast native probe + cache refresh."""
        from . import controls
        initialize_com_for_current_thread()
        try:
            # _is_sharing() uses a fast floating-toolbar probe and validates cache.
            still_sharing = controls._is_sharing(session)
            if not still_sharing:
                # The user may have just started sharing and still have the
                # share selection dialog (ZPShareEntranceClass) open.
                # Keep the poller armed while the user is choosing a screen.
                if not share_selection_dialog_open():
                    self._sig_sharing.emit(generation, False)
        except Exception:  # noqa: BLE001 - Zoom UIA state-probe boundary
            log.debug("Failed to poll Zoom sharing state", exc_info=True)

    def _on_sharing_main(self, generation: int, sharing: bool) -> None:
        if not self._is_current_generation(generation):
            return
        was_sharing = self._sharing
        self._sharing = sharing
        self.sharing_state_changed.emit(sharing)
        if sharing and not was_sharing:
            # Pause participant polling, start share-state polling
            self._part_timer.stop()
            self._share_timer.start(3000)
        elif not sharing and was_sharing:
            # Resume participant polling
            self._share_timer.stop()
            show_parts = self._settings.show_participants()
            if show_parts and self._connected:
                self._part_timer.start(5000)

    def _on_share_error_main(self, generation: int, message: str) -> None:
        if not self._is_current_generation(generation):
            return
        self.share_error.emit(message)

    # ── Open Audio for All ────────────────────────────────────────────────

    def _worker_open_audio(
        self,
        _generation: int,
        session: ZoomSession,
    ) -> None:
        """Leave computer audio + unmute all."""
        from . import controls
        initialize_com_for_current_thread()
        try:
            controls.leave_computer_audio(session)
            time.sleep(0.3)
        except Exception as e:  # noqa: BLE001 - Zoom UIA operation boundary
            log.warning("Zoom leave_computer_audio failed: %s", e)

        try:
            controls.unmute_all(session)
        except Exception as e:  # noqa: BLE001 - Zoom UIA operation boundary
            log.warning("Zoom unmute_all failed: %s", e)
