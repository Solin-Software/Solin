"""Program recording for the scene-engine sidecar (libobs ffmpeg_muxer).

Records the program composite (the main video mix) plus the main audio mix to a
file, using libobs' own recording pipeline: an ``ffmpeg_muxer`` output fed by a
video encoder (``obs_x264`` — universal + reliable) and an AAC audio encoder,
both auto-attached to the global mix.

Audio track: the encoder binds the main audio mix, so recordings carry an audio
track. Selecting specific microphone / system-audio devices (adding capture
sources to the mix) is a later slice; today the track is whatever the main mix
carries.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# Recording muxers, in order of preference. ``mp4_output`` is OBS' modern
# in-process hybrid-MP4 muxer (30.2+): it writes the container directly, needs no
# external helper binary, and finalizes a playable file even after a crash.
# ``ffmpeg_muxer`` is the classic muxer — robust where the ``obs-ffmpeg-mux``
# helper executable ships, but it spawns that helper (unavailable in some
# libobs distributions), so it is only a fallback here.
_OUTPUT_KINDS = ("mp4_output", "ffmpeg_muxer")
_VIDEO_ENCODER = "obs_x264"
_AUDIO_ENCODER = "ffmpeg_aac"


class LibobsRecorder:
    """Owns a libobs recording output + its encoders."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._output: Any = None
        self._video_encoder: Any = None
        self._audio_encoder: Any = None
        self._path = ""

    @property
    def active(self) -> bool:
        return self._output is not None

    @property
    def path(self) -> str:
        return self._path

    def start(self, path: str, *, video_bitrate: int = 6000, audio_bitrate: int = 160) -> bool:
        if self._output is not None:
            return False  # already recording
        ob = self._runtime.ob
        video_encoder = audio_encoder = output = None
        try:
            output_kind = self._select_output_kind(ob.enum_output_types())
            if output_kind is None:
                log.warning(
                    "no recording muxer is available (looked for %s)",
                    ", ".join(_OUTPUT_KINDS),
                )
                return False
            video_encoder = ob.VideoEncoder.create(
                _VIDEO_ENCODER, "solin-rec-video", {"bitrate": int(video_bitrate)}
            )
            audio_encoder = ob.AudioEncoder.create(
                _AUDIO_ENCODER, "solin-rec-audio", {"bitrate": int(audio_bitrate)}
            )
            output = ob.Output.create(output_kind, "solin-recording", {"path": str(path)})
            output.set_video_encoder(video_encoder)
            output.set_audio_encoder(audio_encoder, 0)
            if not output.start():
                raise RuntimeError("recording output.start() returned False")
        except Exception:  # noqa: BLE001 - encoder/output/libobs boundary
            log.warning("Could not start recording to %r", path, exc_info=True)
            for obj in (output, video_encoder, audio_encoder):
                if obj is not None:
                    try:
                        obj.release()
                    except Exception:  # noqa: BLE001 - best-effort cleanup
                        log.debug("recording cleanup release errored", exc_info=True)
            return False
        self._output = output
        self._video_encoder = video_encoder
        self._audio_encoder = audio_encoder
        self._path = str(path)
        return True

    @staticmethod
    def _select_output_kind(available: Any) -> str | None:
        registered = set(available)
        for kind in _OUTPUT_KINDS:
            if kind in registered:
                return kind
        return None

    def stop(self) -> None:
        output = self._output
        if output is not None:
            try:
                output.stop(wait=True)  # finalize the container (moov atom etc.)
            except Exception:  # noqa: BLE001 - libobs boundary
                log.debug("recording output stop errored", exc_info=True)
        self._release()

    def _release(self) -> None:
        self._path = ""
        for attr in ("_output", "_video_encoder", "_audio_encoder"):
            obj = getattr(self, attr, None)
            setattr(self, attr, None)
            if obj is not None:
                try:
                    obj.release()
                except Exception:  # noqa: BLE001 - libobs boundary
                    log.debug("%s release errored", attr, exc_info=True)
