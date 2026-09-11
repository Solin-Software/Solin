"""Pitch-preserving time stretch for media playback.

libobs changes playback rate by varispeed — it resamples, the way speeding up a
tape does, so the pitch rises with the rate. Nothing in the shipped obs plugin set
does tempo without pitch (the audio filters are gain, EQ, noise suppression, gate,
compressor, limiter, expander), and ``media_playback`` exposes no filter graph.

ffmpeg's ``atempo`` filter does exactly this and ffmpeg is already a runtime
dependency, so the stretched audio is produced there and played back as its own
source, while the media source's own audio is muted.
"""

from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import threading
from typing import Any

log = logging.getLogger(__name__)

# atempo refuses anything outside this range in a single pass, so wider stretches
# are chained. The bounds come from the filter itself, not from Solin.
_ATEMPO_MINIMUM = 0.5
_ATEMPO_MAXIMUM = 2.0

# Below this, a rate is not audibly different from normal and is not worth a
# second decode of the whole track.
_RATE_EPSILON = 0.001

# Where the stretched audio is handed over. A localhost TCP stream rather than a
# fifo: os.mkfifo does not exist on Windows, while ffmpeg's tcp protocol is the
# same everywhere and both ends of this hand-off are ffmpeg.
_LISTEN_HOST = "127.0.0.1"

# Long enough for ffmpeg to open the input and bind, short enough not to be felt.
_BIND_GRACE_S = 0.4

# How long ffmpeg holds the port open waiting for the source, in microseconds.
_LISTEN_TIMEOUT_US = 10_000_000

# How long to wait for obs to buffer the stretched stream and start emitting it.
_AUDIO_FLOW_TIMEOUT_S = 2.0


def is_unity_rate(rate: float) -> bool:
    """Whether ``rate`` is close enough to normal speed to need no stretching."""
    return abs(float(rate) - 1.0) <= _RATE_EPSILON


def atempo_chain(rate: float) -> list[float]:
    """Split ``rate`` into atempo stages, each within the filter's own limits.

    A single atempo accepts 0.5–2.0. Anything further is reached by chaining, so
    0.25 becomes two halvings and 4.0 two doublings. Stages are kept equal rather
    than maximal so the artefacts are spread evenly instead of concentrated in one
    extreme pass.
    """
    rate = float(rate)
    if rate <= 0.0:
        raise ValueError("Playback rate must be positive")
    if is_unity_rate(rate):
        return []

    stages = 1
    while True:
        stage_rate = rate ** (1.0 / stages)
        if _ATEMPO_MINIMUM <= stage_rate <= _ATEMPO_MAXIMUM:
            return [stage_rate] * stages
        stages += 1
        if stages > 16:  # 2**16 either way: far past anything usable
            raise ValueError("Playback rate is out of range")


def atempo_filter(rate: float) -> str:
    """The ffmpeg ``-filter:a`` argument that stretches audio to ``rate``."""
    return ",".join(f"atempo={stage:.6f}" for stage in atempo_chain(rate))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((_LISTEN_HOST, 0))
        return int(probe.getsockname()[1])


class TempoAudioCompanion:
    """Plays a media file's audio time-stretched, without shifting its pitch.

    Owns an ffmpeg process doing the stretch and a second libobs source playing
    the result. The caller mutes the real media source's audio while this runs, so
    the two never overlap.

    Every failure path is silent and non-fatal: if ffmpeg is missing, the port is
    taken, or the process dies, :meth:`start` returns False and the caller simply
    keeps libobs' own varispeed. A meeting must never lose audio over this.
    """

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._process: subprocess.Popen | None = None
        self._source: Any = None
        self._active = False
        self._lock = threading.RLock()

    @property
    def running(self) -> bool:
        return self._source is not None

    def command(self, path: str, position_ms: int, rate: float, port: int) -> list[str]:
        """The ffmpeg invocation that produces the stretched audio."""
        start_seconds = max(0, int(position_ms)) / 1000.0
        target = (
            f"tcp://{_LISTEN_HOST}:{port}"
            f"?listen=1&listen_timeout={_LISTEN_TIMEOUT_US}"
        )
        return [
            "ffmpeg",
            "-loglevel", "error",
            "-nostdin",
            # Seek before the input so ffmpeg skips rather than decodes to here.
            "-ss", f"{start_seconds:.3f}",
            "-i", path,
            "-vn",
            "-filter:a", atempo_filter(rate),
            "-f", "wav",
            "-y", target,
        ]

    def start(
        self,
        path: str,
        *,
        position_ms: int,
        rate: float,
        volume_percent: int = 100,
        paused: bool = False,
    ) -> bool:
        """Begin stretched playback from ``position_ms``. False if unavailable.

        ``paused`` opens the stretch without letting it play, so the caller can
        release it at a moment of its choosing.
        """
        self.stop()
        if not path or is_unity_rate(rate):
            return False
        if shutil.which("ffmpeg") is None:
            log.info("ffmpeg is not on PATH; keeping libobs' own playback rate")
            return False
        try:
            command = self.command(path, position_ms, rate, _free_port())
        except ValueError:
            return False  # a rate atempo cannot express
        except OSError:
            log.warning("Could not reserve a port for stretched audio", exc_info=True)
            return False
        port = int(command[-1].rsplit(":", 1)[1].split("?", 1)[0])

        with self._lock:
            try:
                self._process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            except OSError:
                log.warning("Could not start the audio stretch", exc_info=True)
                self._process = None
                return False
            if not self._await_listener() or not self._open_source(
                port, volume_percent, paused
            ):
                self.stop()
                return False
        return True

    def _await_listener(self) -> bool:
        """Give ffmpeg a moment to bind before the source tries to attach.

        Deliberately does NOT connect to check: ffmpeg's tcp listener accepts
        exactly one connection, so a probe would consume the very slot the audio
        source needs, and ffmpeg would exit as soon as the probe closed. If the
        source is still early, obs reconnects on its own.
        """
        waited = 0.0
        step = 0.05
        clock = threading.Event()
        while waited < _BIND_GRACE_S:
            process = self._process
            if process is not None and process.poll() is not None:
                log.warning("The audio stretch exited before it served anything")
                return False
            clock.wait(step)
            waited += step
        return True

    def _open_source(self, port: int, volume_percent: int, paused: bool = False) -> bool:
        from solin.core.media.obs_runtime import MONITORING_MONITOR_ONLY

        try:
            source = self._runtime.ob.Source.create(
                "ffmpeg_source",
                f"solin-tempo-audio-{port}",
                {
                    "is_local_file": False,
                    "input": f"tcp://{_LISTEN_HOST}:{port}",
                    "restart_on_activate": False,
                    "log_changes": False,
                },
            )
        except Exception:  # noqa: BLE001 - source creation boundary
            log.warning("Could not create the stretched audio source", exc_info=True)
            return False
        if source is None:
            return False

        self._source = source
        self.set_volume(volume_percent)
        try:
            self._runtime.set_source_monitoring(source, MONITORING_MONITOR_ONLY)
        except Exception:  # noqa: BLE001 - monitoring is best-effort
            log.warning("Could not monitor the stretched audio", exc_info=True)
        self._set_active(True)
        # Play first either way: an ffmpeg_source only reaches out to its input
        # once it is running, and a stream that has never been opened has nothing
        # buffered to release later.
        self.set_paused(False)
        if paused:
            self.await_audio()
            self.set_paused(True)
        return True

    @property
    def elapsed_ms(self) -> int:
        """How far the stretched stream has played, in real time."""
        source = self._source
        if source is None:
            return 0
        try:
            return max(0, int(source.media_time))
        except Exception:  # noqa: BLE001 - libobs boundary
            return 0

    def await_audio(self, timeout_s: float = _AUDIO_FLOW_TIMEOUT_S) -> int:
        """Block until the stretched audio is actually playing; return its elapsed ms.

        obs has to connect to the stream and buffer it before a sample reaches the
        speakers. The caller holds the picture still over that gap and then uses the
        returned figure to line the two back up, so however long this takes, the
        audio and the video still start together.
        """
        clock = threading.Event()
        waited = 0.0
        step = 0.02
        while waited < timeout_s:
            elapsed = self.elapsed_ms
            if elapsed > 0:
                return elapsed
            clock.wait(step)
            waited += step
        log.info("The stretched audio did not start within %.1fs", timeout_s)
        return 0

    def set_volume(self, volume_percent: int) -> None:
        source = self._source
        if source is None:
            return
        try:
            source.volume = max(0.0, int(volume_percent) / 100.0)
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("could not set stretched audio volume", exc_info=True)

    def set_paused(self, paused: bool) -> None:
        source = self._source
        if source is None:
            return
        try:
            source.media_play_pause(bool(paused))
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("could not pause stretched audio", exc_info=True)

    def _set_active(self, active: bool) -> None:
        """Monitored audio is discarded while activate_refs is zero."""
        source = self._source
        pointer = getattr(source, "_ptr", None) if source is not None else None
        if pointer is None or active == self._active:
            return
        try:
            from pylibobs._ffi import get_lib

            lib = get_lib()
            if active:
                lib.obs_source_inc_active(pointer)
            else:
                lib.obs_source_dec_active(pointer)
        except Exception:  # noqa: BLE001 - unwrapped libobs symbol
            log.warning("Could not change stretched audio activation", exc_info=True)
            return
        self._active = active

    def _terminate_process(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=2.0)
        except Exception:  # noqa: BLE001 - teardown must not raise
            try:
                process.kill()
            except Exception:  # noqa: BLE001 - teardown must not raise
                log.debug("could not kill the audio stretch", exc_info=True)

    def stop(self) -> None:
        with self._lock:
            source = self._source
            if source is not None:
                self._set_active(False)
                self._source = None
                for step in ("media_stop", "release"):
                    try:
                        getattr(source, step)()
                    except Exception:  # noqa: BLE001 - teardown must not raise
                        log.debug("stretched audio %s errored", step, exc_info=True)
            self._terminate_process()
