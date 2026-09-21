from __future__ import annotations

import os
from pathlib import Path
import tempfile


def write_bytes_atomic(
    path: str | Path,
    payload: bytes,
    *,
    mode: int = 0o600,
) -> None:
    """Durably replace one local binary file without exposing partial content."""

    if not isinstance(payload, bytes):
        raise TypeError("Binary file payload must be bytes")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            temporary.chmod(mode)
        os.replace(temporary, target)
        if os.name != "nt":
            target.chmod(mode)
            directory_descriptor = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def publish_bytes_immutable(path: str | Path, payload: bytes, *, mode: int = 0o600) -> bool:
    """Publish *payload* once without replacing a concurrent writer.

    Returns ``True`` when this call created the file and ``False`` when an
    identical file already existed. A different existing payload is a
    conflict. The temporary file lives beside the target so publication stays
    on one filesystem.
    """

    if not isinstance(payload, bytes):
        raise TypeError("Binary file payload must be bytes")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            temporary.chmod(mode)
        try:
            if os.name == "nt":
                os.rename(temporary, target)
            else:
                os.link(temporary, target)
        except FileExistsError:
            if target.read_bytes() != payload:
                raise
            return False
        if os.name != "nt":
            target.chmod(mode)
            directory_descriptor = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        return True
    finally:
        temporary.unlink(missing_ok=True)
