from __future__ import annotations

from types import SimpleNamespace
import weakref

import pylibobs._ffi as pylibobs_ffi
import pytest

from solin.core.media.pylibobs_compat import set_video_compat


def test_pylibobs_012_video_init_keeps_graphics_buffer_alive(monkeypatch) -> None:
    graphics_owner = object()
    video = SimpleNamespace()

    class Ffi:
        @staticmethod
        def new(declaration, value=None):
            if declaration == "char[]":
                assert value == b"/runtime/libobs-opengl.dylib"
                return graphics_owner
            assert declaration == "struct obs_video_info *"
            return video

    def reset_video(value):
        assert value is video
        assert value.graphics_module is graphics_owner
        return 0

    monkeypatch.setattr(pylibobs_ffi, "ffi", Ffi())
    monkeypatch.setattr("solin.core.media.pylibobs_compat.platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        pylibobs_ffi,
        "get_lib",
        lambda: SimpleNamespace(obs_reset_video=reset_video),
    )
    binding = SimpleNamespace(
        __version__="0.1.2",
        VideoFormat=SimpleNamespace(NV12=2),
        ColorSpace=SimpleNamespace(DEFAULT=0),
        VideoRange=SimpleNamespace(DEFAULT=0),
        ScaleType=SimpleNamespace(BICUBIC=2),
    )

    set_video_compat(
        SimpleNamespace(),
        binding=binding,
        width=320,
        height=180,
        fps_num=30,
        graphics_module="/runtime/libobs-opengl.dylib",
    )

    assert video.fps_num == 30
    assert video.fps_den == 1
    assert video.base_width == video.output_width == 320
    assert video.base_height == video.output_height == 180
    assert video.output_format == 2
    assert video.gpu_conversion is True
    assert video.scale_type == 2


def test_future_pylibobs_versions_delegate_to_upstream_set_video() -> None:
    calls = []
    context = SimpleNamespace(set_video=lambda *args, **kwargs: calls.append((args, kwargs)))

    set_video_compat(
        context,
        binding=SimpleNamespace(__version__="0.1.3"),
        width=640,
        height=360,
        fps_num=60,
        graphics_module="graphics.dylib",
    )

    assert calls == [((640, 360), {"fps_num": 60, "graphics_module": "graphics.dylib"})]


@pytest.mark.parametrize("system", ["Darwin", "Linux", "Windows"])
@pytest.mark.parametrize("error", [0, -5, -99])
def test_pinned_binding_returns_owner_for_the_real_cffi_graphics_buffer(
    monkeypatch, system, error
) -> None:
    ffi = pylibobs_ffi.ffi
    graphics_owner = None
    graphics_name = "libobs-d3d11" if system == "Windows" else "libobs-opengl"

    def allocate(declaration, value=None):
        nonlocal graphics_owner
        allocation = ffi.new(declaration, value) if value is not None else ffi.new(declaration)
        if declaration == "char[]":
            graphics_owner = weakref.ref(allocation)
        return allocation

    def reset_video(video):
        assert graphics_owner is not None and graphics_owner() is not None
        assert ffi.string(video.graphics_module).decode() == graphics_name
        assert video.base_width == video.output_width == 320
        assert video.base_height == video.output_height == 180
        return error

    monkeypatch.setattr(pylibobs_ffi, "ffi", SimpleNamespace(new=allocate))
    monkeypatch.setattr(
        pylibobs_ffi, "get_lib", lambda: SimpleNamespace(obs_reset_video=reset_video)
    )
    monkeypatch.setattr("solin.core.media.pylibobs_compat.platform.system", lambda: system)
    binding = SimpleNamespace(
        __version__="0.1.2",
        VideoFormat=SimpleNamespace(NV12=2),
        ColorSpace=SimpleNamespace(DEFAULT=0),
        VideoRange=SimpleNamespace(DEFAULT=0),
        ScaleType=SimpleNamespace(BICUBIC=2),
    )

    def initialize():
        return set_video_compat(
            SimpleNamespace(),
            binding=binding,
            width=320,
            height=180,
            fps_num=30,
        )

    if error:
        expected = "general failure" if error == -5 else "unknown error -99"
        with pytest.raises(RuntimeError, match=expected):
            initialize()
    else:
        owner = initialize()
        assert graphics_owner is not None and graphics_owner() is owner
        assert owner is not None
        del owner
        assert graphics_owner is not None and graphics_owner() is None
