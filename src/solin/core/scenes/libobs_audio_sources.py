"""Audio capture sources (microphone + system audio) for the libobs sidecar.

The recorder and the virtual camera both bind the libobs **main audio mix**
(``context.get_audio()``). Any source placed on a global output channel is summed
into that mix, so this module turns the app's per-recording audio *selection*
(a microphone + a system-audio choice) into libobs capture sources routed onto
their own channels — the recording and the vcam then carry that audio for free.

Two selections are handled, each an :class:`AudioDeviceSelection` reduced to its
wire form ``{"mode", "device_id"}`` (see ``recording.py``):

* **microphone** — an audio *input* device (``pulse_input_capture`` on Linux,
  ``wasapi_input_capture`` on Windows, ``alsa_input_capture`` as a fallback).
* **system_audio** — an audio *output* monitor (``pulse_output_capture`` /
  ``wasapi_output_capture``).

Selection modes: ``system_default`` binds libobs' ``"default"`` device sentinel,
``device`` binds the named ``device_id``, and ``none`` removes the capture. The
sources are created only while a selection asks for them and are torn down when
the selection clears or recording stops, so an unselected microphone is never
left live.

When a selection names a device that is no longer present (or the capture source
cannot be created), :meth:`apply` reports a ``device_unavailable`` warning for
that slot so the sidecar can surface it to the app without interrupting the
recording — mirroring the native engine's ``microphone_warning`` behaviour.

Discovery (:meth:`list_devices`) enumerates the real devices behind each capture
kind via libobs source *properties*; the synthetic ``default`` / ``__custom__``
entries are dropped because ``system_default`` already expresses "the default".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# libobs' sentinel device id for "follow the OS default device".
_DEFAULT_DEVICE_ID = "default"
# Property list entries that are not real, selectable devices.
_SYNTHETIC_DEVICE_IDS = frozenset({"default", "__custom__"})
# Warning surfaced when a selected device is missing or its capture won't start.
_DEVICE_UNAVAILABLE = "device_unavailable"
# Client-side caps mirrored so we never overflow the wire contract.
_MAX_DEVICES = 64
_MAX_DEVICE_ID = 1024
_MAX_DEVICE_NAME = 512


def _sanitize(text: str, maximum: int) -> str:
    """Drop control characters the wire contract rejects, then length-bound.

    The client's ``require_text`` / ``_bounded_text`` reject any character with
    ``ord < 32`` or ``ord == 127``; a real device name can legitimately carry a
    tab or other control char, and one bad name would otherwise fail the *whole*
    device list. Stripping keeps the rest of the list valid.
    """
    cleaned = "".join(ch for ch in text if 32 <= ord(ch) != 127)
    return cleaned[:maximum]


@dataclass(frozen=True)
class _CaptureSpec:
    """A capture direction and the obs source kinds that provide it, best first."""

    direction: str  # "input" | "output"
    kinds: tuple[str, ...]


# Kind preference is platform-ordered: the Windows kind first, then Linux, then
# an ALSA fallback. Only registered kinds are ever used (see _resolve_kind), so
# listing all platforms here is harmless and keeps the module platform-neutral.
_MICROPHONE = _CaptureSpec(
    direction="input",
    kinds=("wasapi_input_capture", "pulse_input_capture", "alsa_input_capture"),
)
_SYSTEM_AUDIO = _CaptureSpec(
    direction="output",
    kinds=("wasapi_output_capture", "pulse_output_capture"),
)


@dataclass
class _SlotState:
    """The live capture source for one selection slot (mic or system audio)."""

    source: Any = None
    channel: int | None = None
    signature: tuple[str, str] | None = None  # (kind, device_id) currently bound


class LibobsAudioMixer:
    """Owns the microphone + system-audio capture sources feeding the main mix."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._microphone = _SlotState()
        self._system_audio = _SlotState()
        self._generation = 0

    # ── discovery ─────────────────────────────────────────────────────────

    def available(self) -> tuple[bool, bool]:
        """(microphone_supported, system_audio_supported) for the capabilities."""
        registered = self._registered_source_types() or set()
        return (
            self._resolve_kind(_MICROPHONE, registered) is not None,
            self._resolve_kind(_SYSTEM_AUDIO, registered) is not None,
        )

    def list_devices(self) -> dict[str, object]:
        """Build the ``audio_device_list`` payload (input + output devices)."""
        registered = self._registered_source_types() or set()
        devices: list[dict[str, object]] = []
        seen: set[tuple[str, str]] = set()
        for spec in (_MICROPHONE, _SYSTEM_AUDIO):
            kind = self._resolve_kind(spec, registered)
            if kind is None:
                continue
            try:
                raw = self._raw_devices(kind)
            except Exception:  # noqa: BLE001 - properties/libobs boundary
                log.warning("could not enumerate %s devices", kind, exc_info=True)
                continue
            for raw_id, raw_name in raw:
                device_id = _sanitize(raw_id, _MAX_DEVICE_ID)
                if not device_id or device_id in _SYNTHETIC_DEVICE_IDS:
                    continue  # the "default"/custom pseudo-entries are not real devices
                key = (spec.direction, device_id)
                if key in seen:
                    continue  # duplicate (direction, device_id) would fail the client
                seen.add(key)
                display_name = _sanitize(raw_name, _MAX_DEVICE_NAME) or device_id
                devices.append(
                    {
                        "device_id": device_id,
                        "display_name": display_name,
                        "direction": spec.direction,
                        "is_default": False,
                    }
                )
        if len(devices) > _MAX_DEVICES:
            log.warning(
                "audio device list truncated from %d to %d", len(devices), _MAX_DEVICES
            )
            devices = devices[:_MAX_DEVICES]
        self._generation += 1
        return {
            "supported": True,
            "ready": True,
            "generation": self._generation,
            "devices": devices,
            "error_code": "",
        }

    def _raw_devices(self, kind: str) -> list[tuple[str, str]]:
        """(device_id, display_name) pairs from a capture kind's device_id list."""
        ob = self._runtime.ob
        properties = ob.Properties.from_source_id(kind)
        if properties is None:
            return []
        out: list[tuple[str, str]] = []
        try:
            prop = properties.get("device_id") or properties.get("device")
            items = list(getattr(prop, "items", None) or []) if prop is not None else []
            for item in items:
                out.append(
                    (
                        str(getattr(item, "value", "") or ""),
                        str(getattr(item, "name", "") or ""),
                    )
                )
        finally:
            try:
                properties.release()
            except Exception:  # noqa: BLE001 - best-effort release
                log.debug("audio properties release errored", exc_info=True)
        return out

    # ── selection ─────────────────────────────────────────────────────────

    def apply(self, microphone: dict | None, system_audio: dict | None) -> dict[str, str]:
        """Reconcile both capture sources; return per-slot warnings ("" if fine).

        Keys ``microphone`` / ``system_audio`` map to ``""`` (bound / removed) or
        ``"device_unavailable"`` (a named device is missing or its capture failed).
        """
        registered = self._registered_source_types()
        return {
            "microphone": self._apply_slot(
                self._microphone, _MICROPHONE, microphone, registered
            ),
            "system_audio": self._apply_slot(
                self._system_audio, _SYSTEM_AUDIO, system_audio, registered
            ),
        }

    def _apply_slot(
        self,
        state: _SlotState,
        spec: _CaptureSpec,
        record: dict | None,
        registered: set[str] | None,
    ) -> str:
        record = record or {}
        mode = str(record.get("mode") or "system_default")
        if mode == "none":
            self._teardown_slot(state)
            return ""
        if mode == "device":
            device_id = str(record.get("device_id") or "") or _DEFAULT_DEVICE_ID
        else:  # system_default (or an unknown mode — fail safe to the default device)
            device_id = _DEFAULT_DEVICE_ID

        if registered is None:
            # Enumeration failed transiently: never tear down a live capture over a
            # momentary hiccup. Keep whatever is bound; treat as "no kinds" only if
            # nothing is bound yet.
            if state.source is not None:
                return ""
            registered = set()
        kind = self._resolve_kind(spec, registered)
        if kind is None:
            self._teardown_slot(state)  # this capture direction is unavailable here
            return _DEVICE_UNAVAILABLE if mode == "device" else ""

        warning = ""
        if mode == "device" and not self._device_exists(kind, device_id):
            warning = _DEVICE_UNAVAILABLE  # selected device is gone; bind anyway (silence)

        signature = (kind, device_id)
        if state.source is not None and state.signature == signature:
            return warning  # already bound to exactly this device — avoid an audio glitch

        self._teardown_slot(state)
        ob = self._runtime.ob
        try:
            source = ob.Source.create(
                kind, f"solin-audio-{spec.direction}", {"device_id": device_id}
            )
        except Exception:  # noqa: BLE001 - source-creation / plugin boundary
            log.warning("could not create %s (%s)", kind, device_id, exc_info=True)
            return _DEVICE_UNAVAILABLE
        if source is None:
            return _DEVICE_UNAVAILABLE

        channel: int | None = None
        try:
            channel = self._runtime.acquire_channel()
            self._runtime.set_channel_source(channel, source)
        except Exception:  # noqa: BLE001 - channel routing boundary
            log.warning("could not route %s onto an output channel", kind, exc_info=True)
            if channel is not None:  # release the reservation we just took
                try:
                    self._runtime.release_channel(channel)
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    log.debug("audio channel release errored", exc_info=True)
            try:
                source.release()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                log.debug("audio source release errored", exc_info=True)
            return _DEVICE_UNAVAILABLE
        state.source = source
        state.channel = channel
        state.signature = signature
        return warning

    def _device_exists(self, kind: str, device_id: str) -> bool:
        if device_id == _DEFAULT_DEVICE_ID:
            return True
        try:
            return any(value == device_id for value, _name in self._raw_devices(kind))
        except Exception:  # noqa: BLE001 - can't verify → assume present, don't false-warn
            log.debug("could not verify device %s for %s", device_id, kind, exc_info=True)
            return True

    def _teardown_slot(self, state: _SlotState) -> None:
        source, channel = state.source, state.channel
        state.source = None
        state.channel = None
        state.signature = None
        if source is None:
            return
        if channel is not None:
            try:
                self._runtime.set_channel_source(channel, None)
            except Exception:  # noqa: BLE001 - channel boundary
                log.debug("audio channel clear errored", exc_info=True)
            try:
                self._runtime.release_channel(channel)
            except Exception:  # noqa: BLE001 - channel boundary
                log.debug("audio channel release errored", exc_info=True)
        try:
            source.release()
        except Exception:  # noqa: BLE001 - libobs boundary
            log.debug("audio source release errored", exc_info=True)

    def clear(self) -> None:
        """Remove both capture sources (e.g. when recording stops)."""
        self._teardown_slot(self._microphone)
        self._teardown_slot(self._system_audio)

    def shutdown(self) -> None:
        self.clear()

    # ── helpers ───────────────────────────────────────────────────────────

    def _registered_source_types(self) -> set[str] | None:
        """Registered obs source kinds, or ``None`` if enumeration failed."""
        try:
            return set(self._runtime.ob.enum_source_types())
        except Exception:  # noqa: BLE001 - enumeration boundary
            log.debug("could not enumerate source types", exc_info=True)
            return None

    @staticmethod
    def _resolve_kind(spec: _CaptureSpec, registered: set[str]) -> str | None:
        for kind in spec.kinds:
            if kind in registered:
                return kind
        return None
