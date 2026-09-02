"""Build a libobs scene graph from a ``SceneDocument`` record.

This is the first compositing slice of the libobs scene engine. It translates the
document's scenes/layers into real ``obs_scene`` objects and routes the active
program scene onto a global output channel so it composites into the main
texture.

For now every layer is rendered as a **color placeholder** positioned by its
normalized rect — real sources (Solin content over the data plane, cameras,
images) arrive in later stages. That keeps this slice verifiable end-to-end (a
composed frame is non-black and laid out per the document) without depending on
the not-yet-built content transport.
"""

from __future__ import annotations

import logging
from typing import Any

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

# Model TransitionKind (see model.TransitionKind) → obs transition source id.
_TRANSITION_IDS = {
    "cut": "cut_transition",
    "dissolve": "fade_transition",
    "fade_to_black": "fade_to_color_transition",
}
_FALLBACK_KIND = "cut"


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


class LibobsSceneGraph:
    """Owns the libobs scenes/sources built for one hydrate snapshot."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._scenes: dict[str, Any] = {}
        self._sources: list[Any] = []
        # Scene items bound to the content slot, tracked so the content source can
        # be swapped live (BGRA frame source <-> libobs-decoded media) without a
        # full re-hydrate. Each entry: {scene, item, rect, placeholder}.
        self._content_items: list[dict[str, Any]] = []
        self._content_source: Any = None
        self._program_channel: int | None = None
        # Program transition: a transition source sits on the program channel and
        # holds the active scene; scene switches animate through take().
        self._transition: Any | None = None
        self._transition_kind: str | None = None
        self._active_scene_id: str | None = None
        self._pending: dict[str, tuple[str, str, int]] = {}
        self._token_seq = 0

    @property
    def scene_ids(self) -> tuple[str, ...]:
        return tuple(self._scenes)

    def hydrate(
        self,
        document: dict,
        active_scenes: dict,
        content_source: Any | None = None,
    ) -> None:
        """Rebuild the scene graph from a document record and route the program.

        ``content_source`` (a libobs source fed by the content ingress) is placed
        for layers referencing the canonical content id; it is *referenced*, not
        owned, so the scene graph never releases it.
        """
        self.clear()
        self._content_source = content_source
        ob = self._runtime.ob
        canvas = self._runtime.video
        sources_by_id = {
            src["id"]: src
            for src in (document.get("sources") or ())
            if isinstance(src, dict) and src.get("id")
        }
        for scene_record in document.get("scenes") or ():
            scene_id = scene_record.get("id")
            if not scene_id:
                continue
            scene = ob.Scene.create(f"solin-scene-{scene_id}")
            self._scenes[scene_id] = scene
            for layer in scene_record.get("layers") or ():
                if not layer.get("visible", True):
                    continue
                self._add_layer(ob, scene, layer, canvas, sources_by_id, content_source)
        self._setup_program(active_scenes)

    def _add_layer(
        self,
        ob: Any,
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
        rect = layer.get("rect") or {}
        self._apply_item_geometry(item, rect, canvas, ob)
        if is_content:
            self._content_items.append(
                {"scene": scene, "item": item, "rect": rect, "placeholder": placeholder}
            )

    def _apply_item_geometry(self, item: Any, rect: dict, canvas: Any, ob: Any) -> None:
        # Scale the source into its normalized rect (SCALE_INNER preserves aspect),
        # so real sources of any native size (media, camera, image) fill the rect
        # rather than rendering at their own dimensions.
        item.pos = (
            float(rect.get("x", 0.0)) * canvas.width,
            float(rect.get("y", 0.0)) * canvas.height,
        )
        item.bounds = (
            max(1.0, float(rect.get("width", 1.0)) * canvas.width),
            max(1.0, float(rect.get("height", 1.0)) * canvas.height),
        )
        item.bounds_type = int(ob.BoundsType.SCALE_INNER)
        item.bounds_alignment = int(ob.Alignment.LEFT | ob.Alignment.TOP)

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
            return (self._runtime.camera_source(device, name), False)  # runtime-owned
        if kind == "rtsp_camera":
            uri = str(config.get("uri", ""))
            if not uri:
                return (None, False)
            source = ob.Source.create(
                "ffmpeg_source",
                f"solin-rtsp-{layer.get('id', 'layer')}",
                {"is_local_file": False, "input": uri, "reconnect_delay_sec": 2},
            )
            return (source, True)
        # image (needs app-side asset resolution) and scene_reference are not yet
        # wired → placeholder.
        return (None, False)

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
            rect = record["rect"]
            old_placeholder = record["placeholder"]
            try:
                order = int(old_item.order_position)
            except Exception:  # noqa: BLE001 - libobs boundary
                order = None
            if new_source is not None:
                source = new_source
                new_placeholder = None
            else:  # reverting to "no content" — stand in with a placeholder
                source = self._create_color(ob, {"id": "content", "rect": rect}, canvas,
                                            _PLACEHOLDER_COLOR)
                new_placeholder = source
            new_item = scene.add(source)  # added on top; restore its z-order below
            self._apply_item_geometry(new_item, rect, canvas, ob)
            if order is not None:
                try:
                    new_item.order_position = order
                except Exception:  # noqa: BLE001 - libobs boundary
                    log.debug("content item order restore errored", exc_info=True)
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
        self._transition = self._create_transition(_FALLBACK_KIND)
        self._transition_kind = _FALLBACK_KIND
        self._transition.set_source(scene.as_source())
        self._active_scene_id = program_scene_id
        self._runtime.set_channel_source(self._program_channel, self._transition)

    def _create_transition(self, model_kind: str) -> Any:
        ob = self._runtime.ob
        canvas = self._runtime.video
        obs_id = _TRANSITION_IDS.get(model_kind, _TRANSITION_IDS[_FALLBACK_KIND])
        settings = {"color": 0xFF000000} if obs_id == "fade_to_color_transition" else {}
        transition = ob.Transition.create(obs_id, f"solin-transition-{model_kind}", settings)
        try:
            transition.set_size(canvas.width, canvas.height)
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("transition set_size errored", exc_info=True)
        return transition

    def prepare(self, scene_id: str, model_kind: str, duration_ms: int) -> dict | None:
        """Stage a switch to ``scene_id``. Returns a token + resolved transition,
        or None if the scene is unknown."""
        if scene_id not in self._scenes:
            return None
        fallback_applied = model_kind not in _TRANSITION_IDS
        effective_kind = _FALLBACK_KIND if fallback_applied else model_kind
        self._token_seq += 1
        token = f"prep-{self._token_seq}"
        self._pending[token] = (scene_id, effective_kind, int(duration_ms))
        return {
            "token": token,
            "kind": effective_kind,
            "fallback_applied": fallback_applied,
            "fallback_reason": "unsupported transition kind" if fallback_applied else "",
        }

    def take(self, token: str) -> bool:
        """Execute a prepared switch, animating the program transition."""
        pending = self._pending.pop(token, None)
        if pending is None:
            return False
        scene_id, model_kind, duration_ms = pending
        scene = self._scenes.get(scene_id)
        if scene is None or self._transition is None:
            return False
        if model_kind != self._transition_kind:
            self._swap_transition(model_kind)
        self._transition.start(scene.as_source(), duration_ms)
        self._active_scene_id = scene_id
        return True

    def _swap_transition(self, model_kind: str) -> None:
        # Changing kind means a new transition source; carry the live scene into
        # it so the program does not blink.
        new_transition = self._create_transition(model_kind)
        active = self._scenes.get(self._active_scene_id) if self._active_scene_id else None
        if active is not None:
            new_transition.set_source(active.as_source())
        old, self._transition = self._transition, new_transition
        self._transition_kind = model_kind
        if self._program_channel is not None:
            self._runtime.set_channel_source(self._program_channel, new_transition)
        if old is not None:
            try:
                old.release()
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("old transition release errored", exc_info=True)

    def cancel_all(self) -> None:
        self._pending.clear()

    def clear(self) -> None:
        """Release the current scenes/sources (keeps the reserved channel)."""
        self._pending.clear()
        self._content_items = []
        self._content_source = None
        if self._program_channel is not None:
            try:
                self._runtime.set_channel_source(self._program_channel, None)
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("Could not clear the program channel", exc_info=True)
        if self._transition is not None:
            try:
                self._transition.release()
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("transition release errored", exc_info=True)
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
        self.clear()
        if self._program_channel is not None:
            try:
                self._runtime.release_channel(self._program_channel)
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("release_channel errored", exc_info=True)
            self._program_channel = None
