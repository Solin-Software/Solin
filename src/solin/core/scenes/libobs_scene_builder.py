"""Build a libobs scene graph from a ``SceneDocument`` record.

Translates the document's scenes and layers into ``obs_scene`` objects, applies
their geometry, and routes the active program onto a global output channel.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from threading import RLock
from urllib.parse import quote, urlsplit, urlunsplit
from typing import Any, Protocol

from solin.core.scenes.model import Crop, NormalizedRect
from solin.core.scenes.libobs_transitions import (
    FALLBACK_TRANSITION_KIND,
    TRANSITION_SOURCE_IDS,
    LibobsTransitionPool,
)

log = logging.getLogger(__name__)

# Neutral-gray placeholder for sources whose real pixels aren't wired yet.
_PLACEHOLDER_COLOR = 0xFF404040
# BusId values (see solin.core.scenes.model.BusId). VIRTUAL_CAMERA is the
# "Program" bus; MEDIA_WINDOWS mirrors it. The program scene is what composites.
_PROGRAM_BUS = "virtual_camera"
_MIRROR_BUS = "media_windows"
# The single canonical content source id (see model.CONTENT_SOURCE_ID). Layers
# referencing it are fed by the content ingress source, not a placeholder.
_CONTENT_SOURCE_ID = "solin.content.current"


class _SceneTransition(Protocol):
    def set_source(self, source: object) -> None: ...

    def set_size(self, width: int, height: int) -> None: ...

    def start(self, destination: object, duration_ms: int) -> bool: ...

    def clear(self) -> None: ...


def _parse_color(hex_color: str) -> int:
    """Parse ``#RRGGBB`` into libobs' color_source uint32 (0xAABBGGRR)."""
    text = hex_color.lstrip("#")
    if len(text) >= 6:
        try:
            red = int(text[0:2], 16)
            green = int(text[2:4], 16)
            blue = int(text[4:6], 16)
        except ValueError:
            return _PLACEHOLDER_COLOR
        return 0xFF000000 | (blue << 16) | (green << 8) | red
    return _PLACEHOLDER_COLOR


_DEFAULT_FIT_MODE = "contain"
# libobs centres bounded content when no edge flag is set; there is no CENTER member.
_CENTER_ALIGNMENT = 0


def _bounds_type(ob: Any, fit_mode: str) -> Any:
    """Map the document's fit mode onto the libobs bounds type that expresses it."""
    kinds = {
        "contain": ob.BoundsType.SCALE_INNER,
        "cover": ob.BoundsType.SCALE_OUTER,
        "stretch": ob.BoundsType.STRETCH,
    }
    return kinds.get(str(fit_mode or "").strip().lower(), ob.BoundsType.SCALE_INNER)


def _layer_fit_mode(layer: dict) -> str:
    return str(layer.get("fit_mode") or _DEFAULT_FIT_MODE)


class LibobsSceneGraph:
    """Owns the libobs scenes/sources built for one hydrate snapshot."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._scenes: dict[str, Any] = {}
        self._sources: list[Any] = []
        # Scene items bound to the content slot, tracked so the content source can
        # be swapped live (BGRA frame source <-> libobs-decoded media) without a
        # full re-hydrate. Entries are shared with _layer_items so source swaps
        # always use the latest geometry and update the live editing target.
        self._content_items: list[dict[str, Any]] = []
        self._content_source: Any = None
        # Year-text image sources built this hydrate, tracked so the app can force a
        # re-read of the rendered PNG (reload_yeartext) without a full re-hydrate.
        self._yeartext_sources: list[Any] = []
        # Item records keyed by (scene_id, layer_id), so a layer's geometry can be
        # updated live (obs_sceneitem transform) without rebuilding the graph.
        self._layer_items: dict[tuple[str, str], Any] = {}
        # source id -> {"username", "password"}; never persisted, see hydrate().
        self._source_credentials: dict[str, Any] = {}
        # libobs crops in source pixels; the document crops in source fractions.
        # Recompute only cropped items before rendering when asynchronous sources
        # acquire/change dimensions. The lock keeps callbacks off released items.
        self._crop_lock = RLock()
        self._cropped_items: dict[int, tuple[Any, Crop, tuple[int, int]]] = {}
        self._crop_render_callback: Any | None = None
        self._program_channel: int | None = None
        # Program transition: a transition source sits on the program channel and
        # holds the active scene; scene switches animate through take().
        self._transition: _SceneTransition | None = None
        self._transitions = LibobsTransitionPool(runtime, "solin-transition")
        self._transition_kind: str | None = None
        self._active_scene_id: str | None = None
        self._pending: dict[str, tuple[str, str, int, str]] = {}
        self._token_seq = 0

    @property
    def scene_ids(self) -> tuple[str, ...]:
        return tuple(self._scenes)

    def scene_source(self, scene_id: str) -> Any | None:
        """Borrowed source of a built scene (for off-screen preview readback)."""
        scene = self._scenes.get(scene_id)
        return scene.as_source() if scene is not None else None

    def hydrate(
        self,
        document: dict,
        active_scenes: dict,
        content_source: Any | None = None,
        source_credentials: dict | None = None,
        *,
        before_activate: Callable[[], None] | None = None,
    ) -> None:
        """Rebuild the scene graph from a document record and route the program.

        ``content_source`` (a libobs source fed by the content ingress) is placed
        for layers referencing the canonical content id; it is *referenced*, not
        owned, so the scene graph never releases it.

        ``source_credentials`` maps a source id to its ``{"username", "password"}``.
        It is passed alongside the document rather than inside it: the document
        record is persisted and cached, and a password must live in neither.

        ``before_activate`` runs after rebuilding, with the previous Program
        detached and before routing the new one. Exceptions abort activation;
        the caller owns synchronization with borrowers throughout the rebuild.
        """
        credentials = dict(source_credentials or {})
        self.clear()
        self._source_credentials = credentials
        if self._crop_render_callback is None:
            self._crop_render_callback = self._runtime.ob.add_main_render_callback(
                self.refresh_source_crops,
            )
        self._content_source = content_source
        ob = self._runtime.ob
        canvas = self._runtime.video
        sources_by_id = {
            src["id"]: src
            for src in (document.get("sources") or ())
            if isinstance(src, dict) and src.get("id")
        }
        scene_records = [
            record
            for record in (document.get("scenes") or ())
            if isinstance(record, dict) and record.get("id")
        ]
        # Pass 1: create every scene (empty) so a scene_reference layer can resolve
        # any target — including a forward reference — in pass 2.
        for scene_record in scene_records:
            scene_id = scene_record["id"]
            self._scenes[scene_id] = ob.Scene.create(f"solin-scene-{scene_id}")
        # Pass 2: populate each scene's layers.
        for scene_record in scene_records:
            scene_id = scene_record["id"]
            scene = self._scenes[scene_id]
            for layer in scene_record.get("layers") or ():
                if not layer.get("visible", True):
                    continue
                self._add_layer(ob, scene_id, scene, layer, canvas, sources_by_id, content_source)
        self._transitions.prepare_document(document)
        if before_activate is not None:
            before_activate()
        self._setup_program(active_scenes)

    def _add_layer(
        self,
        ob: Any,
        scene_id: str,
        scene: Any,
        layer: dict,
        canvas: Any,
        sources_by_id: dict,
        content_source: Any | None,
    ) -> None:
        source, owned = self._resolve_source(ob, layer, canvas, sources_by_id, content_source)
        is_content = self._is_content_layer(layer, sources_by_id)
        placeholder = None
        if source is None:
            source = self._create_color(ob, layer, canvas, _PLACEHOLDER_COLOR)
            owned = True
            if is_content:
                placeholder = source  # owned stand-in until content arrives
        if owned:
            self._sources.append(source)  # released on clear (runtime owns shared cams)
        item = scene.add(source)
        self._apply_item_geometry(item, layer, canvas, ob)
        record = {
            "scene": scene,
            "item": item,
            "layer": layer,
            "placeholder": placeholder,
        }
        layer_id = str(layer.get("id") or "")
        if layer_id:
            self._layer_items[(scene_id, layer_id)] = record
        if is_content:
            self._content_items.append(record)

    def apply_layer_geometry(self, scene_id: str, layer_id: str, layer: dict) -> bool:
        """Update one layer's geometry on the live scene item (no rebuild).

        Returns ``False`` when the layer is not built in the current graph (e.g.
        a stale request after a re-hydrate), so the caller can reject it."""
        record = self._layer_items.get((scene_id, layer_id))
        if record is None:
            return False
        self._apply_item_geometry(
            record["item"],
            layer,
            self._runtime.video,
            self._runtime.ob,
        )
        record["layer"] = layer
        return True

    def _apply_item_geometry(
        self,
        item: Any,
        layer: dict,
        canvas: Any,
        ob: Any,
    ) -> None:
        rect = NormalizedRect.from_record(layer.get("rect") or NormalizedRect().to_record())
        crop = Crop.from_record(layer.get("crop") or {})
        fit_mode = _layer_fit_mode(layer).strip().lower()
        x = rect.x * canvas.width
        y = rect.y * canvas.height
        width, height = (
            max(1.0, rect.width * canvas.width),
            max(1.0, rect.height * canvas.height),
        )
        # Centre the anchor so rotation and negative scales preserve the
        # document's rect instead of rotating around libobs' default top-left.
        transform = ob.TransformInfo(
            pos=(x + width / 2, y + height / 2),
            bounds=(width, height),
            bounds_type=int(_bounds_type(ob, fit_mode)),
            bounds_alignment=_CENTER_ALIGNMENT,
            alignment=_CENTER_ALIGNMENT,
            scale=(-1.0 if layer.get("mirror_x", False) else 1.0,
                   -1.0 if layer.get("mirror_y", False) else 1.0),
            rotation=float(layer.get("rotation_degrees", 0.0)),
            crop_to_bounds=fit_mode == "cover",
        )
        item.defer_update_begin()
        try:
            item.set_transform(transform)
            with self._crop_lock:
                dimensions = self._set_item_crop(item, crop)
                if crop != Crop():
                    self._cropped_items[id(item)] = (item, crop, dimensions)
                else:
                    self._cropped_items.pop(id(item), None)
        finally:
            item.defer_update_end()

    @staticmethod
    def _set_item_crop(item: Any, crop: Crop) -> tuple[int, int]:
        if crop == Crop():
            item.crop = (0, 0, 0, 0)
            return (0, 0)
        source = item.source
        width, height = int(source.width), int(source.height)
        if width <= 0 or height <= 0:
            return (width, height)
        # Leave at least one source pixel visible even at very low resolutions.
        left = min(round(crop.left * width), max(0, width - 1))
        top = min(round(crop.top * height), max(0, height - 1))
        right = min(round(crop.right * width), max(0, width - left - 1))
        bottom = min(round(crop.bottom * height), max(0, height - top - 1))
        item.crop = (left, top, right, bottom)
        return (width, height)

    def refresh_source_crops(self, *_canvas_size: int) -> bool:
        """Resolve source-relative crop after source ticks and before composition."""
        # The video thread owns libobs' graphics mutex. Never wait for an editor
        # update which may be waiting for that mutex; retry on the next frame.
        if not self._crop_lock.acquire(blocking=False):
            return False
        try:
            for key, (item, crop, dimensions) in self._cropped_items.items():
                source = item.source
                current = (int(source.width), int(source.height))
                if current != dimensions:
                    self._cropped_items[key] = (item, crop, self._set_item_crop(item, crop))
            return True
        except Exception:  # noqa: BLE001 - native video callback boundary
            log.exception("Could not refresh scene source crop")
            return False
        finally:
            self._crop_lock.release()

    @staticmethod
    def _is_content_layer(layer: dict, sources_by_id: dict) -> bool:
        definition = sources_by_id.get(layer.get("source_id"))
        kind = definition.get("type") if definition else None
        return kind == "solin_content" or layer.get("source_id") == _CONTENT_SOURCE_ID

    def _resolve_source(
        self,
        ob: Any,
        layer: dict,
        canvas: Any,
        sources_by_id: dict,
        content_source: Any | None,
    ) -> tuple[Any | None, bool]:
        """Return ``(source, owned)`` for a layer, or ``(None, False)`` to fall
        back to a placeholder. ``owned`` sources are released on clear; shared
        camera sources and the content source are referenced, not owned."""
        definition = sources_by_id.get(layer.get("source_id"))
        kind = definition.get("type") if definition else None
        config = (definition.get("configuration") if definition else None) or {}

        if kind == "solin_content" or layer.get("source_id") == _CONTENT_SOURCE_ID:
            return (content_source, False)  # referenced (the consumer owns it)
        if kind == "color":
            return (self._create_color(ob, layer, canvas, _parse_color(str(config.get("color", "")))), True)
        if kind == "local_camera":
            device = str(config.get("device_id", ""))
            name = str(definition.get("name", "")) if definition else ""
            # Pin the selected capture format (empty when "Automatic"); pinning MJPG
            # at a resolution restores full frame rate vs the plugin's heavy default.
            return (
                self._runtime.camera_source(
                    device, name,
                    pixel_format=str(config.get("pixel_format", "")),
                    width=int(config.get("width", 0) or 0),
                    height=int(config.get("height", 0) or 0),
                ),
                False,  # runtime-owned
            )
        if kind == "rtsp_camera":
            uri = str(config.get("uri", ""))
            if not uri:
                return (None, False)
            camera_id = str(layer.get("source_id", ""))
            uri = self._authenticated_uri(uri, camera_id)
            # Shared per camera, not per layer: an IP camera usually serves only one
            # or two concurrent streams, so a source per scene meant the first scene
            # connected and every other one was refused and rendered black.
            return (
                self._runtime.rtsp_source(
                    camera_id,
                    uri,
                    {
                        "is_local_file": False,
                        "input": uri,
                        "reconnect_delay_sec": 2,
                        # ffmpeg_source dumps its settings — the full URL included —
                        # at LOG_INFO on every update, and that stream reaches the
                        # log users attach to bug reports. An RTSP address routinely
                        # carries the camera password.
                        "log_changes": False,
                    },
                ),
                False,  # runtime-owned, like the shared local capture sources
            )
        if kind == "scene_reference":
            target = str(config.get("target_scene_id", ""))
            scene = self._scenes.get(target)
            # A nested scene: reference the target scene's source (owned by the
            # graph, so borrowed here — not released by the referencing item).
            return (scene.as_source(), False) if scene is not None else (None, False)
        if kind == "image":
            path = self._resolve_image_asset(str(config.get("asset_id", "")))
            if not path:
                return (None, False)  # unresolved → placeholder
            source = ob.Source.create(
                "image_source",
                f"solin-image-{layer.get('id', 'layer')}",
                {"file": path},
            )
            return (source, True)
        if kind == "yeartext":
            # The year text is rendered by the app (in its own styling) to a PNG at
            # SOLIN_YEARTEXT_IMAGE; the sidecar shows it as a plain image source. A
            # layer positions/sizes it. When the PNG is re-rendered the app calls
            # reload_yeartext() to re-read the file in place.
            path = self._resolve_yeartext_image()
            if not path:
                return (None, False)  # not rendered yet → placeholder
            source = ob.Source.create(
                "image_source",
                f"solin-yeartext-{layer.get('id', 'layer')}",
                {"file": path},
            )
            self._yeartext_sources.append(source)
            return (source, True)
        return (None, False)

    @staticmethod
    def _resolve_yeartext_image() -> str | None:
        """Absolute path to the app-rendered year-text PNG (``SOLIN_YEARTEXT_IMAGE``),
        or ``None`` when it has not been rendered yet."""
        import os

        path = os.environ.get("SOLIN_YEARTEXT_IMAGE")
        return path if path and os.path.isfile(path) else None

    @staticmethod
    def _resolve_image_asset(asset_id: str) -> str | None:
        """Resolve a scene image asset id to an absolute file under the profile's
        images directory (passed to the sidecar via ``SOLIN_SCENE_IMAGES_DIR``).

        The id maps to a file named exactly ``asset_id`` or ``asset_id.*``; obs'
        image_source auto-detects the format from the file contents.
        """
        if not asset_id:
            return None
        import os
        from pathlib import Path

        root = os.environ.get("SOLIN_SCENE_IMAGES_DIR")
        if not root:
            return None
        base = Path(root)
        exact = base / asset_id
        if exact.is_file():
            return str(exact)
        try:
            matches = sorted(base.glob(f"{asset_id}.*"))
        except OSError:
            return None
        return str(matches[0]) if matches else None

    def _authenticated_uri(self, uri: str, source_id: str) -> str:
        """Put the stream login back into the URI, only for the connection.

        ffmpeg_source takes a single ``input`` string and the RTSP demuxer exposes
        no user/password option, so credentials can only reach the camera embedded
        in the URI. Solin keeps them out of the stored address and re-attaches them
        here, at the moment the source is created.

        Percent-encode on the way in: libavformat decodes the userinfo before
        authenticating, so a password containing @ : / ? # would otherwise arrive
        mangled or split the URI.
        """
        credentials = self._source_credentials.get(source_id) if source_id else None
        if not isinstance(credentials, dict):
            return uri
        username = str(credentials.get("username", ""))
        password = str(credentials.get("password", ""))
        if not username and not password:
            return uri
        try:
            parsed = urlsplit(uri)
        except ValueError:
            return uri
        if parsed.username is not None or parsed.password is not None:
            return uri  # the address already carries a login; do not double it
        host = parsed.hostname or ""
        if not host:
            return uri
        if ":" in host:
            host = f"[{host}]"  # IPv6 literals keep their brackets
        if parsed.port:
            host = f"{host}:{parsed.port}"
        userinfo = quote(username, safe="")
        if password:
            userinfo = f"{userinfo}:{quote(password, safe='')}"
        return urlunsplit(
            (parsed.scheme, f"{userinfo}@{host}", parsed.path, parsed.query,
             parsed.fragment)
        )

    def set_content_source(self, new_source: Any | None) -> None:
        """Retarget the content-slot items to ``new_source`` without re-hydrating.

        Used to swap between the BGRA frame source and a libobs-decoded media
        source live. Each content item is re-created with the new source at its
        original z-order and geometry; an owned placeholder (used when there was
        no content) is released once replaced.
        """
        if new_source is self._content_source:
            return
        self._content_source = new_source
        ob = self._runtime.ob
        canvas = self._runtime.video
        for record in self._content_items:
            scene = record["scene"]
            old_item = record["item"]
            layer = record["layer"]
            old_placeholder = record["placeholder"]
            try:
                order = int(old_item.order_position)
            except Exception:  # noqa: BLE001 - libobs boundary
                order = None
            if new_source is not None:
                source = new_source
                new_placeholder = None
            else:  # reverting to "no content" — stand in with a placeholder
                source = self._create_color(ob, layer, canvas,
                                            _PLACEHOLDER_COLOR)
                new_placeholder = source
            new_item = scene.add(source)  # added on top; restore its z-order below
            self._apply_item_geometry(new_item, layer, canvas, ob)
            if order is not None:
                try:
                    new_item.order_position = order
                except Exception:  # noqa: BLE001 - libobs boundary
                    log.debug("content item order restore errored", exc_info=True)
            with self._crop_lock:
                self._cropped_items.pop(id(old_item), None)
            try:
                old_item.remove()
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("content item remove errored", exc_info=True)
            record["item"] = new_item
            record["placeholder"] = new_placeholder
            if old_placeholder is not None:
                try:
                    old_placeholder.release()
                except Exception:  # noqa: BLE001 - libobs boundary
                    log.debug("content placeholder release errored", exc_info=True)
                try:
                    self._sources.remove(old_placeholder)
                except ValueError:
                    pass
            if new_placeholder is not None:
                self._sources.append(new_placeholder)

    def _create_color(self, ob: Any, layer: dict, canvas: Any, color: int) -> Any:
        rect = layer.get("rect") or {}
        width = max(1, round(float(rect.get("width", 1.0)) * canvas.width))
        height = max(1, round(float(rect.get("height", 1.0)) * canvas.height))
        return ob.Source.create(
            "color_source_v3",
            f"solin-color-{layer.get('id', 'layer')}",
            {"color": color, "width": width, "height": height},
        )

    def _setup_program(self, active_scenes: dict) -> None:
        program_scene_id = active_scenes.get(_PROGRAM_BUS) or active_scenes.get(_MIRROR_BUS)
        scene = self._scenes.get(program_scene_id) if program_scene_id else None
        if scene is None:
            return
        if self._program_channel is None:
            self._program_channel = self._runtime.acquire_channel()
        transition = self._transitions.prepared(FALLBACK_TRANSITION_KIND)
        self._transition = transition
        self._transition_kind = FALLBACK_TRANSITION_KIND
        transition.set_source(scene.as_source())
        self._active_scene_id = program_scene_id
        self._runtime.set_channel_source(self._program_channel, self._transition)

    def prepare(
        self,
        scene_id: str,
        model_kind: str,
        duration_ms: int,
        bus_id: str = _PROGRAM_BUS,
    ) -> dict | None:
        """Stage a switch to ``scene_id``. Returns a token + resolved transition,
        or None if the scene is unknown.

        The token remembers which output it was prepared for, so a take can only
        move the output it was staged against — a duplicated or reordered take
        can never swap the program using a token meant for the projection."""
        if scene_id not in self._scenes:
            return None
        fallback_applied = model_kind not in TRANSITION_SOURCE_IDS
        effective_kind = FALLBACK_TRANSITION_KIND if fallback_applied else model_kind
        if bus_id == _PROGRAM_BUS:
            self._transitions.prepare(effective_kind)
        self._token_seq += 1
        token = f"prep-{self._token_seq}"
        self._pending[token] = (scene_id, effective_kind, int(duration_ms), bus_id)
        return {
            "token": token,
            "kind": effective_kind,
            "fallback_applied": fallback_applied,
            "fallback_reason": "unsupported transition kind" if fallback_applied else "",
        }

    def pending_route(self, token: str) -> str | None:
        """Which output ``token`` was prepared for, or None if unknown."""
        pending = self._pending.get(token)
        return pending[3] if pending is not None else None

    def take_projection(self, token: str, route: Any) -> bool:
        """Execute a prepared switch on the projection output's own transition."""
        pending = self._pending.pop(token, None)
        if pending is None or route is None:
            return False
        scene_id, model_kind, duration_ms, _bus = pending
        scene = self._scenes.get(scene_id)
        if scene is None:
            return False
        return bool(route.start(scene_id, scene.as_source(), model_kind, duration_ms))

    def take(self, token: str) -> bool:
        """Execute a prepared switch, animating the program transition."""
        pending = self._pending.pop(token, None)
        if pending is None:
            return False
        scene_id, model_kind, duration_ms, _bus = pending
        scene = self._scenes.get(scene_id)
        if scene is None or self._transition is None:
            return False
        if model_kind != self._transition_kind:
            self._swap_transition(model_kind)
        self._transition.start(scene.as_source(), duration_ms)
        self._active_scene_id = scene_id
        return True

    def discard(self, token: str) -> bool:
        """Consume a prepared switch without executing it on the program channel.

        The editor channel never drives an output: taking one of its scenes
        re-points the off-screen preview egress instead of animating a
        transition, so its prepared token is dropped here rather than through
        :meth:`take`. Returns ``False`` for an unknown token, matching
        :meth:`take`."""
        return self._pending.pop(token, None) is not None

    def _swap_transition(self, model_kind: str) -> None:
        # Carry the live scene into the prepared transition before routing it.
        new_transition = self._transitions.prepared(model_kind)
        active = self._scenes.get(self._active_scene_id) if self._active_scene_id else None
        if active is not None:
            new_transition.set_source(active.as_source())
        old, self._transition = self._transition, new_transition
        self._transition_kind = model_kind
        if self._program_channel is not None:
            self._runtime.set_channel_source(self._program_channel, new_transition)
        if old is not None:
            try:
                old.clear()
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("old transition clear errored", exc_info=True)

    def cancel_all(self) -> None:
        self._pending.clear()

    def reload_yeartext(self) -> bool:
        """Force the year-text image sources to re-read their PNG in place.

        Called after the app re-renders the year-text image so the change shows
        without a full re-hydrate. Returns True if any source was refreshed."""
        path = self._resolve_yeartext_image()
        refreshed = False
        for source in self._yeartext_sources:
            try:
                source.update({"file": path} if path else {"file": ""})
                refreshed = True
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("yeartext source reload errored", exc_info=True)
        return refreshed

    def clear(self) -> None:
        """Detach Program before releasing its graph; keep the reserved channel.

        A failed detachment propagates without releasing or mutating the graph.
        Its sources may still be rendered by the active output channel.
        """
        if self._program_channel is not None:
            self._runtime.set_channel_source(self._program_channel, None)
        self._transitions.reset_sources()
        with self._crop_lock:
            self._cropped_items.clear()
        self._pending.clear()
        self._content_items = []
        self._content_source = None
        self._yeartext_sources = []
        self._layer_items = {}
        self._transition = None
        self._transition_kind = None
        self._active_scene_id = None
        for source in self._sources:
            try:
                source.release()
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("placeholder source release errored", exc_info=True)
        for scene in self._scenes.values():
            try:
                scene.release()
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("scene release errored", exc_info=True)
        self._sources.clear()
        self._scenes.clear()

    def shutdown(self) -> None:
        """Release everything, including the reserved output channel."""
        if self._crop_render_callback is not None:
            self._runtime.ob.remove_main_render_callback(self._crop_render_callback)
            self._crop_render_callback = None
        self.clear()
        self._transitions.shutdown()
        if self._program_channel is not None:
            try:
                self._runtime.release_channel(self._program_channel)
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("release_channel errored", exc_info=True)
            self._program_channel = None
