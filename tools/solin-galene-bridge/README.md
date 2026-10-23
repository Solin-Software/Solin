# solin-galene-bridge

Helper process that connects Solin to a [Galene](https://galene.org) SFU (v1.2.x) for
hosted hybrid meetings. Solin supervises it; it logs to **stderr** and reports status as
**JSON lines on stdout**.

Build with `python scripts/build_galene_bridge.py` (see that script's docstring for the
per-OS requirements: cgo, libopus, pkg-config). The binary lands in
`build/hosted-meeting/<platform>/`, where `solin.core.hosted_meeting.binaries` finds it.

## Commands

### `publish` — the hall program over WHIP

```
solin-galene-bridge publish --whip https://HOST/group/NAME/.whip --token JWT \
    [--token-file PATH] [--opus-bitrate 64000] [--opus-loss 10] [--no-fec] [--opus-voip]
```

Reads framed media from stdin (below) and publishes one H.264 video track
(packetization-mode 1, profile-level-id 42e01f) and one Opus stereo 48 kHz track.

- WHIP: `OPTIONS` for ICE servers (`Link` headers, best effort), then `POST`
  (`Content-Type: application/sdp`, `Authorization: Bearer <token>`), expecting
  `201 Created` + `Location`. The resource is `DELETE`d on shutdown.
- RTP timestamps follow the frames' pts; RTCP sender reports (1/s) map them to the
  capture clock so receivers lip-sync audio and video. NACK responder and TWCC are on.
- PCM input is cut into 20 ms frames by sample-count continuity (framing restarts on a
  pts discontinuity above 5 ms) and encoded with libopus: in-band FEC on (unless
  `--no-fec`), packet-loss hint `--opus-loss`.
- After each (re)connect, video is held back until the next IDR access unit; an IDR
  that arrives without SPS/PPS gets the last ones seen prepended.
- Reconnects: a failed WHIP request, a PeerConnection that fails, or one that stays
  disconnected for more than 5 s is torn down and retried with backoff 1 s → 10 s.
  Stdin keeps being drained meanwhile; media is dropped while not connected.
- Stops cleanly (DELETE, exit 0) on SIGINT, SIGTERM or **stdin EOF** — on Windows,
  close stdin to stop it.

stdout events:

```json
{"event":"state","state":"connecting"}      // also connected | reconnecting | failed
{"event":"stats","video_kbps":2480,"audio_kbps":64}   // every 5 s, payload bitrate
{"event":"keyframe_request"}                // a receiver sent PLI/FIR (at most 1/s)
```

### `receive` — remote participants' audio on a hall output

```
solin-galene-bridge receive --ws wss://HOST/ws --group NAME --token JWT \
    [--token-file PATH] [--ignore-user hall-program] [--device ID|NAME] [--gain 1.0] \
    [--channels 2] [--period-ms 10] [--periods 3] [--jitter-ms 40] [--max-delay-ms 200]
```

Joins the group over Galene's WebSocket protocol with the token
(`handshake` v2, `join` with `token`), requests `{"": ["audio"]}`, refuses (`abort`)
offers whose `username` equals `--ignore-user`, and plays every other remote Opus track
on the playback device. PeerConnections use the `rtcConfiguration` (ICE/TURN servers)
from Galene's `joined` message; local ICE candidates are trickled.

- Decoding: libopus at 48 kHz, `--channels` 1 or 2. A single lost packet is recovered
  from the next packet's in-band FEC; up to 5 lost packets are concealed (PLC + FEC);
  late/duplicate packets are dropped.
- Playback: miniaudio, float32, 48 kHz, `--periods` × `--period-ms` device buffer
  (30 ms by default). Each stream has a jitter buffer that starts playing at
  `--jitter-ms`, re-buffers after an underrun and drops its oldest audio above
  `--max-delay-ms`. Concurrent streams are summed and hard-clipped.
- `--device` takes an `id` from `devices` or an exact device name; default is the
  system default device.
- The WebSocket is reconnected with backoff 1 s → 10 s.

stdout events:

```json
{"event":"state","state":"connected"}       // connecting | connected | reconnecting | failed
{"event":"stream","id":"…","user":"Ana Souza","state":"started"}   // or "stopped"
```

### `devices`

Prints the playback devices and exits 0:

```json
[{"id": "616c73615f6f7574…", "name": "Built-in Audio Analog Stereo", "default": true}]
```

The `id` is miniaudio's device ID in hex (PulseAudio/PipeWire sink name, WASAPI endpoint
ID, Core Audio UID), stable across runs.

### `version`

Prints the version (set at build time with `-X main.version=…`).

## Tokens

Galene checks a token only when a client joins, and Solin's join tokens are short-lived.
With `--token`, a refused token is fatal (exit 3) — including on a reconnect after the
token expired — and the supervisor restarts the bridge with a fresh one. With
`--token-file`, the file is re-read on every connection attempt, so the supervisor can
rewrite it instead; `publish` then keeps retrying after a 401/403 (`receive` still exits
3 on a refused join).

## Exit codes

| code | meaning |
|------|---------|
| 0 | clean stop (signal, stdin EOF for `publish`) |
| 1 | fatal error (e.g. corrupt stdin framing) |
| 2 | bad command line |
| 3 | token refused: WHIP 401/403, Galene join failure, WebSocket close 4401/4403 |
| 4 | audio device not found, could not be opened, or stopped (unplugged) |

## stdin framing (`publish`)

A sequence of frames, each a 13-byte header followed by the payload:

| offset | size | field |
|--------|------|-------|
| 0 | 1 | kind |
| 1 | 8 | pts, signed int64 nanoseconds, big endian (capture clock; audio and video share it) |
| 9 | 4 | payload length, uint32 big endian (≤ 32 MiB) |
| 13 | n | payload |

| kind | payload |
|------|---------|
| 0 | one Opus packet (48 kHz), sent as is |
| 1 | one H.264 access unit, Annex-B (start codes), Constrained Baseline/level ≤ 3.1 as negotiated |
| 2 | raw PCM: float32 little endian, interleaved stereo, 48 kHz, any number of frames |

Unknown kinds are ignored (logged once). Send either kind 0 or kind 2 for audio, not both.

## Tests

```
go test -tags nolibopusfile ./...                                  # unit tests
SOLIN_BRIDGE_SMOKE=1 go test -tags nolibopusfile -run Smoke -v ./...  # end to end
```

The smoke test (`smoke_test.go`) starts the Galene in
`build/hosted-meeting/<platform>/galene` (or `$SOLIN_GALENE`) with a token-only group,
publishes an ffmpeg-generated H.264 stream plus a PCM sine, checks a browser-like Pion
client receives both tracks (and that the audio decodes to the sine), runs `receive`
with `--ignore-user` against a second, audio-only publisher, restarts Galene to check
both bridges reconnect, and checks that bad tokens exit with code 3. It needs ffmpeg
with libx264 and a playback device (it plays at `--gain 0`).
