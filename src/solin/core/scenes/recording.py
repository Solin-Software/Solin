from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from solin.core.scenes.model import SceneValidationError


MAXIMUM_AUDIO_DEVICES = 64
MAXIMUM_AUDIO_DEVICE_ID_LENGTH = 1024
MAXIMUM_AUDIO_DEVICE_NAME_LENGTH = 512
MAXIMUM_RECORDING_PATH_LENGTH = 4096
MAXIMUM_RECORDING_DIMENSION = 3840
MAXIMUM_RECORDING_PIXELS = 3840 * 2160
MAXIMUM_RECORDING_FRAMES_PER_SECOND = 60


class AudioSelectionMode(StrEnum):
    SYSTEM_DEFAULT = "system_default"
    NONE = "none"
    DEVICE = "device"


class AudioDeviceDirection(StrEnum):
    INPUT = "input"
    OUTPUT = "output"


@dataclass(frozen=True, slots=True)
class AudioDeviceSelection:
    mode: AudioSelectionMode = AudioSelectionMode.SYSTEM_DEFAULT
    device_id: str = ""
    display_name: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.mode, AudioSelectionMode):
            raise SceneValidationError("Invalid recording audio selection mode")
        _bounded_text(
            self.device_id,
            maximum=MAXIMUM_AUDIO_DEVICE_ID_LENGTH,
            field_name="recording audio device id",
        )
        _bounded_text(
            self.display_name,
            maximum=MAXIMUM_AUDIO_DEVICE_NAME_LENGTH,
            field_name="recording audio device name",
        )
        if self.mode is AudioSelectionMode.DEVICE:
            if not self.device_id or not self.display_name:
                raise SceneValidationError(
                    "An explicit recording audio device requires its id and name"
                )
        elif self.device_id or self.display_name:
            raise SceneValidationError(
                "Default and disabled recording audio selections cannot name a device"
            )

    def to_record(self) -> dict[str, str]:
        return {
            "mode": self.mode.value,
            "device_id": self.device_id,
            "display_name": self.display_name,
        }

    @classmethod
    def from_record(cls, raw: object) -> AudioDeviceSelection:
        data = _strict_mapping(
            raw,
            field_name="recording audio selection",
            allowed_keys={"mode", "device_id", "display_name"},
        )
        try:
            mode = AudioSelectionMode(data.get("mode"))
        except (TypeError, ValueError) as exc:
            raise SceneValidationError("Invalid recording audio selection mode") from exc
        device_id = data.get("device_id")
        display_name = data.get("display_name")
        if not isinstance(device_id, str) or not isinstance(display_name, str):
            raise SceneValidationError("Invalid recording audio selection")
        return cls(mode=mode, device_id=device_id, display_name=display_name)

    def to_engine_record(self) -> dict[str, str]:
        return {"mode": self.mode.value, "device_id": self.device_id}


@dataclass(frozen=True, slots=True)
class SceneRecordingConfig:
    microphone: AudioDeviceSelection = field(default_factory=AudioDeviceSelection)
    system_audio: AudioDeviceSelection = field(default_factory=AudioDeviceSelection)
    output_directory: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.microphone, AudioDeviceSelection):
            raise SceneValidationError("Invalid recording microphone selection")
        if not isinstance(self.system_audio, AudioDeviceSelection):
            raise SceneValidationError("Invalid recording system-audio selection")
        _bounded_text(
            self.output_directory,
            maximum=MAXIMUM_RECORDING_PATH_LENGTH,
            field_name="recording output directory",
        )
        if self.output_directory and not Path(self.output_directory).is_absolute():
            raise SceneValidationError("Recording output directory must be absolute")

    def to_record(self) -> dict[str, object]:
        return {
            "microphone": self.microphone.to_record(),
            "system_audio": self.system_audio.to_record(),
            "output_directory": self.output_directory,
        }

    @classmethod
    def from_record(cls, raw: object) -> SceneRecordingConfig:
        data = _strict_mapping(
            raw,
            field_name="Scene recording configuration",
            allowed_keys={"microphone", "system_audio", "output_directory"},
        )
        output_directory = data.get("output_directory")
        if not isinstance(output_directory, str):
            raise SceneValidationError("Invalid recording output directory")
        return cls(
            microphone=AudioDeviceSelection.from_record(data.get("microphone")),
            system_audio=AudioDeviceSelection.from_record(data.get("system_audio")),
            output_directory=output_directory,
        )


@dataclass(frozen=True, slots=True)
class AudioDevice:
    device_id: str
    display_name: str
    direction: AudioDeviceDirection
    is_default: bool

    def __post_init__(self) -> None:
        _bounded_text(
            self.device_id,
            maximum=MAXIMUM_AUDIO_DEVICE_ID_LENGTH,
            field_name="audio device id",
            allow_empty=False,
        )
        _bounded_text(
            self.display_name,
            maximum=MAXIMUM_AUDIO_DEVICE_NAME_LENGTH,
            field_name="audio device name",
            allow_empty=False,
        )
        if not isinstance(self.direction, AudioDeviceDirection):
            raise ValueError("Invalid audio device direction")
        if type(self.is_default) is not bool:
            raise ValueError("Audio device default state must be a boolean")


@dataclass(frozen=True, slots=True)
class AudioDeviceDiscovery:
    supported: bool
    ready: bool
    generation: int
    devices: tuple[AudioDevice, ...]
    error_code: str = ""

    def __post_init__(self) -> None:
        if type(self.supported) is not bool or type(self.ready) is not bool:
            raise ValueError("Audio device discovery state must use booleans")
        if type(self.generation) is not int or not 0 <= self.generation <= 2**63 - 1:
            raise ValueError("Invalid audio device discovery generation")
        if (
            not isinstance(self.devices, tuple)
            or len(self.devices) > MAXIMUM_AUDIO_DEVICES
            or not all(isinstance(device, AudioDevice) for device in self.devices)
        ):
            raise ValueError("Invalid audio device list")
        identities = tuple((device.direction, device.device_id) for device in self.devices)
        if len(identities) != len(set(identities)):
            raise ValueError("Audio device ids must be unique within each direction")
        _bounded_text(self.error_code, maximum=128, field_name="audio discovery error code")
        if not self.supported and not self.error_code:
            raise ValueError("Unsupported audio discovery requires an error code")
        if not self.ready and (self.devices or self.error_code):
            raise ValueError("Pending audio discovery cannot contain results")


class ProgramRecordingStatus(StrEnum):
    IDLE = "idle"
    STARTING = "starting"
    RECORDING = "recording"
    STOPPING = "stopping"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProgramRecordingState:
    status: ProgramRecordingStatus = ProgramRecordingStatus.IDLE
    started_at_monotonic: float | None = None
    output_path: Path | None = None
    active_config: SceneRecordingConfig | None = None
    error_code: str = ""
    message: str = ""
    microphone_warning: str = ""
    system_audio_warning: str = ""
    dropped_frames: int = 0
    duplicated_frames: int = 0
    frame_feed_p95_ns: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.status, ProgramRecordingStatus):
            raise ValueError("Invalid Program recording status")
        if self.started_at_monotonic is not None and (
            isinstance(self.started_at_monotonic, bool)
            or not isinstance(self.started_at_monotonic, (int, float))
            or self.started_at_monotonic < 0
        ):
            raise ValueError("Invalid Program recording start time")
        if self.output_path is not None and not isinstance(self.output_path, Path):
            raise TypeError("Program recording output path must be a Path")
        if self.active_config is not None and not isinstance(
            self.active_config,
            SceneRecordingConfig,
        ):
            raise TypeError("Invalid active Program recording configuration")
        _bounded_text(self.error_code, maximum=128, field_name="recording error code")
        _bounded_text(self.message, maximum=2048, field_name="recording message")
        _bounded_text(
            self.microphone_warning,
            maximum=128,
            field_name="recording microphone warning",
        )
        _bounded_text(
            self.system_audio_warning,
            maximum=128,
            field_name="recording system-audio warning",
        )
        for value, field_name in (
            (self.dropped_frames, "recording dropped-frame count"),
            (self.duplicated_frames, "recording duplicated-frame count"),
            (self.frame_feed_p95_ns, "recording frame-feed P95"),
        ):
            if type(value) is not int or not 0 <= value <= 2**63 - 1:
                raise ValueError(f"Invalid {field_name}")
        if self.status is ProgramRecordingStatus.FAILED and not self.error_code:
            raise ValueError("A failed Program recording requires an error code")
        if self.status in {
            ProgramRecordingStatus.STARTING,
            ProgramRecordingStatus.RECORDING,
            ProgramRecordingStatus.STOPPING,
        } and (self.output_path is None or self.active_config is None):
            raise ValueError("An active Program recording requires its path and configuration")
        if self.status is ProgramRecordingStatus.RECORDING and self.started_at_monotonic is None:
            raise ValueError("A recording Program output requires its start time")

    @property
    def busy(self) -> bool:
        return self.status in {
            ProgramRecordingStatus.STARTING,
            ProgramRecordingStatus.RECORDING,
            ProgramRecordingStatus.STOPPING,
        }


@dataclass(frozen=True, slots=True)
class ProgramRecordingNativeState:
    status: ProgramRecordingStatus
    path: str
    error_code: str
    message: str
    microphone_warning: str
    system_audio_warning: str
    dropped_frames: int
    duplicated_frames: int
    frame_feed_p95_ns: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.status, ProgramRecordingStatus):
            raise ValueError("Invalid native Program recording status")
        _bounded_text(
            self.path,
            maximum=MAXIMUM_RECORDING_PATH_LENGTH,
            field_name="native recording path",
        )
        if self.path and (
            not Path(self.path).is_absolute()
            or Path(self.path).suffix.casefold() != ".mp4"
        ):
            raise ValueError("Native Program recording path must be an absolute MP4 path")
        _bounded_text(self.error_code, maximum=128, field_name="native recording error code")
        _bounded_text(self.message, maximum=2048, field_name="native recording message")
        _bounded_text(
            self.microphone_warning,
            maximum=128,
            field_name="native recording microphone warning",
        )
        _bounded_text(
            self.system_audio_warning,
            maximum=128,
            field_name="native recording system-audio warning",
        )
        for value, field_name in (
            (self.dropped_frames, "native recording dropped-frame count"),
            (self.duplicated_frames, "native recording duplicated-frame count"),
            (self.frame_feed_p95_ns, "native recording frame-feed P95"),
        ):
            if type(value) is not int or not 0 <= value <= 2**63 - 1:
                raise ValueError(f"Invalid {field_name}")
        if self.status is ProgramRecordingStatus.FAILED and not self.error_code:
            raise ValueError("A failed native recording state requires an error code")


@dataclass(frozen=True, slots=True)
class ProgramRecordingRequest:
    path: Path
    width: int
    height: int
    fps_numerator: int
    fps_denominator: int
    microphone: AudioDeviceSelection
    system_audio: AudioDeviceSelection

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("Program recording path must be absolute")
        if self.path.suffix.casefold() != ".mp4":
            raise ValueError("Program recordings must use an MP4 path")
        _bounded_text(
            str(self.path),
            maximum=MAXIMUM_RECORDING_PATH_LENGTH,
            field_name="Program recording path",
            allow_empty=False,
        )
        for value, minimum, maximum, field_name in (
            (
                self.width,
                320,
                MAXIMUM_RECORDING_DIMENSION,
                "Program recording width",
            ),
            (
                self.height,
                180,
                MAXIMUM_RECORDING_DIMENSION,
                "Program recording height",
            ),
            (self.fps_numerator, 1, 240_000, "Program recording FPS numerator"),
            (self.fps_denominator, 1, 1001, "Program recording FPS denominator"),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"Invalid {field_name}")
        if self.width * self.height > MAXIMUM_RECORDING_PIXELS:
            raise ValueError("Program recording resolution exceeds the encoder budget")
        if not (
            1
            <= self.fps_numerator / self.fps_denominator
            <= MAXIMUM_RECORDING_FRAMES_PER_SECOND
        ):
            raise ValueError("Program recording frame rate must be between 1 and 60 FPS")
        if not isinstance(self.microphone, AudioDeviceSelection) or not isinstance(
            self.system_audio,
            AudioDeviceSelection,
        ):
            raise TypeError("Invalid Program recording audio configuration")


def _strict_mapping(
    raw: object,
    *,
    field_name: str,
    allowed_keys: set[str],
) -> dict[str, Any]:
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise SceneValidationError(f"{field_name} must be an object")
    unknown = set(raw) - allowed_keys
    if unknown:
        raise SceneValidationError(
            f"Unknown {field_name} fields: {', '.join(sorted(unknown))}"
        )
    if set(raw) != allowed_keys:
        raise SceneValidationError(f"Missing {field_name} fields")
    return dict(raw)


def _bounded_text(
    value: object,
    *,
    maximum: int,
    field_name: str,
    allow_empty: bool = True,
) -> None:
    if (
        not isinstance(value, str)
        or (not allow_empty and not value)
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"Invalid {field_name}")
