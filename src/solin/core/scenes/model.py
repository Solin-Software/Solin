from __future__ import annotations

import math
import re
import uuid
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from collections.abc import Collection, Iterable
from typing import Any, Final, TypeAlias, TypeVar
from urllib.parse import SplitResult, parse_qsl, urlsplit


SCHEMA_VERSION = 10
MAXIMUM_CAMERA_SOURCE_DIMENSION = 3_840
MAXIMUM_CAMERA_SOURCE_SHORT_EDGE = 2_160
MAXIMUM_CAMERA_SOURCE_PIXELS = 3_840 * 2_160
MAXIMUM_CAMERA_SOURCE_FPS = 60
# GStreamer stores each exact rational component as a signed 32-bit integer.
MAXIMUM_CAMERA_FPS_COMPONENT = 2_147_483_647
MAX_SOURCES = 256
MAX_SCENES = 256
MAX_LAYERS_PER_SCENE = 128
MAX_CAMERA_PRESETS = 512

CONTENT_SOURCE_ID = "solin.content.current"
DEFAULT_CAMERA_SOURCE_ID = "solin.camera.default"
NO_SIGNAL_SOURCE_ID = "solin.source.no-signal"

_IDENTITY_NAMESPACE = uuid.UUID("fb4ab0a2-305c-4dd6-a78e-3ba2e4d19d66")
_IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{8}$")
SENSITIVE_URI_QUERY_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "username",
    "user",
    "api_key",
    "apikey",
    "access_key",
    "auth",
    "authorization",
    "credential",
    "signature",
)
_EnumT = TypeVar("_EnumT", bound=StrEnum)


class SceneDocumentError(ValueError):
    """Base error for an invalid or unsupported persisted scene document."""


class UnsupportedSceneSchemaError(SceneDocumentError):
    def __init__(self, version: int) -> None:
        super().__init__(f"Unsupported scene schema version: {version}")
        self.version = version


class SceneValidationError(SceneDocumentError):
    pass


class SourceKind(StrEnum):
    SOLIN_CONTENT = "solin_content"
    LOCAL_CAMERA = "local_camera"
    RTSP_CAMERA = "rtsp_camera"
    IMAGE = "image"
    COLOR = "color"
    SCENE_REFERENCE = "scene_reference"
    # The year text, rendered by the app in its own styling and shown as an image
    # source. There is a single global year text, so the source carries no config;
    # a layer positions/sizes it like any other. See YeartextSourceConfig.
    YEARTEXT = "yeartext"


class CameraMediaType(StrEnum):
    RAW = "video/x-raw"
    JPEG = "image/jpeg"
    H264 = "video/x-h264"


class FitMode(StrEnum):
    CONTAIN = "contain"
    COVER = "cover"
    STRETCH = "stretch"


class RtspTransport(StrEnum):
    TCP = "tcp"
    UDP = "udp"


class ViscaTransport(StrEnum):
    TCP = "tcp"
    UDP = "udp"


class BusId(StrEnum):
    # MEDIA_WINDOWS is the projection output; VIRTUAL_CAMERA is the program mix
    # (virtual camera + recording). EDITOR is the scenes-editor canvas: a routable
    # channel so the editor never borrows a delivery output's slot, but NOT a
    # delivery route — it is never persisted in a document or runtime row.
    MEDIA_WINDOWS = "media_windows"
    VIRTUAL_CAMERA = "virtual_camera"
    EDITOR = "editor"


# The outputs that actually deliver to an audience, and the only buses a saved
# document or runtime state may describe.
DELIVERY_BUSES: Final = (BusId.MEDIA_WINDOWS, BusId.VIRTUAL_CAMERA)


class OutputMode(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


class TransitionKind(StrEnum):
    CUT = "cut"
    DISSOLVE = "dissolve"
    FADE_TO_BLACK = "fade_to_black"


@dataclass(frozen=True, slots=True)
class TransitionSpec:
    kind: TransitionKind
    duration_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TransitionKind):
            raise SceneValidationError("Invalid transition kind")
        if isinstance(self.duration_ms, bool) or not isinstance(self.duration_ms, int):
            raise SceneValidationError("Transition duration must be an integer")
        if self.kind is TransitionKind.CUT:
            if self.duration_ms != 0:
                raise SceneValidationError("Cut transition duration must be zero")
            return
        if not 50 <= self.duration_ms <= 10_000:
            raise SceneValidationError(
                "Animated transition duration must be between 50 and 10000 milliseconds"
            )

    def to_record(self) -> dict[str, object]:
        return {"kind": self.kind.value, "duration_ms": self.duration_ms}

    @classmethod
    def from_record(cls, raw: object) -> TransitionSpec:
        data = _mapping(
            raw,
            field_name="transition specification",
            allowed_keys={"kind", "duration_ms"},
        )
        return cls(
            kind=_enum(
                TransitionKind,
                data.get("kind"),
                field_name="transition specification.kind",
            ),
            duration_ms=_integer(
                data.get("duration_ms"),
                field_name="transition specification.duration_ms",
                minimum=0,
                maximum=10_000,
            ),
        )


@dataclass(frozen=True, slots=True)
class SceneTransitionPolicy:
    default: TransitionSpec = field(
        default_factory=lambda: TransitionSpec(TransitionKind.CUT, 0)
    )
    overrides: tuple[tuple[str, TransitionSpec], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.default, TransitionSpec):
            raise SceneValidationError("Invalid default transition")
        if not isinstance(self.overrides, tuple) or not all(
            isinstance(override, tuple)
            and len(override) == 2
            and isinstance(override[0], str)
            and isinstance(override[1], TransitionSpec)
            for override in self.overrides
        ):
            raise SceneValidationError(
                "Scene transition overrides must be an immutable transition tuple"
            )
        for scene_id, _ in self.overrides:
            _validate_identity(scene_id, field_name="transition override scene id")
        _ensure_unique(
            (scene_id for scene_id, _ in self.overrides),
            field_name="transition override scene id",
        )

    def override_for(self, scene_id: str) -> TransitionSpec | None:
        return next(
            (spec for candidate_id, spec in self.overrides if candidate_id == scene_id),
            None,
        )

    def effective_for(self, scene_id: str) -> TransitionSpec:
        return self.override_for(scene_id) or self.default

    def with_override(
        self,
        scene_id: str,
        spec: TransitionSpec | None,
    ) -> SceneTransitionPolicy:
        _validate_identity(scene_id, field_name="transition override scene id")
        if spec is not None and not isinstance(spec, TransitionSpec):
            raise SceneValidationError("Invalid scene transition override")
        retained = tuple(
            override for override in self.overrides if override[0] != scene_id
        )
        return replace(
            self,
            overrides=retained if spec is None else (*retained, (scene_id, spec)),
        )

    def to_record(self) -> dict[str, object]:
        return {
            "default": self.default.to_record(),
            "overrides": {
                scene_id: spec.to_record() for scene_id, spec in self.overrides
            },
        }

    @classmethod
    def from_record(cls, raw: object) -> SceneTransitionPolicy:
        data = _mapping(
            raw,
            field_name="scene transition policy",
            allowed_keys={"default", "overrides"},
        )
        overrides = _mapping(
            data.get("overrides", {}),
            field_name="scene transition policy.overrides",
        )
        return cls(
            default=TransitionSpec.from_record(data.get("default")),
            overrides=tuple(
                (
                    _string(
                        scene_id,
                        field_name="transition override scene id",
                        maximum=128,
                    ),
                    TransitionSpec.from_record(spec),
                )
                for scene_id, spec in overrides.items()
            ),
        )


class VideoPixelFormat(StrEnum):
    DYNAMIC = "dynamic"
    BGRA = "bgra"
    NV12 = "nv12"


class VideoColorSpace(StrEnum):
    SRGB = "srgb"
    BT709 = "bt709"


class VideoColorRange(StrEnum):
    FULL = "full"
    LIMITED = "limited"


class ContentCategory(StrEnum):
    IDLE = "idle"
    IMAGE = "image"
    VIDEO = "video"
    TIMER = "timer"
    BROWSER = "browser"
    EXTERNAL_STREAM = "external_stream"
    CAMERA = "camera"


AUTOMATIC_MEDIA_CATEGORIES = (
    ContentCategory.IMAGE,
    ContentCategory.VIDEO,
    ContentCategory.TIMER,
    ContentCategory.BROWSER,
    ContentCategory.EXTERNAL_STREAM,
)


class PtzProtocol(StrEnum):
    ONVIF = "onvif"
    VISCA_IP = "visca_ip"
    VISCA_SERIAL = "visca_serial"


class PtzTimeoutPolicy(StrEnum):
    KEEP_CURRENT = "keep_current"
    TAKE_ANYWAY = "take_anyway"


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_identity() -> str:
    return str(uuid.uuid4())


def stable_identity(seed: str) -> str:
    return str(uuid.uuid5(_IDENTITY_NAMESPACE, seed))


def _validate_identity(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not _IDENTITY_PATTERN.fullmatch(value):
        raise SceneValidationError(f"Invalid {field_name}: {value!r}")


def _validate_name(value: str, *, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 120
        or _contains_control_character(value)
    ):
        raise SceneValidationError(f"Invalid {field_name}")


def _validate_color(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not _COLOR_PATTERN.fullmatch(value):
        raise SceneValidationError(f"Invalid {field_name}: {value!r}")


def _validate_number(
    value: float,
    *,
    field_name: str,
    minimum: float,
    maximum: float,
) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        raise SceneValidationError(f"Invalid {field_name}: {value!r}")


def _mapping(
    value: object,
    *,
    field_name: str,
    allowed_keys: Collection[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SceneValidationError(f"{field_name} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise SceneValidationError(f"{field_name} keys must be strings")
    if allowed_keys is not None:
        unknown = set(value) - set(allowed_keys)
        if unknown:
            fields = ", ".join(sorted(unknown))
            raise SceneValidationError(f"Unknown {field_name} fields: {fields}")
    return dict(value)


def _validate_timestamp(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or len(value) > 64:
        raise SceneValidationError(f"Invalid {field_name}")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SceneValidationError(f"Invalid {field_name}") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise SceneValidationError(f"{field_name} must include a UTC offset")


def _contains_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _reject_sensitive_uri_query(uri: str, *, field_name: str) -> None:
    for key, _ in parse_qsl(urlsplit(uri).query, keep_blank_values=True):
        normalized = key.casefold().replace("-", "_")
        if any(fragment in normalized for fragment in SENSITIVE_URI_QUERY_FRAGMENTS):
            raise SceneValidationError(
                f"{field_name} credentials must use a secure credential reference"
            )


def _validated_network_endpoint(
    uri: str,
    *,
    schemes: Collection[str],
    field_name: str,
    scheme_error: str,
) -> SplitResult:
    try:
        parsed = urlsplit(uri)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise SceneValidationError(f"Invalid {field_name}") from exc
    if parsed.scheme not in schemes or not hostname:
        raise SceneValidationError(scheme_error)
    if parsed.username is not None or parsed.password is not None:
        raise SceneValidationError(
            f"{field_name} credentials must use a secure credential reference"
        )
    if parsed.netloc.endswith(":") or port == 0:
        raise SceneValidationError(f"Invalid {field_name} port")
    _reject_sensitive_uri_query(uri, field_name=field_name)
    return parsed


def _sequence(value: object, *, field_name: str) -> list[object]:
    if not isinstance(value, list):
        raise SceneValidationError(f"{field_name} must be an array")
    return list(value)


def _string(
    value: object,
    *,
    field_name: str,
    allow_empty: bool = False,
    maximum: int = 2048,
) -> str:
    if not isinstance(value, str):
        raise SceneValidationError(f"{field_name} must be a string")
    if (
        len(value) > maximum
        or _contains_control_character(value)
        or (not allow_empty and not value.strip())
    ):
        raise SceneValidationError(f"Invalid {field_name}")
    return value


def _integer(
    value: object,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SceneValidationError(f"{field_name} must be an integer")
    if not minimum <= value <= maximum:
        raise SceneValidationError(f"Invalid {field_name}: {value!r}")
    return value


def _number(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SceneValidationError(f"{field_name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise SceneValidationError(f"Invalid {field_name}: {value!r}")
    return number


def _boolean(value: object, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise SceneValidationError(f"{field_name} must be a boolean")
    return value


def _optional_string(
    value: object,
    *,
    field_name: str,
    maximum: int = 2048,
) -> str:
    return _string(
        "" if value is None else value,
        field_name=field_name,
        allow_empty=True,
        maximum=maximum,
    )


def _enum(
    enum_type: type[_EnumT],
    value: object,
    *,
    field_name: str,
) -> _EnumT:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise SceneValidationError(f"Invalid {field_name}: {value!r}") from exc


@dataclass(frozen=True, slots=True)
class VideoFormat:
    width: int = 1920
    height: int = 1080
    fps_numerator: int = 60
    fps_denominator: int = 1
    pixel_format: VideoPixelFormat = VideoPixelFormat.BGRA
    color_space: VideoColorSpace = VideoColorSpace.SRGB
    color_range: VideoColorRange = VideoColorRange.FULL

    def __post_init__(self) -> None:
        for name, value, minimum, maximum in (
            ("width", self.width, 320, 7680),
            ("height", self.height, 180, 4320),
            ("fps_numerator", self.fps_numerator, 1, 240_000),
            ("fps_denominator", self.fps_denominator, 1, 1001),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise SceneValidationError(f"Invalid video format {name}")
        frames_per_second = self.fps_numerator / self.fps_denominator
        if not 1.0 <= frames_per_second <= 240.0:
            raise SceneValidationError("Video frame rate must be between 1 and 240 fps")
        if (
            not isinstance(self.pixel_format, VideoPixelFormat)
            or self.pixel_format is VideoPixelFormat.DYNAMIC
        ):
            raise SceneValidationError("Invalid video pixel format")
        if not isinstance(self.color_space, VideoColorSpace):
            raise SceneValidationError("Invalid video color space")
        if not isinstance(self.color_range, VideoColorRange):
            raise SceneValidationError("Invalid video color range")

    def to_record(self) -> dict[str, object]:
        return {
            "width": self.width,
            "height": self.height,
            "fps_numerator": self.fps_numerator,
            "fps_denominator": self.fps_denominator,
            "pixel_format": self.pixel_format.value,
            "color_space": self.color_space.value,
            "color_range": self.color_range.value,
        }

    @classmethod
    def from_record(cls, raw: object) -> VideoFormat:
        data = _mapping(
            raw,
            field_name="video format",
            allowed_keys={
                "width",
                "height",
                "fps_numerator",
                "fps_denominator",
                "pixel_format",
                "color_space",
                "color_range",
            },
        )
        return cls(
            width=_integer(
                data.get("width"),
                field_name="video format.width",
                minimum=320,
                maximum=7680,
            ),
            height=_integer(
                data.get("height"),
                field_name="video format.height",
                minimum=180,
                maximum=4320,
            ),
            fps_numerator=_integer(
                data.get("fps_numerator"),
                field_name="video format.fps_numerator",
                minimum=1,
                maximum=240_000,
            ),
            fps_denominator=_integer(
                data.get("fps_denominator"),
                field_name="video format.fps_denominator",
                minimum=1,
                maximum=1001,
            ),
            pixel_format=_enum(
                VideoPixelFormat,
                data.get("pixel_format"),
                field_name="video format.pixel_format",
            ),
            color_space=_enum(
                VideoColorSpace,
                data.get("color_space"),
                field_name="video format.color_space",
            ),
            color_range=_enum(
                VideoColorRange,
                data.get("color_range"),
                field_name="video format.color_range",
            ),
        )


@dataclass(frozen=True, slots=True)
class SolinContentConfig:
    def to_record(self) -> dict[str, object]:
        return {}

    @classmethod
    def from_record(cls, raw: object) -> SolinContentConfig:
        _mapping(raw, field_name="source.configuration", allowed_keys=set())
        return cls()


@dataclass(frozen=True, slots=True)
class OnvifPtzBinding:
    endpoint: str
    profile_token: str = ""
    credential_ref: str = ""
    protocol: PtzProtocol = field(default=PtzProtocol.ONVIF, init=False)

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str)
            for value in (self.endpoint, self.profile_token, self.credential_ref)
        ):
            raise SceneValidationError("ONVIF binding values must be strings")
        if any(
            _contains_control_character(value)
            for value in (self.endpoint, self.profile_token, self.credential_ref)
        ):
            raise SceneValidationError("ONVIF binding values contain control characters")
        _validated_network_endpoint(
            self.endpoint,
            schemes={"http", "https"},
            field_name="ONVIF endpoint",
            scheme_error="ONVIF endpoint must use http:// or https://",
        )
        if len(self.endpoint) > 2048 or len(self.profile_token) > 512:
            raise SceneValidationError("ONVIF binding value is too long")
        if len(self.credential_ref) > 256:
            raise SceneValidationError("ONVIF credential reference is too long")

    def to_record(self) -> dict[str, object]:
        return {
            "protocol": self.protocol.value,
            "endpoint": self.endpoint,
            "profile_token": self.profile_token,
            "credential_ref": self.credential_ref,
        }


@dataclass(frozen=True, slots=True)
class ViscaIpPtzBinding:
    host: str
    port: int = 52381
    transport: ViscaTransport = ViscaTransport.UDP
    protocol: PtzProtocol = field(default=PtzProtocol.VISCA_IP, init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.host, str)
            or not self.host.strip()
            or len(self.host) > 253
            or _contains_control_character(self.host)
        ):
            raise SceneValidationError("Invalid VISCA IP host")
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 1 <= self.port <= 65_535
        ):
            raise SceneValidationError("Invalid VISCA IP port")
        if not isinstance(self.transport, ViscaTransport):
            raise SceneValidationError("Invalid VISCA IP transport")

    def to_record(self) -> dict[str, object]:
        return {
            "protocol": self.protocol.value,
            "host": self.host,
            "port": self.port,
            "transport": self.transport.value,
        }


@dataclass(frozen=True, slots=True)
class ViscaSerialPtzBinding:
    device_id: str
    baud_rate: int = 9600
    camera_address: int = 1
    protocol: PtzProtocol = field(default=PtzProtocol.VISCA_SERIAL, init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.device_id, str)
            or not self.device_id.strip()
            or len(self.device_id) > 1024
            or _contains_control_character(self.device_id)
        ):
            raise SceneValidationError("Invalid VISCA serial device")
        if (
            isinstance(self.baud_rate, bool)
            or not isinstance(self.baud_rate, int)
            or self.baud_rate not in {9600, 19200, 38400, 115200}
        ):
            raise SceneValidationError("Unsupported VISCA serial baud rate")
        if (
            isinstance(self.camera_address, bool)
            or not isinstance(self.camera_address, int)
            or not 1 <= self.camera_address <= 7
        ):
            raise SceneValidationError("Invalid VISCA camera address")

    def to_record(self) -> dict[str, object]:
        return {
            "protocol": self.protocol.value,
            "device_id": self.device_id,
            "baud_rate": self.baud_rate,
            "camera_address": self.camera_address,
        }


PtzBinding: TypeAlias = OnvifPtzBinding | ViscaIpPtzBinding | ViscaSerialPtzBinding


def _ptz_binding_from_record(raw: object) -> PtzBinding:
    data = _mapping(raw, field_name="ptz binding")
    protocol = _enum(PtzProtocol, data.get("protocol"), field_name="ptz binding.protocol")
    if protocol is PtzProtocol.ONVIF:
        strict = _mapping(
            data,
            field_name="ptz binding",
            allowed_keys={"protocol", "endpoint", "profile_token", "credential_ref"},
        )
        return OnvifPtzBinding(
            endpoint=_string(strict.get("endpoint"), field_name="ptz binding.endpoint"),
            profile_token=_optional_string(
                strict.get("profile_token", ""),
                field_name="ptz binding.profile_token",
                maximum=512,
            ),
            credential_ref=_optional_string(
                strict.get("credential_ref", ""),
                field_name="ptz binding.credential_ref",
                maximum=256,
            ),
        )
    if protocol is PtzProtocol.VISCA_IP:
        strict = _mapping(
            data,
            field_name="ptz binding",
            allowed_keys={"protocol", "host", "port", "transport"},
        )
        return ViscaIpPtzBinding(
            host=_string(
                strict.get("host"),
                field_name="ptz binding.host",
                maximum=253,
            ),
            port=_integer(
                strict.get("port", 52381),
                field_name="ptz binding.port",
                minimum=1,
                maximum=65_535,
            ),
            transport=_enum(
                ViscaTransport,
                strict.get("transport", ViscaTransport.UDP.value),
                field_name="ptz binding.transport",
            ),
        )
    strict = _mapping(
        data,
        field_name="ptz binding",
        allowed_keys={"protocol", "device_id", "baud_rate", "camera_address"},
    )
    return ViscaSerialPtzBinding(
        device_id=_string(
            strict.get("device_id"),
            field_name="ptz binding.device_id",
            maximum=1024,
        ),
        baud_rate=_integer(
            strict.get("baud_rate", 9600),
            field_name="ptz binding.baud_rate",
            minimum=9600,
            maximum=115200,
        ),
        camera_address=_integer(
            strict.get("camera_address", 1),
            field_name="ptz binding.camera_address",
            minimum=1,
            maximum=7,
        ),
    )


@dataclass(frozen=True, slots=True)
class LocalCameraConfig:
    device_id: str = ""
    width: int = 0
    height: int = 0
    fps_numerator: int = 0
    fps_denominator: int = 1
    media_type: CameraMediaType | None = None
    pixel_format: str = ""
    ptz_binding: PtzBinding | None = None
    keep_active: bool = True

    def __post_init__(self) -> None:
        if (
            not isinstance(self.device_id, str)
            or not isinstance(self.pixel_format, str)
            or _contains_control_character(self.device_id)
            or _contains_control_character(self.pixel_format)
            or len(self.device_id) > 1024
            or len(self.pixel_format) > 80
        ):
            raise SceneValidationError("Invalid local camera configuration")
        if self.media_type is not None and not isinstance(self.media_type, CameraMediaType):
            raise SceneValidationError("Invalid local camera media type")
        if (self.media_type is None) != (self.pixel_format == ""):
            raise SceneValidationError("Camera media type and pixel format must both be automatic")
        if self.ptz_binding is not None and not isinstance(
            self.ptz_binding,
            (OnvifPtzBinding, ViscaIpPtzBinding, ViscaSerialPtzBinding),
        ):
            raise SceneValidationError("Invalid local camera PTZ binding")
        if not isinstance(self.keep_active, bool):
            raise SceneValidationError("Invalid local camera keep-active state")
        for name, value, maximum in (
            ("width", self.width, MAXIMUM_CAMERA_SOURCE_DIMENSION),
            ("height", self.height, MAXIMUM_CAMERA_SOURCE_DIMENSION),
            ("fps numerator", self.fps_numerator, MAXIMUM_CAMERA_FPS_COMPONENT),
            ("fps denominator", self.fps_denominator, MAXIMUM_CAMERA_FPS_COMPONENT),
        ):
            minimum = 1 if name == "fps denominator" else 0
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not minimum <= value <= maximum
            ):
                raise SceneValidationError(f"Invalid local camera {name}")
        dimensions = (self.width, self.height)
        if (dimensions[0] == 0) != (dimensions[1] == 0):
            raise SceneValidationError("Camera width and height must both be automatic or fixed")
        if (
            self.width * self.height > MAXIMUM_CAMERA_SOURCE_PIXELS
            or min(self.width, self.height) > MAXIMUM_CAMERA_SOURCE_SHORT_EDGE
        ):
            raise SceneValidationError("Local camera frame dimensions exceed the media budget")
        if self.fps_numerator == 0:
            if self.fps_denominator != 1:
                raise SceneValidationError("Automatic camera frame rate must use denominator 1")
        elif (
            self.fps_numerator > MAXIMUM_CAMERA_SOURCE_FPS * self.fps_denominator
            or math.gcd(self.fps_numerator, self.fps_denominator) != 1
        ):
            raise SceneValidationError("Invalid local camera frame rate")
        automatic_format = (
            self.width == 0
            and self.fps_numerator == 0
            and self.media_type is None
            and self.pixel_format == ""
        )
        exact_format = (
            self.width > 0
            and self.fps_numerator > 0
            and self.media_type is not None
            and self.pixel_format != ""
        )
        if not automatic_format and not exact_format:
            raise SceneValidationError("Camera format must be fully automatic or exact")

    def to_record(self) -> dict[str, object]:
        return {
            "device_id": self.device_id,
            "width": self.width,
            "height": self.height,
            "fps_numerator": self.fps_numerator,
            "fps_denominator": self.fps_denominator,
            "media_type": self.media_type.value if self.media_type is not None else "",
            "pixel_format": self.pixel_format,
            "ptz_binding": (self.ptz_binding.to_record() if self.ptz_binding is not None else None),
            "keep_active": self.keep_active,
        }

    @classmethod
    def from_record(cls, raw: object) -> LocalCameraConfig:
        data = _mapping(
            raw,
            field_name="source.configuration",
            allowed_keys={
                "device_id",
                "width",
                "height",
                "fps_numerator",
                "fps_denominator",
                "media_type",
                "pixel_format",
                "ptz_binding",
                "keep_active",
            },
        )
        return cls(
            device_id=_optional_string(
                data.get("device_id", ""),
                field_name="source.configuration.device_id",
                maximum=1024,
            ),
            width=_integer(
                data.get("width", 0),
                field_name="source.configuration.width",
                minimum=0,
                maximum=MAXIMUM_CAMERA_SOURCE_DIMENSION,
            ),
            height=_integer(
                data.get("height", 0),
                field_name="source.configuration.height",
                minimum=0,
                maximum=MAXIMUM_CAMERA_SOURCE_DIMENSION,
            ),
            fps_numerator=_integer(
                data.get("fps_numerator", 0),
                field_name="source.configuration.fps_numerator",
                minimum=0,
                maximum=MAXIMUM_CAMERA_FPS_COMPONENT,
            ),
            fps_denominator=_integer(
                data.get("fps_denominator", 1),
                field_name="source.configuration.fps_denominator",
                minimum=1,
                maximum=MAXIMUM_CAMERA_FPS_COMPONENT,
            ),
            media_type=(
                None
                if data.get("media_type", "") == ""
                else _enum(
                    CameraMediaType,
                    data.get("media_type"),
                    field_name="source.configuration.media_type",
                )
            ),
            pixel_format=_optional_string(
                data.get("pixel_format", ""),
                field_name="source.configuration.pixel_format",
                maximum=80,
            ),
            ptz_binding=(
                None
                if data.get("ptz_binding") is None
                else _ptz_binding_from_record(data["ptz_binding"])
            ),
            keep_active=_boolean(
                data.get("keep_active", True),
                field_name="source.configuration.keep_active",
            ),
        )


@dataclass(frozen=True, slots=True)
class RtspCameraConfig:
    uri: str
    transport: RtspTransport = RtspTransport.TCP
    latency_ms: int = 200
    ptz_binding: PtzBinding | None = None
    keep_active: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.uri, str):
            raise SceneValidationError("Camera endpoints must be strings")
        if _contains_control_character(self.uri):
            raise SceneValidationError("Camera endpoints contain control characters")
        if not isinstance(self.transport, RtspTransport):
            raise SceneValidationError("Invalid RTSP transport")
        if isinstance(self.latency_ms, bool) or not isinstance(self.latency_ms, int):
            raise SceneValidationError("Invalid RTSP latency")
        _validated_network_endpoint(
            self.uri,
            schemes={"rtsp", "rtsps"},
            field_name="RTSP URI",
            scheme_error="RTSP camera URI must use rtsp:// or rtsps://",
        )
        if len(self.uri) > 2048:
            raise SceneValidationError("Camera endpoint is too long")
        if self.ptz_binding is not None and not isinstance(
            self.ptz_binding,
            (OnvifPtzBinding, ViscaIpPtzBinding, ViscaSerialPtzBinding),
        ):
            raise SceneValidationError("Invalid RTSP camera PTZ binding")
        if not isinstance(self.keep_active, bool):
            raise SceneValidationError("Invalid RTSP camera keep-active state")
        if not 0 <= self.latency_ms <= 10_000:
            raise SceneValidationError("Invalid RTSP latency")

    def to_record(self) -> dict[str, object]:
        return {
            "uri": self.uri,
            "transport": self.transport.value,
            "latency_ms": self.latency_ms,
            "ptz_binding": (self.ptz_binding.to_record() if self.ptz_binding is not None else None),
            "keep_active": self.keep_active,
        }

    @classmethod
    def from_record(cls, raw: object) -> RtspCameraConfig:
        data = _mapping(
            raw,
            field_name="source.configuration",
            allowed_keys={
                "uri",
                "transport",
                "latency_ms",
                "ptz_binding",
                "keep_active",
            },
        )
        return cls(
            uri=_string(data.get("uri"), field_name="source.configuration.uri"),
            transport=_enum(
                RtspTransport,
                data.get("transport", RtspTransport.TCP.value),
                field_name="source.configuration.transport",
            ),
            latency_ms=_integer(
                data.get("latency_ms", 200),
                field_name="source.configuration.latency_ms",
                minimum=0,
                maximum=10_000,
            ),
            ptz_binding=(
                None
                if data.get("ptz_binding") is None
                else _ptz_binding_from_record(data["ptz_binding"])
            ),
            keep_active=_boolean(
                data.get("keep_active", True),
                field_name="source.configuration.keep_active",
            ),
        )


@dataclass(frozen=True, slots=True)
class ImageSourceConfig:
    asset_id: str

    def __post_init__(self) -> None:
        _validate_identity(self.asset_id, field_name="image asset id")

    def to_record(self) -> dict[str, object]:
        return {"asset_id": self.asset_id}

    @classmethod
    def from_record(cls, raw: object) -> ImageSourceConfig:
        data = _mapping(
            raw,
            field_name="source.configuration",
            allowed_keys={"asset_id"},
        )
        return cls(
            asset_id=_string(
                data.get("asset_id"),
                field_name="source.configuration.asset_id",
                maximum=128,
            )
        )


@dataclass(frozen=True, slots=True)
class YeartextSourceConfig:
    """Configuration for a year-text source.

    The year text is a single global value rendered by the app in its own
    styling and surfaced as an image; a layer positions/sizes it. There is
    nothing per-source to configure, so the record is empty."""

    def to_record(self) -> dict[str, object]:
        return {}

    @classmethod
    def from_record(cls, raw: object) -> "YeartextSourceConfig":
        _mapping(raw, field_name="source.configuration", allowed_keys=set())
        return cls()


@dataclass(frozen=True, slots=True)
class ColorSourceConfig:
    color: str = "#000000FF"

    def __post_init__(self) -> None:
        _validate_color(self.color, field_name="source.configuration.color")

    def to_record(self) -> dict[str, object]:
        return {"color": self.color.upper()}

    @classmethod
    def from_record(cls, raw: object) -> ColorSourceConfig:
        data = _mapping(
            raw,
            field_name="source.configuration",
            allowed_keys={"color"},
        )
        return cls(
            color=_string(
                data.get("color"),
                field_name="source.configuration.color",
                maximum=9,
            ).upper()
        )


@dataclass(frozen=True, slots=True)
class SceneReferenceConfig:
    target_scene_id: str

    def __post_init__(self) -> None:
        _validate_identity(self.target_scene_id, field_name="referenced scene id")

    def to_record(self) -> dict[str, object]:
        return {"target_scene_id": self.target_scene_id}

    @classmethod
    def from_record(cls, raw: object) -> SceneReferenceConfig:
        data = _mapping(
            raw,
            field_name="source.configuration",
            allowed_keys={"target_scene_id"},
        )
        return cls(
            target_scene_id=_string(
                data.get("target_scene_id"),
                field_name="source.configuration.target_scene_id",
                maximum=128,
            )
        )


SourceConfig: TypeAlias = (
    SolinContentConfig
    | LocalCameraConfig
    | RtspCameraConfig
    | ImageSourceConfig
    | ColorSourceConfig
    | SceneReferenceConfig
    | YeartextSourceConfig
)

_CONFIG_BY_SOURCE_KIND = {
    SourceKind.SOLIN_CONTENT: SolinContentConfig,
    SourceKind.LOCAL_CAMERA: LocalCameraConfig,
    SourceKind.RTSP_CAMERA: RtspCameraConfig,
    SourceKind.IMAGE: ImageSourceConfig,
    SourceKind.COLOR: ColorSourceConfig,
    SourceKind.SCENE_REFERENCE: SceneReferenceConfig,
    SourceKind.YEARTEXT: YeartextSourceConfig,
}


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    id: str
    kind: SourceKind
    name: str
    configuration: SourceConfig
    enabled: bool = True
    credential_ref: str = ""

    def __post_init__(self) -> None:
        _validate_identity(self.id, field_name="source id")
        _validate_name(self.name, field_name="source name")
        if not isinstance(self.kind, SourceKind):
            raise SceneValidationError("Invalid source type")
        expected = _CONFIG_BY_SOURCE_KIND[self.kind]
        if not isinstance(self.configuration, expected):
            raise SceneValidationError(
                f"Configuration for {self.kind.value} must be {expected.__name__}"
            )
        if not isinstance(self.enabled, bool):
            raise SceneValidationError("Source enabled must be a boolean")
        if (
            not isinstance(self.credential_ref, str)
            or len(self.credential_ref) > 256
            or _contains_control_character(self.credential_ref)
        ):
            raise SceneValidationError("Invalid source credential reference")

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "type": self.kind.value,
            "name": self.name,
            "enabled": self.enabled,
            "configuration": self.configuration.to_record(),
            "credential_ref": self.credential_ref,
        }

    @classmethod
    def from_record(cls, raw: object) -> SourceDefinition:
        data = _mapping(
            raw,
            field_name="source",
            allowed_keys={
                "id",
                "type",
                "name",
                "enabled",
                "configuration",
                "credential_ref",
            },
        )
        kind = _enum(SourceKind, data.get("type"), field_name="source.type")
        config_type = _CONFIG_BY_SOURCE_KIND[kind]
        return cls(
            id=_string(data.get("id"), field_name="source.id", maximum=128),
            kind=kind,
            name=_string(data.get("name"), field_name="source.name", maximum=120),
            enabled=_boolean(data.get("enabled", True), field_name="source.enabled"),
            configuration=config_type.from_record(data.get("configuration", {})),
            credential_ref=_optional_string(
                data.get("credential_ref", ""),
                field_name="source.credential_ref",
                maximum=256,
            ),
        )


@dataclass(frozen=True, slots=True)
class NormalizedRect:
    x: float = 0.0
    y: float = 0.0
    width: float = 1.0
    height: float = 1.0

    def __post_init__(self) -> None:
        _validate_number(self.x, field_name="rect.x", minimum=-2.0, maximum=2.0)
        _validate_number(self.y, field_name="rect.y", minimum=-2.0, maximum=2.0)
        _validate_number(self.width, field_name="rect.width", minimum=0.001, maximum=4.0)
        _validate_number(self.height, field_name="rect.height", minimum=0.001, maximum=4.0)

    def to_record(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}

    @classmethod
    def from_record(cls, raw: object) -> NormalizedRect:
        data = _mapping(
            raw,
            field_name="layer.rect",
            allowed_keys={"x", "y", "width", "height"},
        )
        return cls(
            x=_number(data.get("x"), field_name="layer.rect.x"),
            y=_number(data.get("y"), field_name="layer.rect.y"),
            width=_number(data.get("width"), field_name="layer.rect.width"),
            height=_number(data.get("height"), field_name="layer.rect.height"),
        )


@dataclass(frozen=True, slots=True)
class Crop:
    left: float = 0.0
    top: float = 0.0
    right: float = 0.0
    bottom: float = 0.0

    def __post_init__(self) -> None:
        for name, value in (
            ("left", self.left),
            ("top", self.top),
            ("right", self.right),
            ("bottom", self.bottom),
        ):
            _validate_number(value, field_name=f"crop.{name}", minimum=0.0, maximum=0.99)
        if self.left + self.right >= 1.0 or self.top + self.bottom >= 1.0:
            raise SceneValidationError("Crop must leave a visible area")

    def to_record(self) -> dict[str, float]:
        return {
            "left": self.left,
            "top": self.top,
            "right": self.right,
            "bottom": self.bottom,
        }

    @classmethod
    def from_record(cls, raw: object) -> Crop:
        data = _mapping(
            raw,
            field_name="layer.crop",
            allowed_keys={"left", "top", "right", "bottom"},
        )
        return cls(
            left=_number(data.get("left", 0.0), field_name="layer.crop.left"),
            top=_number(data.get("top", 0.0), field_name="layer.crop.top"),
            right=_number(data.get("right", 0.0), field_name="layer.crop.right"),
            bottom=_number(data.get("bottom", 0.0), field_name="layer.crop.bottom"),
        )


@dataclass(frozen=True, slots=True)
class SceneLayer:
    id: str
    source_id: str
    name: str
    rect: NormalizedRect = field(default_factory=NormalizedRect)
    crop: Crop = field(default_factory=Crop)
    rotation_degrees: float = 0.0
    fit_mode: FitMode = FitMode.COVER
    opacity: float = 1.0
    visible: bool = True
    locked: bool = False
    mirror_x: bool = False
    mirror_y: bool = False
    border_color: str = "#00000000"
    border_width: float = 0.0
    corner_radius: float = 0.0

    def __post_init__(self) -> None:
        _validate_identity(self.id, field_name="layer id")
        _validate_identity(self.source_id, field_name="layer source id")
        _validate_name(self.name, field_name="layer name")
        if not isinstance(self.rect, NormalizedRect) or not isinstance(self.crop, Crop):
            raise SceneValidationError("Invalid layer geometry")
        if not isinstance(self.fit_mode, FitMode):
            raise SceneValidationError("Invalid layer fit mode")
        for name, value in (
            ("visible", self.visible),
            ("locked", self.locked),
            ("mirror_x", self.mirror_x),
            ("mirror_y", self.mirror_y),
        ):
            if not isinstance(value, bool):
                raise SceneValidationError(f"Layer {name} must be a boolean")
        _validate_number(
            self.rotation_degrees,
            field_name="layer rotation",
            minimum=-360.0,
            maximum=360.0,
        )
        _validate_number(self.opacity, field_name="layer opacity", minimum=0.0, maximum=1.0)
        _validate_color(self.border_color, field_name="layer border color")
        _validate_number(
            self.border_width,
            field_name="layer border width",
            minimum=0.0,
            maximum=0.1,
        )
        _validate_number(
            self.corner_radius,
            field_name="layer corner radius",
            minimum=0.0,
            maximum=0.5,
        )

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "name": self.name,
            "rect": self.rect.to_record(),
            "crop": self.crop.to_record(),
            "rotation_degrees": self.rotation_degrees,
            "fit_mode": self.fit_mode.value,
            "opacity": self.opacity,
            "visible": self.visible,
            "locked": self.locked,
            "mirror_x": self.mirror_x,
            "mirror_y": self.mirror_y,
            "border_color": self.border_color.upper(),
            "border_width": self.border_width,
            "corner_radius": self.corner_radius,
        }

    @classmethod
    def from_record(cls, raw: object) -> SceneLayer:
        data = _mapping(
            raw,
            field_name="layer",
            allowed_keys={
                "id",
                "source_id",
                "name",
                "rect",
                "crop",
                "rotation_degrees",
                "fit_mode",
                "opacity",
                "visible",
                "locked",
                "mirror_x",
                "mirror_y",
                "border_color",
                "border_width",
                "corner_radius",
            },
        )
        return cls(
            id=_string(data.get("id"), field_name="layer.id", maximum=128),
            source_id=_string(
                data.get("source_id"),
                field_name="layer.source_id",
                maximum=128,
            ),
            name=_string(data.get("name"), field_name="layer.name", maximum=120),
            rect=NormalizedRect.from_record(data.get("rect")),
            crop=Crop.from_record(data.get("crop", {})),
            rotation_degrees=_number(
                data.get("rotation_degrees", 0.0),
                field_name="layer.rotation_degrees",
            ),
            fit_mode=_enum(
                FitMode,
                data.get("fit_mode", FitMode.COVER.value),
                field_name="layer.fit_mode",
            ),
            opacity=_number(data.get("opacity", 1.0), field_name="layer.opacity"),
            visible=_boolean(data.get("visible", True), field_name="layer.visible"),
            locked=_boolean(data.get("locked", False), field_name="layer.locked"),
            mirror_x=_boolean(data.get("mirror_x", False), field_name="layer.mirror_x"),
            mirror_y=_boolean(data.get("mirror_y", False), field_name="layer.mirror_y"),
            border_color=_string(
                data.get("border_color", "#00000000"),
                field_name="layer.border_color",
                maximum=9,
            ).upper(),
            border_width=_number(
                data.get("border_width", 0.0),
                field_name="layer.border_width",
            ),
            corner_radius=_number(
                data.get("corner_radius", 0.0),
                field_name="layer.corner_radius",
            ),
        )


@dataclass(frozen=True, slots=True)
class PtzPosition:
    pan: float
    tilt: float
    zoom: float

    def __post_init__(self) -> None:
        for name, value in (("pan", self.pan), ("tilt", self.tilt)):
            _validate_number(value, field_name=f"ptz position {name}", minimum=-1.0, maximum=1.0)
        _validate_number(
            self.zoom,
            field_name="ptz position zoom",
            minimum=0.0,
            maximum=1.0,
        )

    def to_record(self) -> dict[str, float]:
        return {"pan": self.pan, "tilt": self.tilt, "zoom": self.zoom}

    @classmethod
    def from_record(cls, raw: object) -> PtzPosition:
        data = _mapping(
            raw,
            field_name="camera preset.position",
            allowed_keys={"pan", "tilt", "zoom"},
        )
        return cls(
            pan=_number(data.get("pan"), field_name="camera preset.position.pan"),
            tilt=_number(data.get("tilt"), field_name="camera preset.position.tilt"),
            zoom=_number(data.get("zoom"), field_name="camera preset.position.zoom"),
        )


@dataclass(frozen=True, slots=True)
class CameraPreset:
    id: str
    camera_source_id: str
    name: str
    remote_token: str = ""
    position: PtzPosition | None = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        _validate_identity(self.id, field_name="camera preset id")
        _validate_identity(self.camera_source_id, field_name="camera preset source id")
        _validate_name(self.name, field_name="camera preset name")
        if (
            not isinstance(self.remote_token, str)
            or len(self.remote_token) > 512
            or _contains_control_character(self.remote_token)
        ):
            raise SceneValidationError("Invalid PTZ preset token")
        if self.position is not None and not isinstance(self.position, PtzPosition):
            raise SceneValidationError("Invalid PTZ preset position")
        if not self.remote_token and self.position is None:
            raise SceneValidationError("Camera preset needs a remote token or absolute position")
        _validate_timestamp(self.created_at, field_name="camera preset.created_at")
        _validate_timestamp(self.updated_at, field_name="camera preset.updated_at")

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "camera_source_id": self.camera_source_id,
            "name": self.name,
            "remote_token": self.remote_token,
            "position": self.position.to_record() if self.position is not None else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_record(cls, raw: object) -> CameraPreset:
        data = _mapping(
            raw,
            field_name="camera preset",
            allowed_keys={
                "id",
                "camera_source_id",
                "name",
                "remote_token",
                "position",
                "created_at",
                "updated_at",
            },
        )
        position_raw = data.get("position")
        return cls(
            id=_string(data.get("id"), field_name="camera preset.id", maximum=128),
            camera_source_id=_string(
                data.get("camera_source_id"),
                field_name="camera preset.camera_source_id",
                maximum=128,
            ),
            name=_string(data.get("name"), field_name="camera preset.name", maximum=120),
            remote_token=_optional_string(
                data.get("remote_token", ""),
                field_name="camera preset.remote_token",
                maximum=512,
            ),
            position=(None if position_raw is None else PtzPosition.from_record(position_raw)),
            created_at=_string(
                data.get("created_at"),
                field_name="camera preset.created_at",
                maximum=64,
            ),
            updated_at=_string(
                data.get("updated_at"),
                field_name="camera preset.updated_at",
                maximum=64,
            ),
        )


@dataclass(frozen=True, slots=True)
class RecallPtzPresetAction:
    preset_id: str
    timeout_ms: int = 4000
    on_timeout: PtzTimeoutPolicy = PtzTimeoutPolicy.KEEP_CURRENT

    def __post_init__(self) -> None:
        _validate_identity(self.preset_id, field_name="entry action preset id")
        if (
            isinstance(self.timeout_ms, bool)
            or not isinstance(self.timeout_ms, int)
            or not 500 <= self.timeout_ms <= 10_000
        ):
            raise SceneValidationError("PTZ entry action timeout must be 500-10000 ms")
        if not isinstance(self.on_timeout, PtzTimeoutPolicy):
            raise SceneValidationError("Invalid PTZ timeout policy")

    def to_record(self) -> dict[str, object]:
        return {
            "type": "recall_ptz_preset",
            "preset_id": self.preset_id,
            "timeout_ms": self.timeout_ms,
            "on_timeout": self.on_timeout.value,
        }

    @classmethod
    def from_record(cls, raw: object) -> RecallPtzPresetAction:
        data = _mapping(
            raw,
            field_name="scene entry action",
            allowed_keys={"type", "preset_id", "timeout_ms", "on_timeout"},
        )
        if data.get("type") != "recall_ptz_preset":
            raise SceneValidationError(f"Unsupported scene entry action: {data.get('type')!r}")
        return cls(
            preset_id=_string(
                data.get("preset_id"),
                field_name="scene entry action.preset_id",
                maximum=128,
            ),
            timeout_ms=_integer(
                data.get("timeout_ms", 4000),
                field_name="scene entry action.timeout_ms",
                minimum=500,
                maximum=10_000,
            ),
            on_timeout=_enum(
                PtzTimeoutPolicy,
                data.get("on_timeout", PtzTimeoutPolicy.KEEP_CURRENT.value),
                field_name="scene entry action.on_timeout",
            ),
        )


@dataclass(frozen=True, slots=True)
class SceneDefinition:
    id: str
    name: str
    layers: tuple[SceneLayer, ...] = ()
    entry_actions: tuple[RecallPtzPresetAction, ...] = ()
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        _validate_identity(self.id, field_name="scene id")
        _validate_name(self.name, field_name="scene name")
        if not isinstance(self.layers, tuple) or not all(
            isinstance(layer, SceneLayer) for layer in self.layers
        ):
            raise SceneValidationError("Scene layers must be an immutable layer tuple")
        if not isinstance(self.entry_actions, tuple) or not all(
            isinstance(action, RecallPtzPresetAction) for action in self.entry_actions
        ):
            raise SceneValidationError("Scene entry actions must be an immutable action tuple")
        if len(self.layers) > MAX_LAYERS_PER_SCENE:
            raise SceneValidationError("Scene has too many layers")
        _ensure_unique((layer.id for layer in self.layers), field_name="layer id")
        _ensure_unique(
            (action.preset_id for action in self.entry_actions),
            field_name="entry action preset id",
        )
        _validate_timestamp(self.created_at, field_name="scene.created_at")
        _validate_timestamp(self.updated_at, field_name="scene.updated_at")

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "layers": [layer.to_record() for layer in self.layers],
            "entry_actions": [action.to_record() for action in self.entry_actions],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_record(cls, raw: object) -> SceneDefinition:
        data = _mapping(
            raw,
            field_name="scene",
            allowed_keys={
                "id",
                "name",
                "layers",
                "entry_actions",
                "created_at",
                "updated_at",
            },
        )
        return cls(
            id=_string(data.get("id"), field_name="scene.id", maximum=128),
            name=_string(data.get("name"), field_name="scene.name", maximum=120),
            layers=tuple(
                SceneLayer.from_record(item)
                for item in _sequence(data.get("layers", []), field_name="scene.layers")
            ),
            entry_actions=tuple(
                RecallPtzPresetAction.from_record(item)
                for item in _sequence(
                    data.get("entry_actions", []),
                    field_name="scene.entry_actions",
                )
            ),
            created_at=_string(
                data.get("created_at"),
                field_name="scene.created_at",
                maximum=64,
            ),
            updated_at=_string(
                data.get("updated_at"),
                field_name="scene.updated_at",
                maximum=64,
            ),
        )


@dataclass(frozen=True, slots=True)
class OutputRoute:
    bus_id: BusId
    default_scene_id: str
    start_with_solin: bool = False
    video_format: VideoFormat = field(default_factory=VideoFormat)

    def __post_init__(self) -> None:
        if not isinstance(self.bus_id, BusId):
            raise SceneValidationError("Invalid output bus")
        if self.default_scene_id:
            _validate_identity(self.default_scene_id, field_name="output default scene id")
        elif not isinstance(self.default_scene_id, str):
            raise SceneValidationError("Invalid output default scene id")
        if not isinstance(self.start_with_solin, bool):
            raise SceneValidationError("Output startup state must be a boolean")
        if not isinstance(self.video_format, VideoFormat):
            raise SceneValidationError("Invalid output video format")

    def to_record(self) -> dict[str, object]:
        return {
            "bus_id": self.bus_id.value,
            "default_scene_id": self.default_scene_id,
            "start_with_solin": self.start_with_solin,
            "video_format": self.video_format.to_record(),
        }

    @classmethod
    def from_record(cls, raw: object) -> OutputRoute:
        data = _mapping(
            raw,
            field_name="output route",
            allowed_keys={
                "bus_id",
                "default_scene_id",
                "start_with_solin",
                "video_format",
            },
        )
        return cls(
            bus_id=_enum(BusId, data.get("bus_id"), field_name="output route.bus_id"),
            default_scene_id=_string(
                data.get("default_scene_id"),
                field_name="output route.default_scene_id",
                allow_empty=True,
                maximum=128,
            ),
            start_with_solin=_boolean(
                data.get("start_with_solin", False),
                field_name="output route.start_with_solin",
            ),
            video_format=VideoFormat.from_record(data.get("video_format")),
        )


@dataclass(frozen=True, slots=True)
class AutomationMap:
    bus_id: BusId
    assignments: tuple[tuple[ContentCategory, str], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.bus_id, BusId):
            raise SceneValidationError("Invalid automation bus")
        if not isinstance(self.assignments, tuple) or not all(
            isinstance(assignment, tuple)
            and len(assignment) == 2
            and isinstance(assignment[0], ContentCategory)
            and isinstance(assignment[1], str)
            for assignment in self.assignments
        ):
            raise SceneValidationError("Automation assignments must be an immutable category tuple")
        _ensure_unique((category.value for category, _ in self.assignments), field_name="category")
        for _, scene_id in self.assignments:
            _validate_identity(scene_id, field_name="automation scene id")

    def scene_for(self, category: ContentCategory) -> str | None:
        return next(
            (scene_id for candidate, scene_id in self.assignments if candidate is category),
            None,
        )

    def to_record(self) -> dict[str, object]:
        return {
            "bus_id": self.bus_id.value,
            "assignments": {category.value: scene_id for category, scene_id in self.assignments},
        }

    @classmethod
    def from_record(cls, raw: object) -> AutomationMap:
        data = _mapping(
            raw,
            field_name="automation map",
            allowed_keys={"bus_id", "assignments"},
        )
        assignments = _mapping(
            data.get("assignments"),
            field_name="automation map.assignments",
        )
        return cls(
            bus_id=_enum(BusId, data.get("bus_id"), field_name="automation map.bus_id"),
            assignments=tuple(
                (
                    _enum(ContentCategory, category, field_name="automation category"),
                    _string(scene_id, field_name="automation scene id", maximum=128),
                )
                for category, scene_id in assignments.items()
            ),
        )


def _ensure_unique(values: Iterable[str], *, field_name: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise SceneValidationError(f"Duplicate {field_name}: {value!r}")
        seen.add(value)


def _validate_scene_reference_graph(
    scenes: tuple[SceneDefinition, ...],
    *,
    scene_reference_by_source_id: dict[str, str],
) -> None:
    graph = {
        scene.id: tuple(
            scene_reference_by_source_id[layer.source_id]
            for layer in scene.layers
            if layer.source_id in scene_reference_by_source_id
        )
        for scene in scenes
    }
    state: dict[str, int] = {scene.id: 0 for scene in scenes}

    def visit(scene_id: str) -> None:
        current = state[scene_id]
        if current == 1:
            raise SceneValidationError("Scene references must not contain a cycle")
        if current == 2:
            return
        state[scene_id] = 1
        for target_scene_id in graph[scene_id]:
            visit(target_scene_id)
        state[scene_id] = 2

    for scene in scenes:
        visit(scene.id)


@dataclass(frozen=True, slots=True)
class SceneDocument:
    document_id: str
    revision: int
    sources: tuple[SourceDefinition, ...]
    scenes: tuple[SceneDefinition, ...]
    outputs: tuple[OutputRoute, ...]
    automation: tuple[AutomationMap, ...]
    transition_policy: SceneTransitionPolicy = field(default_factory=SceneTransitionPolicy)
    camera_presets: tuple[CameraPreset, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int):
            raise SceneValidationError("Scene schema version must be an integer")
        if self.schema_version != SCHEMA_VERSION:
            raise UnsupportedSceneSchemaError(self.schema_version)
        _validate_identity(self.document_id, field_name="document id")
        if not isinstance(self.revision, int) or isinstance(self.revision, bool):
            raise SceneValidationError("Document revision must be an integer")
        if self.revision < 0:
            raise SceneValidationError("Document revision must not be negative")
        for field_name, value, item_type in (
            ("sources", self.sources, SourceDefinition),
            ("scenes", self.scenes, SceneDefinition),
            ("outputs", self.outputs, OutputRoute),
            ("automation", self.automation, AutomationMap),
            ("camera_presets", self.camera_presets, CameraPreset),
        ):
            if not isinstance(value, tuple) or not all(
                isinstance(item, item_type) for item in value
            ):
                raise SceneValidationError(
                    f"Document {field_name} must be an immutable typed tuple"
                )
        if not isinstance(self.transition_policy, SceneTransitionPolicy):
            raise SceneValidationError("Invalid scene transition policy")
        if not self.scenes:
            raise SceneValidationError("Document must contain at least one scene")
        if len(self.sources) > MAX_SOURCES:
            raise SceneValidationError("Document has too many sources")
        if len(self.scenes) > MAX_SCENES:
            raise SceneValidationError("Document has too many scenes")
        if len(self.camera_presets) > MAX_CAMERA_PRESETS:
            raise SceneValidationError("Document has too many camera presets")

        _ensure_unique((source.id for source in self.sources), field_name="source id")
        _ensure_unique((scene.id for scene in self.scenes), field_name="scene id")
        _ensure_unique((preset.id for preset in self.camera_presets), field_name="preset id")
        _ensure_unique((route.bus_id.value for route in self.outputs), field_name="output bus")
        _ensure_unique(
            (mapping.bus_id.value for mapping in self.automation),
            field_name="automation bus",
        )

        source_by_id = {source.id: source for source in self.sources}
        scene_ids = {scene.id for scene in self.scenes}
        preset_by_id = {preset.id: preset for preset in self.camera_presets}
        if any(
            scene_id not in scene_ids
            for scene_id, _ in self.transition_policy.overrides
        ):
            raise SceneValidationError(
                "Transition overrides must reference existing scenes"
            )

        content_source = source_by_id.get(CONTENT_SOURCE_ID)
        if content_source is None or content_source.kind is not SourceKind.SOLIN_CONTENT:
            raise SceneValidationError("Document must contain the Solin content source")
        if sum(source.kind is SourceKind.SOLIN_CONTENT for source in self.sources) != 1:
            raise SceneValidationError("Document must contain exactly one Solin content source")
        no_signal_source = source_by_id.get(NO_SIGNAL_SOURCE_ID)
        if no_signal_source is None or no_signal_source.kind is not SourceKind.COLOR:
            raise SceneValidationError("Document must contain the no-signal fallback source")
        _ensure_unique(
            (
                source.configuration.device_id.casefold()
                for source in self.sources
                if source.kind is SourceKind.LOCAL_CAMERA
                and isinstance(source.configuration, LocalCameraConfig)
                and source.configuration.device_id
            ),
            field_name="local camera device",
        )

        scene_reference_by_source_id: dict[str, str] = {}
        for source in self.sources:
            if source.kind is not SourceKind.SCENE_REFERENCE:
                continue
            configuration = source.configuration
            if not isinstance(configuration, SceneReferenceConfig):
                raise SceneValidationError("Invalid scene reference configuration")
            if configuration.target_scene_id not in scene_ids:
                raise SceneValidationError(
                    f"Source {source.id!r} references missing scene "
                    f"{configuration.target_scene_id!r}"
                )
            scene_reference_by_source_id[source.id] = configuration.target_scene_id

        for scene in self.scenes:
            action_camera_ids: list[str] = []
            for layer in scene.layers:
                if layer.source_id not in source_by_id:
                    raise SceneValidationError(
                        f"Layer {layer.id!r} references missing source {layer.source_id!r}"
                    )
            for action in scene.entry_actions:
                preset = preset_by_id.get(action.preset_id)
                if preset is None:
                    raise SceneValidationError(
                        f"Scene {scene.id!r} references missing preset {action.preset_id!r}"
                    )
                action_camera_ids.append(preset.camera_source_id)
            _ensure_unique(
                action_camera_ids,
                field_name=f"entry action camera in scene {scene.id}",
            )

        _validate_scene_reference_graph(
            self.scenes,
            scene_reference_by_source_id=scene_reference_by_source_id,
        )

        for preset in self.camera_presets:
            source = source_by_id.get(preset.camera_source_id)
            if source is None or source.kind not in {
                SourceKind.LOCAL_CAMERA,
                SourceKind.RTSP_CAMERA,
            }:
                raise SceneValidationError(f"Preset {preset.id!r} must reference a camera source")
            configuration = source.configuration
            if not isinstance(configuration, (LocalCameraConfig, RtspCameraConfig)):
                raise SceneValidationError(
                    f"Preset {preset.id!r} has an invalid camera configuration"
                )
            if configuration.ptz_binding is None:
                raise SceneValidationError(f"Preset {preset.id!r} camera has no PTZ binding")

        expected_buses = set(DELIVERY_BUSES)
        if {route.bus_id for route in self.outputs} != expected_buses:
            raise SceneValidationError("Document must define every output bus exactly once")
        if {mapping.bus_id for mapping in self.automation} != expected_buses:
            raise SceneValidationError("Document must define automation for every bus")

        # Each delivery output carries its OWN default scene: the projection can
        # idle on the year text while the program idles on a camera.
        automatic_categories = set(AUTOMATIC_MEDIA_CATEGORIES)
        program_assignments: dict[ContentCategory, str] | None = None
        for mapping in self.automation:
            assignments = dict(mapping.assignments)
            if assignments and set(assignments) != automatic_categories:
                raise SceneValidationError(
                    "Program automation must define every media category"
                )
            if len(set(assignments.values())) > 1:
                raise SceneValidationError(
                    "Program media categories must share one scene"
                )
            if program_assignments is None:
                program_assignments = assignments
            elif assignments != program_assignments:
                raise SceneValidationError(
                    "Every destination must share the Program media scene"
                )

        for route in self.outputs:
            if route.default_scene_id and route.default_scene_id not in scene_ids:
                raise SceneValidationError(
                    f"Output {route.bus_id.value!r} references a missing default scene"
                )
        for mapping in self.automation:
            for _, scene_id in mapping.assignments:
                if scene_id not in scene_ids:
                    raise SceneValidationError(
                        f"Automation for {mapping.bus_id.value!r} references a missing scene"
                    )

    def source(self, source_id: str) -> SourceDefinition:
        return next(source for source in self.sources if source.id == source_id)

    def scene(self, scene_id: str) -> SceneDefinition:
        return next(scene for scene in self.scenes if scene.id == scene_id)

    def output(self, bus_id: BusId) -> OutputRoute:
        return next(route for route in self.outputs if route.bus_id is bus_id)

    def automation_for(self, bus_id: BusId) -> AutomationMap:
        return next(mapping for mapping in self.automation if mapping.bus_id is bus_id)

    def with_revision(self, revision: int) -> SceneDocument:
        return replace(self, revision=revision)

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "document_id": self.document_id,
            "revision": self.revision,
            "sources": [source.to_record() for source in self.sources],
            "scenes": [scene.to_record() for scene in self.scenes],
            "outputs": [route.to_record() for route in self.outputs],
            "automation": [mapping.to_record() for mapping in self.automation],
            "transition_policy": self.transition_policy.to_record(),
            "camera_presets": [preset.to_record() for preset in self.camera_presets],
        }

    @classmethod
    def from_record(cls, raw: object) -> SceneDocument:
        data = _mapping(
            raw,
            field_name="scene document",
            allowed_keys={
                "schema_version",
                "document_id",
                "revision",
                "sources",
                "scenes",
                "outputs",
                "automation",
                "transition_policy",
                "camera_presets",
            },
        )
        version = _integer(
            data.get("schema_version"),
            field_name="schema_version",
            minimum=1,
            maximum=2**31 - 1,
        )
        if version > SCHEMA_VERSION:
            raise UnsupportedSceneSchemaError(version)
        data = _migrate_scene_document_record(data, version)
        return cls(
            schema_version=SCHEMA_VERSION,
            document_id=_string(
                data.get("document_id"),
                field_name="document_id",
                maximum=128,
            ),
            revision=_integer(
                data.get("revision"),
                field_name="revision",
                minimum=0,
                maximum=2**63 - 1,
            ),
            sources=tuple(
                SourceDefinition.from_record(item)
                for item in _sequence(data.get("sources"), field_name="sources")
            ),
            scenes=tuple(
                SceneDefinition.from_record(item)
                for item in _sequence(data.get("scenes"), field_name="scenes")
            ),
            outputs=tuple(
                OutputRoute.from_record(item)
                for item in _sequence(data.get("outputs"), field_name="outputs")
            ),
            automation=tuple(
                AutomationMap.from_record(item)
                for item in _sequence(data.get("automation"), field_name="automation")
            ),
            transition_policy=SceneTransitionPolicy.from_record(
                data.get("transition_policy")
            ),
            camera_presets=tuple(
                CameraPreset.from_record(item)
                for item in _sequence(
                    data.get("camera_presets", []),
                    field_name="camera_presets",
                )
            ),
        )


def _normalize_program_destinations_record(migrated: dict[str, Any]) -> None:
    outputs = migrated.get("outputs")
    program_default_scene_id: str | None = None
    if isinstance(outputs, list):
        for route in outputs:
            if (
                isinstance(route, dict)
                and route.get("bus_id") == BusId.VIRTUAL_CAMERA.value
                and isinstance(route.get("default_scene_id"), str)
            ):
                program_default_scene_id = route["default_scene_id"]
                break
        if program_default_scene_id is not None:
            for route in outputs:
                if isinstance(route, dict):
                    route["default_scene_id"] = program_default_scene_id

    automation = migrated.get("automation")
    media_scene_id: str | None = None
    if not isinstance(automation, list):
        return
    ordered_mappings = sorted(
        (mapping for mapping in automation if isinstance(mapping, dict)),
        key=lambda mapping: mapping.get("bus_id") != BusId.VIRTUAL_CAMERA.value,
    )
    for mapping in ordered_mappings:
        assignments = mapping.get("assignments")
        if not isinstance(assignments, dict):
            continue
        for category in AUTOMATIC_MEDIA_CATEGORIES:
            candidate = assignments.get(category.value)
            if isinstance(candidate, str):
                media_scene_id = candidate
                break
        if media_scene_id is not None:
            break
    program_assignments = (
        {
            category.value: media_scene_id
            for category in AUTOMATIC_MEDIA_CATEGORIES
        }
        if media_scene_id is not None
        else {}
    )
    for mapping in automation:
        if isinstance(mapping, dict):
            mapping["assignments"] = dict(program_assignments)


def _migrate_program_transition_record(migrated: dict[str, Any]) -> None:
    outputs = migrated.get("outputs")
    canonical_route: dict[str, Any] | None = None
    if isinstance(outputs, list):
        canonical_route = next(
            (
                route
                for route in outputs
                if isinstance(route, dict)
                and route.get("bus_id") == BusId.VIRTUAL_CAMERA.value
            ),
            None,
        )
        if canonical_route is None:
            canonical_route = next(
                (route for route in outputs if isinstance(route, dict)),
                None,
            )
    if "transition_policy" not in migrated:
        legacy_kind = (
            canonical_route.get("transition", TransitionKind.CUT.value)
            if canonical_route is not None
            else TransitionKind.CUT.value
        )
        legacy_duration = (
            canonical_route.get("transition_duration_ms", 0)
            if canonical_route is not None
            else 0
        )
        if legacy_kind not in {"cut", "fade"}:
            raise SceneValidationError("Invalid legacy output transition")
        if (
            isinstance(legacy_duration, bool)
            or not isinstance(legacy_duration, int)
            or not 0 <= legacy_duration <= 10_000
        ):
            raise SceneValidationError("Invalid legacy output transition duration")
        if legacy_kind == "cut" and legacy_duration != 0:
            raise SceneValidationError("Legacy cut transition duration must be zero")
        if legacy_kind == "fade":
            # Version 8 accepted a zero-duration fade. Keep those documents
            # loadable while moving them to a real animated transition.
            duration = max(50, legacy_duration)
            default_transition = {
                "kind": TransitionKind.DISSOLVE.value,
                "duration_ms": duration,
            }
        else:
            default_transition = {
                "kind": TransitionKind.CUT.value,
                "duration_ms": 0,
            }
        migrated["transition_policy"] = {
            "default": default_transition,
            "overrides": {},
        }
    if isinstance(outputs, list):
        for route in outputs:
            if isinstance(route, dict):
                route.pop("transition", None)
                route.pop("transition_duration_ms", None)


def _migrate_scene_document_record(
    record: dict[str, Any],
    version: int,
) -> dict[str, Any]:
    migrated = deepcopy(record)
    current_version = version
    while current_version < SCHEMA_VERSION:
        if current_version == 1:
            sources = migrated.get("sources")
            if isinstance(sources, list):
                for source in sources:
                    if not isinstance(source, dict) or source.get("type") != "local_camera":
                        continue
                    configuration = source.get("configuration")
                    if not isinstance(configuration, dict):
                        continue
                    if "fps_numerator" in configuration or "fps_denominator" in configuration:
                        raise SceneValidationError(
                            "Unknown source.configuration frame-rate fields in schema version 1"
                        )
                    legacy_fps = configuration.pop("fps", 0)
                    configuration["fps_numerator"] = legacy_fps
                    configuration["fps_denominator"] = 1
            current_version = 2
            migrated["schema_version"] = current_version
            continue
        if current_version == 2:
            sources = migrated.get("sources")
            if isinstance(sources, list):
                for source in sources:
                    if not isinstance(source, dict) or source.get("type") != "local_camera":
                        continue
                    configuration = source.get("configuration")
                    if not isinstance(configuration, dict):
                        continue
                    if "media_type" in configuration:
                        raise SceneValidationError(
                            "Unknown source.configuration media type in schema version 2"
                        )
                    pixel_format = configuration.get("pixel_format", "")
                    if pixel_format == "":
                        configuration["media_type"] = ""
                        configuration["width"] = 0
                        configuration["height"] = 0
                        configuration["fps_numerator"] = 0
                        configuration["fps_denominator"] = 1
                    elif pixel_format == "JPEG":
                        configuration["media_type"] = CameraMediaType.JPEG.value
                    elif pixel_format == "H264":
                        configuration["media_type"] = CameraMediaType.H264.value
                    else:
                        configuration["media_type"] = CameraMediaType.RAW.value
            current_version = 3
            migrated["schema_version"] = current_version
            continue
        if current_version == 3:
            sources = migrated.get("sources")
            if isinstance(sources, list):
                for source in sources:
                    if not isinstance(source, dict) or source.get("type") != "local_camera":
                        continue
                    configuration = source.get("configuration")
                    if not isinstance(configuration, dict):
                        continue
                    width = configuration.get("width")
                    height = configuration.get("height")
                    fps_numerator = configuration.get("fps_numerator")
                    fps_denominator = configuration.get("fps_denominator")
                    media_type = configuration.get("media_type")
                    pixel_format = configuration.get("pixel_format")
                    exact_within_budget = False
                    if (
                        isinstance(width, int)
                        and not isinstance(width, bool)
                        and isinstance(height, int)
                        and not isinstance(height, bool)
                        and isinstance(fps_numerator, int)
                        and not isinstance(fps_numerator, bool)
                        and isinstance(fps_denominator, int)
                        and not isinstance(fps_denominator, bool)
                    ):
                        exact_within_budget = (
                            width > 0
                            and height > 0
                            and width <= MAXIMUM_CAMERA_SOURCE_DIMENSION
                            and height <= MAXIMUM_CAMERA_SOURCE_DIMENSION
                            and width * height <= MAXIMUM_CAMERA_SOURCE_PIXELS
                            and min(width, height) <= MAXIMUM_CAMERA_SOURCE_SHORT_EDGE
                            and fps_numerator > 0
                            and fps_denominator > 0
                            and fps_numerator <= MAXIMUM_CAMERA_SOURCE_FPS * fps_denominator
                            and isinstance(media_type, str)
                            and media_type != ""
                            and isinstance(pixel_format, str)
                            and pixel_format != ""
                        )
                    fully_automatic = (
                        width == 0
                        and height == 0
                        and fps_numerator == 0
                        and fps_denominator == 1
                        and media_type == ""
                        and pixel_format == ""
                    )
                    if not exact_within_budget and not fully_automatic:
                        configuration.update(
                            {
                                "width": 0,
                                "height": 0,
                                "fps_numerator": 0,
                                "fps_denominator": 1,
                                "media_type": "",
                                "pixel_format": "",
                            }
                        )
            current_version = 4
            migrated["schema_version"] = current_version
            continue
        if current_version == 4:
            camera_scene_id = stable_identity("default-scene:camera")
            content_scene_id = stable_identity("default-scene:content")
            automation = migrated.get("automation")
            if isinstance(automation, list):
                for mapping in automation:
                    if (
                        not isinstance(mapping, dict)
                        or mapping.get("bus_id") != BusId.VIRTUAL_CAMERA.value
                    ):
                        continue
                    assignments = mapping.get("assignments")
                    if not isinstance(assignments, dict) or not assignments:
                        continue
                    if set(assignments.values()) != {camera_scene_id}:
                        continue
                    mapping["assignments"] = {
                        ContentCategory.IMAGE.value: content_scene_id,
                        ContentCategory.VIDEO.value: content_scene_id,
                        ContentCategory.TIMER.value: content_scene_id,
                        ContentCategory.BROWSER.value: content_scene_id,
                        ContentCategory.EXTERNAL_STREAM.value: content_scene_id,
                        ContentCategory.CAMERA.value: camera_scene_id,
                    }
            current_version = 5
            migrated["schema_version"] = current_version
            continue
        if current_version == 5:
            sources = migrated.get("sources")
            replacements: dict[str, str] = {}
            if isinstance(sources, list):
                canonical_by_device: dict[str, dict[str, Any]] = {}
                retained_sources: list[Any] = []
                for source in sources:
                    if not isinstance(source, dict) or source.get("type") != "local_camera":
                        retained_sources.append(source)
                        continue
                    configuration = source.get("configuration")
                    device_id = (
                        configuration.get("device_id")
                        if isinstance(configuration, dict)
                        else None
                    )
                    source_id = source.get("id")
                    if not isinstance(device_id, str) or not device_id or not isinstance(source_id, str):
                        retained_sources.append(source)
                        continue
                    key = device_id.casefold()
                    canonical = canonical_by_device.get(key)
                    if canonical is None:
                        canonical_by_device[key] = source
                        retained_sources.append(source)
                        continue
                    canonical_id = canonical.get("id")
                    if not isinstance(canonical_id, str):
                        retained_sources.append(source)
                        continue
                    replacements[source_id] = canonical_id
                    canonical_configuration = canonical.get("configuration")
                    if (
                        isinstance(canonical_configuration, dict)
                        and canonical_configuration.get("ptz_binding") is None
                        and isinstance(configuration, dict)
                        and configuration.get("ptz_binding") is not None
                    ):
                        canonical_configuration["ptz_binding"] = configuration["ptz_binding"]
                migrated["sources"] = retained_sources
            if replacements:
                scenes = migrated.get("scenes")
                if isinstance(scenes, list):
                    for scene in scenes:
                        layers = scene.get("layers") if isinstance(scene, dict) else None
                        if not isinstance(layers, list):
                            continue
                        for layer in layers:
                            if not isinstance(layer, dict):
                                continue
                            source_id = layer.get("source_id")
                            if isinstance(source_id, str) and source_id in replacements:
                                layer["source_id"] = replacements[source_id]
                presets = migrated.get("camera_presets")
                if isinstance(presets, list):
                    for preset in presets:
                        if not isinstance(preset, dict):
                            continue
                        source_id = preset.get("camera_source_id")
                        if isinstance(source_id, str) and source_id in replacements:
                            preset["camera_source_id"] = replacements[source_id]
            current_version = 6
            migrated["schema_version"] = current_version
            continue
        if current_version == 6:
            _normalize_program_destinations_record(migrated)
            current_version = 7
            migrated["schema_version"] = current_version
            continue
        if current_version == 7:
            current_version = 8
            migrated["schema_version"] = current_version
            continue
        if current_version == 8:
            _migrate_program_transition_record(migrated)
            current_version = 9
            migrated["schema_version"] = current_version
            continue
        if current_version == 9:
            # Outputs may now hold different default scenes. A v9 record has them
            # equal, which is a perfectly valid v10 record, so there is nothing to
            # transform — the bump exists so an older build rejects a diverged
            # document outright instead of failing its own validation.
            current_version = 10
            migrated["schema_version"] = current_version
            continue
        raise UnsupportedSceneSchemaError(current_version)
    return migrated
