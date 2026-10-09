"""Route media playback through the libobs scene-engine sidecar (Fork A).

When the libobs engine is active, a **local** media file is decoded by the
sidecar's ``ffmpeg_source`` instead of Qt ``QMediaPlayer``: the frames are
composited straight into the content slot and never shipped as BGRA. This adapts
the small transport vocabulary the :class:`~solin.core.media.playback.MediaController`
needs onto the scene-engine control protocol (``open_media`` / ``control_media`` /
``set_media_properties``), turning percents and actions into engine requests.

Requests are fire-and-forget: playback state comes back asynchronously as
``media_playback_state`` events (see ``MediaController.on_engine_media_state``),
so a failed transport call is logged, not surfaced synchronously.
"""

from __future__ import annotations

import logging
from concurrent.futures import Future
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from solin.core.scenes.media_control import MediaControlAction

log = logging.getLogger(__name__)

_DEFAULT_DEADLINE_MS = 4000


class SceneEngineMediaRoute:
    """Adapts MediaController transport calls onto the scene-engine media protocol."""

    def __init__(
        self,
        engine: Any,
        *,
        deadline_ms: int = _DEFAULT_DEADLINE_MS,
        slot: int = 0,
        presentation_id: Callable[[], int | None] | None = None,
    ) -> None:
        self._engine = engine
        self._deadline_ms = deadline_ms
        self._slot = int(slot)
        self._presentation_id = presentation_id

    def is_ready(self) -> bool:
        """True when the sidecar is READY to accept media commands."""
        from solin.core.scenes.engine import SceneEngineStatus

        try:
            return self._engine.health.status == SceneEngineStatus.READY
        except Exception:  # noqa: BLE001 - health probe is best-effort
            return False

    @staticmethod
    def _request_id(tag: str) -> str:
        return f"media-{tag}-{uuid4().hex}"

    def _watch(self, future: Future, tag: str) -> None:
        def _log(done: Future) -> None:
            try:
                done.result()
            except Exception:  # noqa: BLE001 - transport is best-effort
                log.debug("engine media %s request failed", tag, exc_info=True)

        try:
            future.add_done_callback(_log)
        except Exception:  # noqa: BLE001 - defensive; a plain value is fine too
            log.debug("could not attach media engine done-callback", exc_info=True)

    def open(
        self,
        path: str,
        *,
        is_local_file: bool,
        autoplay: bool,
        volume_percent: int,
        speed_percent: int,
        trim_start_ms: int,
        trim_end_ms: int,
    ) -> None:
        self._watch(
            self._engine.open_media(
                path,
                is_local_file=is_local_file,
                autoplay=autoplay,
                volume_percent=volume_percent,
                speed_percent=speed_percent,
                trim_start_ms=trim_start_ms,
                trim_end_ms=trim_end_ms,
                slot=self._slot,
                content_media_epoch=(self._presentation_id() if self._presentation_id is not None else None),
                request_id=self._request_id("open"),
                deadline_ms=self._deadline_ms,
            ),
            "open",
        )

    def _control(self, action: MediaControlAction, *, position_ms: int = 0) -> None:
        self._watch(
            self._engine.control_media(
                action,
                position_ms=position_ms,
                slot=self._slot,
                request_id=self._request_id(action.value),
                deadline_ms=self._deadline_ms,
            ),
            action.value,
        )

    def play(self) -> None:
        self._control(MediaControlAction.PLAY)

    def pause(self) -> None:
        self._control(MediaControlAction.PAUSE)

    def stop(self) -> None:
        self._control(MediaControlAction.STOP)

    def restart(self) -> None:
        self._control(MediaControlAction.RESTART)

    def seek(self, position_ms: int) -> None:
        self._control(MediaControlAction.SEEK, position_ms=max(0, int(position_ms)))

    def close(self) -> None:
        self._control(MediaControlAction.CLOSE)

    def set_properties(self, *, volume_percent: int, speed_percent: int) -> None:
        self._watch(
            self._engine.set_media_properties(
                volume_percent=volume_percent,
                speed_percent=speed_percent,
                slot=self._slot,
                request_id=self._request_id("props"),
                deadline_ms=self._deadline_ms,
            ),
            "props",
        )
