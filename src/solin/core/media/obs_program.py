"""libobs projection program — the composited output the projection Display shows.

The second monitor is composed **entirely inside libobs**. Channel 0 holds a
fade ``Transition``; each kind of projected content (idle yeartext, media, still
image, sermon slide, black) is a libobs ``Scene`` scaled to fill the canvas.
Switching content calls ``transition.start(scene)`` for a smooth cross-dissolve
— exactly the pattern the ``pylibobs_studio`` example demonstrates
(``obs_set_output_source(0, transition)`` + ``obs_transition_start``).

Qt-rendered content (yeartext, sermon) is captured to a ``QImage`` by the caller
and turned into an ``image_source`` scene here, reusing Solin's existing
rendering rather than re-implementing it as native libobs text sources.

Process-wide singleton (there is one OBS canvas / channel 0); every projection
surface renders it.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
from pathlib import Path

from PySide6.QtGui import QImage

from .obs_runtime import ObsRuntime, obs_runtime

log = logging.getLogger(__name__)


def proj_diag(msg: str) -> None:
    """Opt-in projection tracer (``SOLIN_PROJ_DIAG=1``) → stderr, flushed.

    A guaranteed-visible trace of the obs projection path (crossfades, content
    switches, clears) for diagnosing "content won't clear / won't switch" issues
    on the real machine, independent of the file-logging configuration."""
    if os.environ.get("SOLIN_PROJ_DIAG"):
        print(f"[proj-diag] {msg}", file=sys.stderr, flush=True)

_DEFAULT_CROSSFADE_MS = 450
# Keys whose scene holds a live/decoding input (a window/screen capture, a camera
# device, a looping idle video). Unlike the cheap idle-text/image/black scenes,
# these must be released as soon as we crossfade away — otherwise the source keeps
# capturing (camera: keeps /dev/video open; idle video: keeps decoding frames) in
# the background. Retired centrally in _crossfade_to. (Whether the *source* is also
# released depends on the entry's owned flag: the camera's v4l2 source and the idle
# ffmpeg_source are owned → released; the browser's reused frame source is not.)
_CAPTURE_KEYS = frozenset({"browser", "camera", "idle_video", "ndi"})
# Grace added to the crossfade duration before an outgoing media source is
# stopped + released, so libobs has fully finished the fade (and dropped its own
# reference to the outgoing scene) before we tear the source down.
_DISPOSAL_GRACE_MS = 120
_BLACK_KEY = "__black__"


def _default_defer(ms: int, fn) -> None:
    """Run ``fn`` after ``ms`` on the GUI thread (the program is always driven
    from it). Injectable so unit tests can dispose synchronously."""
    from PySide6.QtCore import QTimer

    QTimer.singleShot(int(ms), fn)


class _SceneEntry:
    __slots__ = ("scene", "owned_sources", "key", "item", "image_size")

    def __init__(
        self, scene, owned_sources, key: str, *, item=None, image_size=None
    ) -> None:
        self.scene = scene
        self.owned_sources = owned_sources  # sources the program created (to release)
        self.key = key
        # For zoomable content (stills, sermon) the scene item is kept, along
        # with the source image's native pixel size, so a later zoom/pan can be
        # applied as a raw scale+pos transform (see set_image_transform).
        self.item = item
        self.image_size = image_size


class ProjectionProgram:
    """Owns channel 0's fade transition and the content scenes it crossfades."""

    def __init__(
        self,
        runtime: ObsRuntime,
        *,
        crossfade_ms: int = _DEFAULT_CROSSFADE_MS,
        defer=None,
    ) -> None:
        self._runtime = runtime
        self._crossfade_ms = crossfade_ms
        self._defer = defer if defer is not None else _default_defer
        self._lock = threading.RLock()
        self._transition = None
        self._entries: dict[str, _SceneEntry] = {}
        self._current_key: str | None = None
        # The exact entry the transition is currently showing — distinct from
        # _current_key because two different media scenes share the "media" key,
        # so key-equality alone cannot tell "already showing this" from a swap.
        self._current_entry: _SceneEntry | None = None
        # The engine's ffmpeg source currently shown as media.
        self._media_source = None
        # Outgoing content (retired scenes + detached media) is not released
        # immediately — that would run obs_source_remove on a source the fade is
        # still dissolving *from*, cutting it to black. Instead it waits here,
        # kept alive, until the crossfade completes: _pending_disposal collects
        # this crossfade's casualties; _schedule_disposal moves each batch into
        # _in_flight and defers the actual release; shutdown drains both.
        self._pending_disposal: list[tuple[object, _SceneEntry | None]] = []
        self._in_flight: list[list[tuple[object, _SceneEntry | None]]] = []
        self._image_dir: Path | None = None
        self._seq = 0

    # ── lifecycle ─────────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        return self._transition is not None

    @property
    def current_key(self) -> str | None:
        """Key of the content the transition is currently showing (or None)."""
        return self._current_key

    @property
    def is_blank(self) -> bool:
        """True when the program shows nothing real yet — not created, or still on
        its initial black scene. A projection surface attaching *later* (e.g. the
        window preview opened while media plays) checks this so it renders the
        shared channel-0 output as-is instead of resetting it to the idle screen."""
        return self._current_key in (None, _BLACK_KEY)

    def ensure(self) -> None:
        """Create the fade transition on channel 0 (idempotent)."""
        with self._lock:
            if self._transition is not None:
                return
            self._runtime.ensure_started()
            ob = self._runtime.ob
            canvas = self._runtime.video
            transition = ob.Transition.create("fade_transition", "solin-program")
            transition.set_size(canvas.width, canvas.height)
            black = self._black_entry()
            transition.set_source(black.scene.as_source())
            self._current_key = _BLACK_KEY
            self._current_entry = black
            self._transition = transition
            self._runtime.set_channel_source(0, transition)
            log.info("libobs projection program ready (fade transition on channel 0)")

    def shutdown(self) -> None:
        with self._lock:
            if self._transition is None:
                return
            try:
                self._runtime.set_channel_source(0, None)
                self._transition.clear()
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("Error clearing projection transition", exc_info=True)
            self._transition = None
            self._current_key = None
            self._current_entry = None
            # Drain every deferred fade-out — both batches not yet scheduled
            # (_pending_disposal) and batches scheduled but whose timer has not
            # fired (_in_flight) — so nothing is disposed against a freed OBS
            # context after shutdown, and nothing leaks. Emptying _in_flight also
            # makes any late timer that still fires a no-op (see _dispose_batch).
            batches = [self._pending_disposal, *self._in_flight]
            self._pending_disposal = []
            self._in_flight = []
            for batch in batches:
                self._dispose_entries(batch)
            self._dispose_source(self._media_source)
            self._media_source = None
            for entry in list(self._entries.values()):
                self._release_entry(entry)
            self._entries.clear()

    # ── content ───────────────────────────────────────────────────────────

    def show_media(self, source) -> None:
        """Crossfade to a scene wrapping ``source`` (the engine's ffmpeg source).

        Any previous media source is retired for a fade-out (kept playing until
        this crossfade completes, then stopped + released) rather than cut.
        """
        with self._lock:
            self._retire_media_source()
            entry = self._media_entry(source)
            self._media_source = source
            self._crossfade_to("media", entry)

    def detach_media(self) -> None:
        """Fade the current media out instead of hard-cutting it to black.

        Called by the engine on stop/replace **in place of releasing the source**
        — pylibobs ``Source.release`` runs ``obs_source_remove`` first, which
        would yank the source out of the live scene mid-fade. Here the source is
        moved to pending disposal but kept playing; the next crossfade (``clear``
        → idle, or the next media) dissolves away from live video and then
        ``_dispose_batch`` stops + releases it.
        """
        with self._lock:
            self._retire_media_source()

    def _retire_media_source(self) -> None:
        source = self._media_source
        entry = self._entries.pop("media", None)
        self._media_source = None
        if source is None and entry is None:
            return
        if source is not None:
            # Silence the outgoing audio at once: the video still dissolves over
            # the crossfade, but the sound must not linger after stop/switch.
            # Set both muted and volume=0 — audio *monitoring* (MONITOR_ONLY, how
            # Solin routes playback to the speakers) may not honour the mute flag,
            # but a zero base volume is applied before the monitor split.
            try:
                source.muted = True
                source.volume = 0.0
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("Could not silence outgoing media source", exc_info=True)
        self._pending_disposal.append((source, entry))

    def show_image(self, key: str, image: QImage, *, zoomable: bool = False) -> None:
        """Crossfade to an ``image_source`` scene rendered from ``image``.

        ``key`` names the content slot (e.g. ``"idle"``, ``"image"``, ``"sermon"``)
        so re-showing the same slot reuses/retires it cleanly. ``zoomable`` keeps
        the scene item on raw scale+pos (instead of a letterbox bounds fit) so a
        later :meth:`set_image_transform` can zoom/pan it.
        """
        if image is None or image.isNull():
            return
        entry = self._image_entry(key, image, zoomable=zoomable)
        if entry is not None:
            self._crossfade_to(key, entry)

    def set_image_transform(self, zoom: float, norm_x: float, norm_y: float) -> None:
        """Zoom/pan the currently shown image scene in place (no crossfade).

        No-op unless the current scene is a zoomable image/sermon item — media,
        black and the letterboxed idle/timer scenes carry no ``image_size``.
        """
        with self._lock:
            entry = self._current_entry
            if entry is None or entry.item is None or entry.image_size is None:
                return
            iw, ih = entry.image_size
            self._apply_item_transform(entry.item, iw, ih, zoom, norm_x, norm_y)

    def show_source(self, key: str, source, *, owned: bool = True) -> None:
        """Crossfade to a scene wrapping an arbitrary libobs ``source`` (e.g. a
        capture or a frame-injection source), scaled to fill the canvas.

        ``owned`` — release the source when the scene is disposed. Pass False for
        a source the caller keeps and reuses (the browser frame source, which the
        driver pushes frames into across re-shows).
        """
        ob = self._runtime.ob
        canvas = self._runtime.video
        scene = ob.Scene.create(f"solin-scene-{key}-{self._next_seq()}")
        item = scene.add(source)
        self._fill_canvas(item, ob, canvas)
        entry = self._install(key, _SceneEntry(scene, [source] if owned else [], key))
        self._crossfade_to(key, entry)

    def show_black(self) -> None:
        self._crossfade_to(_BLACK_KEY, self._black_entry())

    def update_image(self, key: str, image: QImage) -> bool:
        """Swap the texture of an existing image scene **in place** (no fade).

        Used for content that changes rapidly while already shown — the media
        countdown ticking every second — where a crossfade per update would
        flicker. Returns False if ``key`` is not a live image scene.
        """
        if image is None or image.isNull():
            return False
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or not entry.owned_sources:
                return False
            source = entry.owned_sources[0]
            seq = self._next_seq()
            path = self._save(key, seq, image)
            if path is None:
                return False
            try:
                source.update({"file": str(path)})
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("Could not update image texture for %s", key, exc_info=True)
                return False
            return True

    # ── scene builders ────────────────────────────────────────────────────

    def _black_entry(self) -> _SceneEntry:
        existing = self._entries.get(_BLACK_KEY)
        if existing is not None:
            return existing
        ob = self._runtime.ob
        canvas = self._runtime.video
        color = ob.Source.create(
            "color_source_v3",
            "solin-black",
            {"color": 0xFF000000, "width": canvas.width, "height": canvas.height},
        )
        scene = ob.Scene.create("solin-scene-black")
        scene.add(color)
        entry = _SceneEntry(scene, [color], _BLACK_KEY)
        self._entries[_BLACK_KEY] = entry
        return entry

    def _media_entry(self, source) -> _SceneEntry:
        ob = self._runtime.ob
        canvas = self._runtime.video
        scene = ob.Scene.create(f"solin-scene-media-{self._next_seq()}")
        item = scene.add(source)
        self._fill_canvas(item, ob, canvas)
        # The media scene does NOT own the source (the engine does); [] owned.
        return self._install("media", _SceneEntry(scene, [], "media"))

    def _image_entry(self, key: str, image: QImage, *, zoomable: bool = False) -> _SceneEntry | None:
        ob = self._runtime.ob
        canvas = self._runtime.video
        seq = self._next_seq()
        path = self._save(key, seq, image)
        if path is None:
            return None
        source = ob.Source.create(
            "image_source", f"solin-img-{key}-{seq}", {"file": str(path)}
        )
        scene = ob.Scene.create(f"solin-scene-{key}-{seq}")
        item = scene.add(source)
        if zoomable:
            # Raw scale+pos at the identity fit (pixel-identical to the letterbox
            # bounds), so a zoom/pan can be layered on top later.
            iw, ih = image.width(), image.height()
            self._apply_item_transform(item, iw, ih, 1.0, 0.0, 0.0)
            return self._install(
                key, _SceneEntry(scene, [source], key, item=item, image_size=(iw, ih))
            )
        self._fill_canvas(item, ob, canvas)
        return self._install(key, _SceneEntry(scene, [source], key))

    @staticmethod
    def _fill_canvas(item, ob, canvas) -> None:
        item.bounds_type = int(ob.BoundsType.SCALE_INNER)
        item.bounds = (float(canvas.width), float(canvas.height))
        item.bounds_alignment = int(ob.Alignment.CENTER)

    def _apply_item_transform(self, item, iw: int, ih: int, zoom: float, nx: float, ny: float) -> None:
        """Map (zoom, norm_x, norm_y) onto a scene item as raw scale+position.

        Mirrors the Qt VideoDisplayWidget exactly: the image is fit to the canvas,
        scaled about its centre by ``zoom``, and panned by a fraction of the canvas
        size. Identity (zoom=1, norm=0) reproduces the letterbox fit pixel-for-pixel.
        Anything pushed past the canvas edge is clipped by the canvas-sized output.
        """
        ob = self._runtime.ob
        canvas = self._runtime.video
        cw, ch = float(canvas.width), float(canvas.height)
        iw_f = float(max(1, iw))
        ih_f = float(max(1, ih))
        fit = min(cw / iw_f, ch / ih_f)
        scale = fit * max(0.1, min(10.0, float(zoom)))
        item.defer_update_begin()
        try:
            item.bounds_type = int(ob.BoundsType.NONE)
            item.alignment = int(ob.Alignment.CENTER)
            item.crop = (0, 0, 0, 0)
            item.scale = (scale, scale)
            item.pos = (cw / 2.0 + float(nx) * cw, ch / 2.0 + float(ny) * ch)
        finally:
            item.defer_update_end()

    # ── internals ─────────────────────────────────────────────────────────

    def _crossfade_to(self, key: str, entry: _SceneEntry) -> None:
        with self._lock:
            if self._transition is None:
                proj_diag(f"crossfade_to {key}: NO TRANSITION (program not ensured)")
                return
            # Compare the entry itself, not the key: media→media reuses the
            # "media" key with a *different* scene, so a key check would wrongly
            # skip the swap.
            if entry is self._current_entry:
                proj_diag(f"crossfade_to {key}: SKIP (already current entry)")
                return
            previous = self._current_entry
            try:
                self._transition.start(
                    entry.scene.as_source(),
                    duration_ms=self._crossfade_ms,
                    mode=self._runtime.ob.TransitionMode.AUTO,
                )
            except Exception as exc:  # noqa: BLE001 - libobs boundary
                proj_diag(f"crossfade_to {key}: transition.start RAISED {exc!r}")
                log.debug("Crossfade to %s failed", key, exc_info=True)
                return
            proj_diag(f"crossfade {self._current_key} -> {key} OK")
            self._current_key = key
            self._current_entry = entry
            # Leaving a live capture scene → retire it so its source stops
            # capturing (only when it is still the registered content for its key,
            # i.e. we are not just swapping one capture for a newer one, which
            # _install already retired).
            if (
                previous is not None
                and previous.key in _CAPTURE_KEYS
                and previous is not entry
                and self._entries.get(previous.key) is previous
            ):
                self._entries.pop(previous.key, None)
                self._pending_disposal.append((None, previous))
            self._schedule_disposal()

    def _schedule_disposal(self) -> None:
        """Release the content this crossfade faded away from, once the fade
        completes (retired scenes + any detached media source)."""
        if not self._pending_disposal:
            return
        batch = self._pending_disposal
        self._pending_disposal = []
        self._in_flight.append(batch)
        self._defer(
            self._crossfade_ms + _DISPOSAL_GRACE_MS,
            lambda: self._dispose_batch(batch),
        )

    def _dispose_batch(self, batch: list[tuple[object, _SceneEntry | None]]) -> None:
        with self._lock:
            if not any(b is batch for b in self._in_flight):
                return  # already drained by shutdown
            self._in_flight = [b for b in self._in_flight if b is not batch]
            self._dispose_entries(batch)

    def _dispose_entries(self, batch: list[tuple[object, _SceneEntry | None]]) -> None:
        for source, entry in batch:
            if entry is not None:
                self._release_entry(entry)
            self._dispose_source(source)

    def _dispose_source(self, source) -> None:
        if source is None:
            return
        try:
            source.media_stop()
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("media_stop errored during media disposal", exc_info=True)
        try:
            source.release()
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("source release errored during media disposal", exc_info=True)

    def _install(self, key: str, entry: _SceneEntry) -> _SceneEntry:
        """Register entry under key, retiring any previous scene for that key.

        The previous scene is still the source the transition is about to
        dissolve *from*, and it owns its image source (obs_source_remove on
        release would blank it mid-fade), so it is queued for disposal *after*
        the crossfade rather than released now (see _schedule_disposal).
        """
        with self._lock:
            previous = self._entries.get(key)
            self._entries[key] = entry
            if previous is not None and previous is not entry:
                self._pending_disposal.append((None, previous))
            return entry

    def _release_entry(self, entry: _SceneEntry) -> None:
        try:
            entry.scene.release()
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("Error releasing program scene %s", entry.key, exc_info=True)
        for source in entry.owned_sources:
            try:
                source.release()
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("Error releasing program source for %s", entry.key, exc_info=True)

    def _save(self, key: str, seq: int, image: QImage) -> Path | None:
        if self._image_dir is None:
            self._image_dir = Path(tempfile.mkdtemp(prefix="solin-obs-prog-"))
        path = self._image_dir / f"{key}-{seq}.png"
        if not image.save(str(path)):
            log.debug("Could not save program image for %s", key)
            return None
        return path

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq


_program: ProjectionProgram | None = None
_program_lock = threading.Lock()


def projection_program() -> ProjectionProgram:
    """Return the process-wide :class:`ProjectionProgram` singleton."""
    global _program
    if _program is None:
        with _program_lock:
            if _program is None:
                _program = ProjectionProgram(obs_runtime())
    return _program


__all__ = ["ProjectionProgram", "projection_program"]
