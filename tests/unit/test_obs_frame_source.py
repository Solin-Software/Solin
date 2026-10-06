from types import SimpleNamespace

import pylibobs._ffi as pylibobs_ffi
import pytest

from solin.core.media.obs_frame_source import ObsFrameSource, _frame_ffi


@pytest.fixture
def uploaded_frames(monkeypatch):
    uploads = []

    def copy_frame(_source, frame):
        if frame != _frame_ffi.NULL:
            uploads.append({
                "flags": int(frame.flags),
                "pixels": b"".join(
                    bytes(_frame_ffi.buffer(
                        frame.data[0] + row * frame.linesize[0], frame.width * 4,
                    ))
                    for row in range(frame.height)
                ),
            })

    lib = SimpleNamespace(
        obs_source_output_video=copy_frame,
        obs_source_preload_video=copy_frame,
        obs_source_show_preloaded_video=lambda _source: None,
    )
    monkeypatch.setattr(pylibobs_ffi, "ffi", _frame_ffi)
    monkeypatch.setattr(pylibobs_ffi, "get_lib", lambda: lib)
    monkeypatch.setattr(pylibobs_ffi, "is_alive", lambda: True)
    return ObsFrameSource(SimpleNamespace(_ptr="source")), uploads


@pytest.mark.parametrize("reset", [False, True])
@pytest.mark.parametrize(
    ("data", "width", "height", "stride", "expected_flags", "pixels"),
    [
        (b"\x20\x40\xe0\xff" * 2, 2, 1, 8, 1, b"\x20\x40\xe0\xff" * 2),
        (b"\x20\x40\xe0\x80" * 2, 2, 1, 8, 0, b"\x20\x40\xe0\x80" * 2),
        (b"\xff\xff\xff\x00", 1, 1, 4, 0, b"\xff\xff\xff\x00"),
        (b"\xff\xff\xff\xff\xff\xff\xff\xfe", 2, 1, 8, 0,
         b"\xff\xff\xff\xff\xff\xff\xff\xfe"),
        (b"\x20\x40\xe0\xff\x00\x20\x40\xe0\xff", 1, 2, 5, 1,
         b"\x20\x40\xe0\xff" * 2),
        (b"\x20\x40\xe0\xff\x00\x20\x40\xe0\x80", 1, 2, 5, 0,
         b"\x20\x40\xe0\xff\x20\x40\xe0\x80"),
        (b"\x20\x40\xe0\xff\x00\x00\x00\x00", 1, 1, 4, 1,
         b"\x20\x40\xe0\xff"),
    ],
    ids=["opaque", "translucent", "transparent", "last-pixel-alpha",
         "padded-rows", "padded-row-alpha", "trailing-capacity"],
)
def test_upload_declares_linear_alpha_only_for_fully_opaque_pixels(
    uploaded_frames, reset, data, width, height, stride, expected_flags, pixels,
):
    source, uploads = uploaded_frames
    assert source.push_bgra(data, width, height, stride, reset=reset)
    assert uploads == [{"flags": expected_flags, "pixels": pixels}]


@pytest.mark.parametrize("reset", [False, True])
def test_upload_updates_alpha_semantics_for_every_frame(uploaded_frames, reset):
    source, uploads = uploaded_frames
    for alpha in (255, 128, 255):
        assert source.push_bgra(bytes((10, 20, 30, alpha)), 1, 1, 4, reset=reset)
    assert [frame["flags"] for frame in uploads] == [1, 0, 1]


@pytest.mark.parametrize("buffer_type", [bytes, bytearray, memoryview])
def test_upload_accepts_contiguous_bgra_buffers(uploaded_frames, buffer_type):
    source, uploads = uploaded_frames
    pixels = bytes((32, 64, 224, 255))
    assert source.push_bgra(buffer_type(pixels), 1, 1, 4)
    assert uploads == [{"flags": 1, "pixels": pixels}]


@pytest.mark.parametrize(
    ("data", "width", "height", "stride"),
    [(b"", 1, 1, 4), (b"\xff" * 3, 1, 1, 4), (b"\xff" * 7, 1, 2, 4),
     (b"\xff" * 12, 2, 2, 8), (b"\xff" * 4, 1, 1, 3),
     (b"\xff" * 4, 1, 1, -4), (b"\xff" * 4, 0, 1, 4),
     (b"\xff" * 4, 1, 0, 4)],
)
def test_upload_rejects_incomplete_bgra_layout_before_native_calls(
    uploaded_frames, data, width, height, stride,
):
    source, uploads = uploaded_frames
    assert not source.push_bgra(data, width, height, stride)
    assert uploads == []
