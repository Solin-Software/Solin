"""S3 hall side: program audio from libobs (hall mic) with optional echo cancellation,
remote attendee audio played on the hall speakers.

Spike code. Logs JSON lines with epoch-ms timestamps so they can be matched against
the remote page (same machine, same clock):
  - remote_rx:   beep onset in the audio received from the remote attendee
  - mic_raw:     beep onset picked up by the hall mic (before echo cancellation)
  - echo_window: beep energy in the program before vs after echo cancellation
"""

from __future__ import annotations

import argparse
import asyncio
import json
import queue
import subprocess
import threading
import time

import numpy as np
from livekit import api, rtc

from pylibobs import OBSContext, OBSData, Scene, Source
import pylibobs as ob
from pylibobs._ffi import get_lib

RATE, CH, BLOCK = 48000, 2, 480   # APM works on 10 ms frames


def now_ms() -> float:
    return time.time() * 1000.0


_WIN = np.hanning(BLOCK)
_FREQS = np.fft.rfftfreq(BLOCK, 1 / RATE)
_BAND = (_FREQS >= 900) & (_FREQS <= 1100)


def band_rms(x: np.ndarray) -> float:
    """RMS of the 900-1100 Hz band (the test beep) in a 10 ms mono block of floats in [-1, 1]."""
    if x.size != BLOCK:
        x = np.resize(x, BLOCK)
    spec = np.fft.rfft(x.astype(np.float64) * _WIN)
    return float(np.sqrt(2 * np.sum(np.abs(spec[_BAND]) ** 2)) / np.sum(_WIN))


class Onset:
    """Rising-edge detector over an adaptive noise floor (threshold = max(min_thr, ratio x floor))."""

    def __init__(self, name: str, log, min_thr: float, ratio: float = 6.0, debounce_ms: float = 1000) -> None:
        self.name, self.log, self.min_thr, self.ratio, self.debounce = name, log, min_thr, ratio, debounce_ms
        self.last = 0.0
        self.floor = min_thr / ratio
        self.peak_1s, self.next_level_log = 0.0, 0.0

    def feed(self, rms: float, t_ms: float, **extra) -> None:
        thr = max(self.min_thr, self.ratio * self.floor)
        if rms > thr and t_ms - self.last > self.debounce:
            self.last = t_ms
            self.log({"event": self.name, "epoch_ms": round(t_ms, 1), "rms": round(rms, 4),
                      "floor": round(self.floor, 5), **extra})
        elif rms < thr:
            self.floor = 0.995 * self.floor + 0.005 * rms
        self.peak_1s = max(self.peak_1s, rms)
        if t_ms >= self.next_level_log:
            self.log({"event": f"{self.name}_level", "epoch_ms": round(t_ms, 1), "peak": round(self.peak_1s, 4),
                      "floor": round(self.floor, 5)})
            self.peak_1s, self.next_level_log = 0.0, t_ms + 1000


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="ws://127.0.0.1:7880")
    p.add_argument("--room", default="s3")
    p.add_argument("--mic", required=True, help="PulseAudio/PipeWire source for the hall mic")
    p.add_argument("--aec", action="store_true", help="run WebRTC echo cancellation on the program audio")
    p.add_argument("--ns", action="store_true", help="also noise suppression")
    p.add_argument("--play-target", default="", help="PipeWire sink for the remote audio (default: system default)")
    p.add_argument("--play-gain", type=float, default=4.0, help="gain applied to remote audio on the hall speakers")
    p.add_argument("--echo-delay-ms", type=float, default=0,
                   help="emulate the speaker->mic/mixer path: mix the played remote audio back into the "
                        "hall feed after this delay (0 = off)")
    p.add_argument("--echo-gain", type=float, default=0.5)
    p.add_argument("--duration", type=float, default=120)
    p.add_argument("--out", default="")
    a = p.parse_args()

    out = open(a.out, "a") if a.out else None
    lock = threading.Lock()

    def log(rec: dict) -> None:
        line = json.dumps({"aec": a.aec, **rec})
        with lock:
            print(line, flush=True)
            if out:
                out.write(line + "\n")
                out.flush()

    apm = rtc.AudioProcessingModule(echo_cancellation=a.aec, noise_suppression=a.ns, high_pass_filter=a.aec)
    room = rtc.Room()
    tok = (api.AccessToken("devkey", "secret").with_identity("hall").with_name("Kingdom Hall")
           .with_grants(api.VideoGrants(room_join=True, room=a.room)).to_jwt())
    await room.connect(a.url, tok)
    source = rtc.AudioSource(RATE, CH, queue_size_ms=200)
    track = rtc.LocalAudioTrack.create_audio_track("program-audio", source)
    await room.local_participant.publish_track(track, rtc.TrackPublishOptions(
        source=rtc.TrackSource.SOURCE_MICROPHONE))

    # --- hall speakers: remote audio -> pw-cat, same frames fed to the APM as the far-end reference
    target = ["--target", a.play_target] if a.play_target else []
    # Emulated speaker->mic path: a FIFO pre-filled with `echo_delay_ms` of silence.
    echo_lock = threading.Lock()
    echo_fifo: list[np.ndarray] = []
    delay_line = np.zeros(int(RATE * a.echo_delay_ms / 1000), dtype=np.float32)
    echo_tail = np.zeros(0, dtype=np.float32)

    def take_echo(n: int) -> np.ndarray:
        nonlocal echo_tail
        with echo_lock:
            while echo_tail.size < n and echo_fifo:
                echo_tail = np.concatenate([echo_tail, echo_fifo.pop(0)])
        out = echo_tail[:n]
        echo_tail = echo_tail[n:]
        return np.pad(out, (0, n - out.size))

    player = subprocess.Popen(["pw-cat", "--playback", *target, "--format", "s16", "--rate", str(RATE), "--channels", "1",
                               "--latency", "20ms", "--raw", "-"], stdin=subprocess.PIPE)
    rx_onset = Onset("remote_rx", log, min_thr=0.005)

    async def play_remote(track: rtc.RemoteAudioTrack) -> None:
        async for ev in rtc.AudioStream(track, sample_rate=RATE, num_channels=1):
            f = ev.frame
            pcm = np.frombuffer(f.data, dtype=np.int16)
            for i in range(0, pcm.size - BLOCK + 1, BLOCK):
                rx_onset.feed(band_rms(pcm[i:i + BLOCK] / 32768.0), now_ms())
            pcm = np.clip(pcm.astype(np.float32) * a.play_gain, -32768, 32767).astype(np.int16)
            with echo_lock:
                echo_fifo.append(pcm.astype(np.float32) / 32768.0)
            player.stdin.write(pcm.tobytes())
            player.stdin.flush()

    room.on("track_subscribed", lambda t, pub, rp: asyncio.ensure_future(play_remote(t))
            if t.kind == rtc.TrackKind.KIND_AUDIO else None)

    # --- program audio: libobs main mix (hall mic) -> 10 ms blocks -> APM -> LiveKit
    ctx = OBSContext(locale="en-US")
    ctx.__enter__()
    ctx.set_video(width=640, height=360, fps_num=30, fps_den=1)
    ctx.set_audio(samples_per_sec=RATE)
    ctx.load_modules()
    scene = Scene.create("program")
    mic = Source.create("pulse_input_capture", "mic", OBSData({"device_id": a.mic}))
    scene.add(mic)
    get_lib().obs_set_output_source(0, scene.as_source()._ptr)

    chunks: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)

    def on_audio(_mix, planes, frames, _ts):
        left = np.frombuffer(planes[0], dtype=np.float32)
        right = np.frombuffer(planes[1], dtype=np.float32) if planes[1] else left
        st = np.empty(frames * 2, dtype=np.float32)
        st[0::2], st[1::2] = left, right
        try:
            chunks.put_nowait(st)
        except queue.Full:
            pass

    cb = ob.add_raw_audio_callback(on_audio, channels=2)
    mic_onset = Onset("mic_raw", log, min_thr=0.002, ratio=8.0)

    async def pump() -> None:
        loop = asyncio.get_running_loop()
        pending = np.zeros(0, dtype=np.float32)
        nonlocal delay_line
        window = None   # [start_ms, raw_energy, out_energy] for 400 ms after a mic onset
        while True:
            try:
                st = await loop.run_in_executor(None, chunks.get, True, 0.5)
            except queue.Empty:
                continue
            pending = np.concatenate([pending, st])
            while pending.size >= BLOCK * CH:
                block, pending = pending[: BLOCK * CH], pending[BLOCK * CH:]
                # Render and capture on the same 10 ms tick, like a sound card: the block "played"
                # now is the far-end reference, and comes back `echo_delay_ms` later in the capture.
                played = take_echo(BLOCK)
                if a.aec:
                    ref = np.clip(played * 32767, -32768, 32767).astype(np.int16)
                    apm.process_reverse_stream(rtc.AudioFrame(ref.tobytes(), RATE, 1, BLOCK))
                if a.echo_delay_ms:
                    delay_line = np.concatenate([delay_line, played])
                    echo, delay_line = a.echo_gain * delay_line[:BLOCK], delay_line[BLOCK:]
                    block = block.copy()
                    block[0::2] += echo
                    block[1::2] += echo
                t = now_ms()
                raw_rms = band_rms(block[0::2])
                mic_onset.feed(raw_rms, t)
                pcm = np.clip(block * 32767, -32768, 32767).astype(np.int16)
                frame = rtc.AudioFrame(pcm.tobytes(), RATE, CH, BLOCK)
                if a.aec:
                    apm.process_stream(frame)
                out_rms = band_rms(np.frombuffer(frame.data, dtype=np.int16)[0::2] / 32768.0)
                if mic_onset.last == t:
                    window = [t, 0.0, 0.0, 0]
                if window is not None:
                    window[1] += raw_rms ** 2
                    window[2] += out_rms ** 2
                    window[3] += 1
                    if t - window[0] >= 400:
                        raw_e, out_e = window[1] / window[3], window[2] / window[3]
                        log({"event": "echo_window", "epoch_ms": round(window[0], 1),
                             "raw_rms": round(raw_e ** 0.5, 4), "out_rms": round(out_e ** 0.5, 5),
                             "attenuation_db": round(10 * np.log10(raw_e / max(out_e, 1e-12)), 1)})
                        window = None
                await source.capture_frame(frame)

    if a.aec:
        apm.set_stream_delay_ms(int(a.echo_delay_ms))
    pumper = asyncio.create_task(pump())
    log({"event": "start", "mic": a.mic, "ns": a.ns, "echo_delay_ms": a.echo_delay_ms, "echo_gain": a.echo_gain})
    await asyncio.sleep(a.duration)
    pumper.cancel()
    ob.remove_raw_audio_callback(cb)
    ctx.__exit__(None, None, None)
    player.stdin.close()
    player.terminate()
    await room.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
