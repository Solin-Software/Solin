"""Effective sidecar demand against real native sources in isolated processes.

Only output availability is gated by fixtures; the sidecar, graph, transitions,
preview egress and shared idle decoder are real. No physical display or camera
is required. Playback fixtures and native assertions are shared with idle smoke.
"""

from __future__ import annotations

import json
import hashlib
import os
from contextlib import ExitStack
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any

import pytest

from tests.integration import test_native_idle_screen as native

pytestmark = native.pytestmark


def _scenario(directory: Path, live_route: str) -> dict:
    from solin.core.media.obs_runtime import ObsRuntime
    from solin.core.media.obs_source_render import shutdown as shutdown_readback
    from solin.core.scenes.content_frame_channel import SharedFrameChannelWriter
    from solin.core.scenes.idle import IdleScreenState
    from solin.core.scenes.ipc_protocol import SceneIpcEnvelope
    from solin.core.scenes.libobs_media_source import STATE_PAUSED, STATE_PLAYING
    from solin.core.scenes.libobs_sidecar import LibobsSidecarEngine

    deadline = time.monotonic() + native._SCENARIO_TIMEOUT_SECONDS
    video, image, _ = native._fixtures(directory, deadline)
    sequence = 0

    def request(message, payload=None):
        nonlocal sequence
        sequence += 1
        return SceneIpcEnvelope(
            message,
            f"native-demand-{sequence}",
            "native-demand",
            "generation-1",
            sequence,
            7,
            int((time.monotonic() + native._remaining(deadline)) * 1000),
            payload or {},
        )

    with ExitStack() as stack:
        runtime = ObsRuntime()
        stack.callback(runtime.shutdown)
        runtime.ensure_started(width=native._WIDTH, height=native._HEIGHT, fps=native._FPS)
        engine = LibobsSidecarEngine(lambda: runtime)
        stack.callback(engine.shutdown)
        hello = engine.handle(request("hello"))
        assert hello is not None and hello.payload["hardware_compositing"]
        # Egress callbacks must stop before their readback pool; the pool must
        # close before engine.shutdown releases the graphics context.
        stack.callback(shutdown_readback)
        preview_egress, program_egress = engine._preview_egress, engine._program_egress
        assert preview_egress is not None and program_egress is not None
        stack.callback(preview_egress.shutdown)
        stack.callback(program_egress.shutdown)
        document = {
            "revision": 7,
            "sources": [
                {"id": "idle", "type": "idle_screen", "enabled": False, "configuration": {}},
                {
                    "id": "nested-reference",
                    "type": "scene_reference",
                    "enabled": False,
                    "configuration": {"target_scene_id": "idle-child"},
                },
                {"id": "background", "type": "color", "configuration": {"color": "#112233"}},
            ],
            "scenes": [
                {"id": "idle-child", "layers": [{"id": "idle-layer", "source_id": "idle"}]},
                {
                    "id": "nested",
                    "layers": [{"id": "nested-layer", "source_id": "nested-reference"}],
                },
                {
                    "id": "other",
                    "layers": [
                        {"id": "background-layer", "source_id": "background"},
                        {"id": "hidden-idle", "source_id": "idle", "visible": False},
                    ],
                },
            ],
        }
        state = IdleScreenState(10, str(video), str(image), 3)
        hydrated = engine.handle(
            request(
                "hydrate",
                {
                    "document": document,
                    "idle_screen": state.to_record(),
                    "active_scenes": {
                        "virtual_camera": "nested",
                        "media_windows": "nested",
                        "editor": "nested",
                    },
                    "render_enabled": {
                        "virtual_camera": True,
                        "media_windows": True,
                        "editor": True,
                    },
                    "output_enabled": {"virtual_camera": False},
                },
            )
        )
        assert hydrated is not None and hydrated.payload["applied"]
        owner, graph = engine._idle_source, engine._scene_graph
        projection_route = engine._projection_route
        assert owner is not None and graph is not None and projection_route is not None
        assert owner._producer is not None
        idle = native._idle_decoder(owner)
        assert idle is not None
        presentation = owner._producer.source
        shared = owner.source
        initial_hash = hashlib.sha256(
            native._assert_visible_contain(native._frame(shared))[0]
        ).hexdigest()
        assert graph.scene_uses_idle_source("nested")
        assert not graph.scene_uses_idle_source("other")
        assert runtime.active_source_tree_contains(graph.program_source, shared)
        assert runtime.active_source_tree_contains(projection_route.transition_source, shared)
        assert native._native_decoder_count() == 1
        patch = stack.enter_context(pytest.MonkeyPatch.context())
        original_restart = runtime.ob.Source.media_restart
        restarts = 0

        def restart(source):
            nonlocal restarts
            decoder = native._idle_decoder(owner)
            if decoder is not None and source._ptr == decoder._ptr:
                restarts += 1
            original_restart(source)

        patch.setattr(runtime.ob.Source, "media_restart", restart)

        def paused():
            native._wait_decoder(
                owner, lambda decoder: decoder.media_state == STATE_PAUSED, deadline
            )
            assert (
                hashlib.sha256(native._assert_visible_contain(native._frame(shared))[0]).hexdigest()
                == initial_hash
            )
            time.sleep(0.15)
            assert (
                hashlib.sha256(native._assert_visible_contain(native._frame(shared))[0]).hexdigest()
                == initial_hash
            )

        # Program really contains idle and aggregate scheduling is enabled, but
        # no editor/program egress, display, recording or virtual camera exists.
        paused()
        assert restarts == 0 and not owner._demand

        preview = SharedFrameChannelWriter(native._READBACK_WIDTH, native._READBACK_HEIGHT)
        stack.callback(preview.close)
        preview_egress.configure(
            {
                "transport": "shared_memory_bgra",
                "handle_token": preview.name,
                "width": native._READBACK_WIDTH,
                "height": native._READBACK_HEIGHT,
            }
        )
        assert preview_egress.active
        native._wait_for(lambda: owner._demand, deadline)
        paused()
        assert restarts == 0 and not owner._live_demand
        preview_position = idle.media_time

        # Availability gates replace only physical output adapters. Demand still
        # walks the actual OBS transition/scene tree through borrowed pointers.
        camera = SimpleNamespace(active=False)
        targets = []
        patch.setattr(engine, "_virtual_camera", camera)
        patch.setattr(
            type(engine._window_output), "render_targets", property(lambda _: tuple(targets))
        )
        rendered_projection_frames = 0
        render_errors = []

        def render_projection(_width, _height):
            nonlocal rendered_projection_frames
            if not targets:
                return
            try:
                # A transition outside the main view settles while its actual
                # display renders it. Exercise that native path offscreen.
                assert native._frame(projection_route.transition_source) is not None
                rendered_projection_frames += 1
            except Exception as error:  # noqa: BLE001 - surface native callback failures
                render_errors.append(str(error))

        callback = runtime.ob.add_main_render_callback(render_projection)
        stack.callback(runtime.ob.remove_main_render_callback, callback)
        if live_route == "projection":
            targets.append(("projection", ""))
        else:
            camera.active = True
        idle = native._wait_decoder(
            owner,
            lambda decoder: (
                restarts == 1 and decoder.media_state == STATE_PLAYING and decoder.media_time < 200
            ),
            deadline,
        )
        first_live_position = idle.media_time
        if live_route == "projection":
            camera.active = True
        else:
            targets.append(("projection", ""))
        time.sleep(0.15)
        assert restarts == 1, "An additional live consumer restarted the decoder"
        if live_route == "projection":
            camera.active = False
        else:
            targets.clear()
        preview_egress.configure(None)
        assert not preview_egress.active

        bus = "media_windows" if live_route == "projection" else "virtual_camera"

        def take(scene_id):
            prepared = engine.handle(
                request(
                    "prepare_scene",
                    {
                        "bus_id": bus,
                        "scene_id": scene_id,
                        "transition": {"kind": "cut", "duration_ms": 0},
                        "content_media_epoch": None,
                        "content_source_kind": "frames",
                    },
                )
            )
            assert prepared is not None and prepared.message_type == "scene_prepared"
            taken = engine.handle(
                request(
                    "take_prepared",
                    {
                        "bus_id": bus,
                        "scene_id": scene_id,
                        "preparation_token": prepared.payload["preparation_token"],
                    },
                )
            )
            assert taken is not None and taken.payload["applied"]

        take("other")
        try:
            paused()
        except AssertionError as error:
            from pylibobs._ffi import get_lib

            lib: Any = get_lib()
            diagnostics = {
                "live_route": live_route,
                "demand": owner._demand,
                "live": owner._live_demand,
                "camera_active": camera.active,
                "targets": targets,
                "preview_active": preview_egress.active,
                "projection_scene": projection_route.scene_id,
                "projection_contains_idle": runtime.active_source_tree_contains(
                    projection_route.transition_source, shared
                ),
                "projection_scene_contains_idle": runtime.active_source_tree_contains(
                    projection_route.scene_source, shared
                ),
                "transition_time": lib.obs_transition_get_time(
                    projection_route.transition_source._ptr
                ),
                "rendered_projection_frames": rendered_projection_frames,
                "render_errors": render_errors,
            }
            raise AssertionError(json.dumps(diagnostics)) from error
        root = (
            projection_route.transition_source
            if live_route == "projection"
            else graph.program_source
        )
        retained_origin = runtime.active_source_tree_contains(root, shared)
        assert not runtime.transition_source_tree_contains(root, shared)
        assert not owner._demand and restarts == 1
        take("nested")
        idle = native._wait_decoder(
            owner,
            lambda decoder: (
                restarts == 2 and decoder.media_state == STATE_PLAYING and decoder.media_time < 200
            ),
            deadline,
        )
        assert runtime.transition_source_tree_contains(root, shared)
        assert native._native_decoder_count() == 1
        assert owner._producer is not None and owner._producer.source is presentation
        assert not render_errors, render_errors
        assert rendered_projection_frames > 0
        return {
            "live_route": live_route,
            "preview_before_live_ms": preview_position,
            "first_live_position_ms": first_live_position,
            "return_position_ms": idle.media_time,
            "restarts": restarts,
            "native_decoders": 1,
            "coarse_flags_without_consumer_paused": True,
            "preview_first_frame_static": True,
            "hidden_scene_paused": True,
            "nested_disabled_definitions_rendered": True,
            "rendered_projection_frames": rendered_projection_frames,
            "completed_origin_retained_in_raw_tree": retained_origin,
        }


@pytest.mark.parametrize("live_route", ["projection", "virtual_camera"])
def test_native_sidecar_uses_effective_consumers_and_actual_nested_source_tree(
    tmp_path, live_route
):
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-X",
            "faulthandler",
            "-m",
            "tests.integration.test_native_idle_demand",
            str(tmp_path),
            live_route,
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=native._PROCESS_TIMEOUT_SECONDS,
        env=dict(os.environ, SOLIN_RECORD_HW_ENCODE="0"),
    )
    assert result.returncode == 0, (result.stdout + result.stderr)[-16000:]
    report = json.loads((tmp_path / "demand.json").read_text(encoding="utf-8"))
    assert report["restarts"] == 2 and report["native_decoders"] == 1
    print("NATIVE_IDLE_SIDECAR_DEMAND", json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    directory, route = Path(sys.argv[1]), sys.argv[2]
    report = _scenario(directory, route)
    (directory / "demand.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
