"""Build uncompressed AVI clips for native playback tests."""

from __future__ import annotations

import math
from pathlib import Path
import struct


AV_FRAME_NUMBER_BITS = 8
AV_FRAME_NUMBER_ROWS = 4
AV_LOW_TONE_HZ = 400
AV_HIGH_TONE_HZ = 1000


def write_av_clip(
    path: Path,
    *,
    duration: int = 5,
    alternate_colors: bool = False,
    constant_frequency: int | None = None,
    number_frames: bool = False,
) -> None:
    """Write red and blue video with PCM tones and optional per-frame markers."""
    width, fps, rate = 64, 10, 48000
    frame_size = width * width * 3
    frame_count = duration * fps
    if number_frames and frame_count > 1 << AV_FRAME_NUMBER_BITS:
        raise ValueError("Clip has too many frames for unique frame numbers")
    samples_per_frame = rate // fps

    def chunk(kind, data):
        return kind + struct.pack("<I", len(data)) + data + b"\0" * (len(data) % 2)

    header = struct.pack(
        "<14I",
        1000000 // fps,
        frame_size * fps + rate * 2,
        0,
        0x10,
        frame_count,
        0,
        2,
        frame_size,
        width,
        width,
        0,
        0,
        0,
        0,
    )
    video = struct.pack(
        "<4s4sIHH8I4h",
        b"vids",
        b"DIB ",
        0,
        0,
        0,
        0,
        1,
        fps,
        0,
        frame_count,
        frame_size,
        0xFFFFFFFF,
        0,
        0,
        0,
        width,
        width,
    )
    bitmap = struct.pack("<IiiHHIIiiII", 40, width, width, 1, 24, 0, frame_size, 0, 0, 0, 0)
    audio = struct.pack(
        "<4s4sIHH8I4h",
        b"auds",
        b"\0" * 4,
        0,
        0,
        0,
        0,
        2,
        rate * 2,
        0,
        rate * duration,
        samples_per_frame * 2,
        0xFFFFFFFF,
        2,
        0,
        0,
        0,
        0,
    )
    wave_format = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    video_list = chunk(b"LIST", b"strl" + chunk(b"strh", video) + chunk(b"strf", bitmap))
    audio_list = chunk(b"LIST", b"strl" + chunk(b"strh", audio) + chunk(b"strf", wave_format))
    headers = chunk(b"LIST", b"hdrl" + chunk(b"avih", header) + video_list + audio_list)
    tile_size = width // AV_FRAME_NUMBER_BITS * 3
    payload, index, offset = [], [], 4
    for frame in range(frame_count):
        blue = (frame // fps) % 2 == 1 if alternate_colors else frame >= 2 * fps
        pixel = b"\xff\0\0" if blue else b"\0\0\xff"
        if number_frames:
            # Number each frame with tiles that survive OBS color conversion.
            number_row = b"".join(
                (b"\xff" if frame & (1 << bit) else b"\0") * tile_size
                for bit in range(AV_FRAME_NUMBER_BITS)
            )
            # Repeat the strip on both edges so row order cannot affect the number.
            strip = number_row * AV_FRAME_NUMBER_ROWS
            picture = strip + pixel * (width * (width - 2 * AV_FRAME_NUMBER_ROWS)) + strip
        else:
            picture = pixel * (width * width)
        frequency = constant_frequency
        if frequency is None:
            frequency = AV_HIGH_TONE_HZ if blue else AV_LOW_TONE_HZ
        pcm = b"".join(
            struct.pack("<h", round(16000 * math.sin(2 * math.pi * frequency * i / rate)))
            for i in range(samples_per_frame)
        )
        for kind, data in ((b"00db", picture), (b"01wb", pcm)):
            entry = chunk(kind, data)
            payload.append(entry)
            index.append(struct.pack("<4sIII", kind, 0x10, offset, len(data)))
            offset += len(entry)
    data = b"AVI " + headers + chunk(b"LIST", b"movi" + b"".join(payload))
    data += chunk(b"idx1", b"".join(index))
    path.write_bytes(b"RIFF" + struct.pack("<I", len(data)) + data)
