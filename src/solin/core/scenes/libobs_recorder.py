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
import os
from typing import Any

log = logging.getLogger(__name__)

# Recording muxers, in order of preference. ``mp4_output`` is OBS' modern
# in-process hybrid-MP4 muxer (30.2+): it writes the container directly, needs no
# external helper binary, and finalizes a playable file even after a crash.
# ``ffmpeg_muxer`` is the classic muxer — robust where the ``obs-ffmpeg-mux``
# helper executable ships, but it spawns that helper (unavailable in some
# libobs distributions), so it is only a fallback here.
_OUTPUT_KINDS = ("mp4_output", "ffmpeg_muxer")

# Hardware H.264 encoders in preference order, then the universal CPU encoder as
# the final fallback. Recording tries each *registered* candidate in turn and
# drops to the next on any create/start failure, so a box without working
# hardware encode still records via x264. Only encoders the runtime actually
# loaded (``enum_encoder_types``) are attempted.
_HW_VIDEO_ENCODERS = (
    "obs_nvenc_h264_tex",  # NVIDIA NVENC (texture input, modern OBS)
    "jim_nvenc",           # NVIDIA NVENC (older id)
    "obs_qsv11",           # Intel Quick Sync
    "h264_texture_amf",    # AMD AMF
    "ffmpeg_vaapi",        # generic VAAPI (Linux)
)
_CPU_VIDEO_ENCODER = "obs_x264"
_AUDIO_ENCODER = "ffmpeg_aac"


def _hw_encode_enabled() -> bool:
    """Whether to try hardware video encoders for recording (on by default).

    Set ``SOLIN_RECORD_HW_ENCODE=0`` to force the CPU encoder (x264) — e.g. a box
    whose hardware encoder is present but produces bad files. Even with hardware
    enabled, recording always falls back to x264 when no HW encoder can start.
    """
    flag = os.environ.get("SOLIN_RECORD_HW_ENCODE", "1").strip().lower()
    return flag not in ("0", "false", "off", "no")


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
        output_kind = self._select_output_kind(ob.enum_output_types())
        if output_kind is None:
            log.warning(
                "no recording muxer is available (looked for %s)",
                ", ".join(_OUTPUT_KINDS),
            )
            return False
        candidates = self._video_encoder_candidates(ob.enum_encoder_types())
        for encoder_id in candidates:
            if self._try_start(
                ob, output_kind, encoder_id, str(path), int(video_bitrate), int(audio_bitrate)
            ):
                log.info("recording started with the %s video encoder", encoder_id)
                return True
        log.warning("Could not start recording to %r with any encoder", path)
        return False

    def _try_start(
        self,
        ob: Any,
        output_kind: str,
        video_encoder_id: str,
        path: str,
        video_bitrate: int,
        audio_bitrate: int,
    ) -> bool:
        """Attempt one full recording start with ``video_encoder_id``.

        Returns True and records on success; on any failure (encoder create,
        output create/bind, or ``output.start()`` returning False) it releases
        whatever it made and returns False so the caller tries the next
        candidate — this is the hardware→software fallback.
        """
        video_encoder = audio_encoder = output = None
        try:
            video_encoder = ob.VideoEncoder.create(
                video_encoder_id, "solin-rec-video", {"bitrate": video_bitrate}
            )
            audio_encoder = ob.AudioEncoder.create(
                _AUDIO_ENCODER, "solin-rec-audio", {"bitrate": audio_bitrate}
            )
            output = ob.Output.create(output_kind, "solin-recording", {"path": path})
            output.set_video_encoder(video_encoder)
            output.set_audio_encoder(audio_encoder, 0)
            if not output.start():
                raise RuntimeError("recording output.start() returned False")
        except Exception as exc:  # noqa: BLE001 - encoder/output/libobs boundary
            # Expected on boxes without this hardware encoder — fall back quietly.
            log.info(
                "recording video encoder %r unavailable (%s); trying the next",
                video_encoder_id, type(exc).__name__,
            )
            log.debug("recording encoder %r failure detail", video_encoder_id, exc_info=True)
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
        self._path = path
        return True

    @staticmethod
    def _select_output_kind(available: Any) -> str | None:
        registered = set(available)
        for kind in _OUTPUT_KINDS:
            if kind in registered:
                return kind
        return None

    @staticmethod
    def _video_encoder_candidates(available: Any) -> list[str]:
        """Registered hardware encoders (preferred) then x264 as the last resort."""
        registered = set(available)
        ordered: list[str] = []
        if _hw_encode_enabled():
            ordered = [enc for enc in _HW_VIDEO_ENCODERS if enc in registered]
        ordered.append(_CPU_VIDEO_ENCODER)  # universal CPU fallback, always last
        return ordered

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
