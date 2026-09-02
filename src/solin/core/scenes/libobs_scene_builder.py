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


class LibobsSceneGraph:
    """Owns the libobs scenes/sources built for one hydrate snapshot."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._scenes: dict[str, Any] = {}
        self._sources: list[Any] = []
        self._program_channel: int | None = None

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
        ob = self._runtime.ob
        canvas = self._runtime.video
        for scene_record in document.get("scenes") or ():
            scene_id = scene_record.get("id")
            if not scene_id:
                continue
            scene = ob.Scene.create(f"solin-scene-{scene_id}")
            self._scenes[scene_id] = scene
            for layer in scene_record.get("layers") or ():
                if not layer.get("visible", True):
                    continue
                self._add_layer(ob, scene, layer, canvas, content_source)
        self._route_program(active_scenes)

    def _add_layer(
        self, ob: Any, scene: Any, layer: dict, canvas: Any, content_source: Any | None
    ) -> None:
        if content_source is not None and layer.get("source_id") == _CONTENT_SOURCE_ID:
            source = content_source  # referenced (the consumer owns its lifetime)
        else:
            source = self._create_placeholder(ob, layer, canvas)
            self._sources.append(source)  # owned → released on clear
        item = scene.add(source)
        rect = layer.get("rect") or {}
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

    def _create_placeholder(self, ob: Any, layer: dict, canvas: Any) -> Any:
        rect = layer.get("rect") or {}
        width = max(1, round(float(rect.get("width", 1.0)) * canvas.width))
        height = max(1, round(float(rect.get("height", 1.0)) * canvas.height))
        return ob.Source.create(
            "color_source_v3",
            f"solin-placeholder-{layer.get('id', 'layer')}",
            {"color": _PLACEHOLDER_COLOR, "width": width, "height": height},
        )

    def _route_program(self, active_scenes: dict) -> None:
        program_scene_id = active_scenes.get(_PROGRAM_BUS) or active_scenes.get(_MIRROR_BUS)
        scene = self._scenes.get(program_scene_id) if program_scene_id else None
        if scene is None:
            return
        if self._program_channel is None:
            self._program_channel = self._runtime.acquire_channel()
        self._runtime.set_channel_source(self._program_channel, scene.as_source())

    def clear(self) -> None:
        """Release the current scenes/sources (keeps the reserved channel)."""
        if self._program_channel is not None:
            try:
                self._runtime.set_channel_source(self._program_channel, None)
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("Could not clear the program channel", exc_info=True)
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
