from __future__ import annotations

import re
from pathlib import Path
from typing import TypedDict
from urllib.parse import unquote

from solin.core.foundation.constants import JWPUB_EXTS, PDF_EXTS, PLAYLIST_EXTS
from solin.core.media.formats import AUDIO_EXTS, IMAGE_EXTS, VIDEO_EXTS

ALLOWED_UPLOAD_EXTS: frozenset[str] = (
    VIDEO_EXTS | AUDIO_EXTS | IMAGE_EXTS | PDF_EXTS | PLAYLIST_EXTS | JWPUB_EXTS
)
MAX_UPLOAD_BODY_BYTES: int = 2 * 1024**3


class UploadPart(TypedDict):
    filename: str
    data: bytes
    content_type: str


def safe_filename(raw: str) -> str:
    name = re.split(r"[\\/]+", raw)[-1]
    name = re.sub(r"[^\w\s.\-]", "_", name)
    name = name.strip(". ") or "upload"
    return name[:180]


def is_allowed_upload_filename(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_UPLOAD_EXTS


def parse_multipart(body: bytes, boundary: bytes) -> list[UploadPart]:
    """
    Return uploaded file parts and ignore regular form fields without filename.
    """
    delim = b"--" + boundary
    parts: list[UploadPart] = []

    segments = body.split(delim)
    for segment in segments[1:]:
        if segment.startswith(b"--"):
            break
        try:
            header_end = segment.index(b"\r\n\r\n")
        except ValueError:
            continue

        header_raw = segment[:header_end].decode("utf-8", errors="replace")
        body_part = segment[header_end + 4:]
        if body_part.endswith(b"\r\n"):
            body_part = body_part[:-2]

        disposition = re.search(
            r'Content-Disposition:[^\r\n]*filename\*?=["\']?(?:utf-8\'\')?([^"\'\r\n;]+)',
            header_raw,
            re.IGNORECASE,
        )
        if not disposition:
            continue

        raw_name = unquote(disposition.group(1).strip().strip("\"'"))
        filename = safe_filename(raw_name)
        if not filename:
            continue

        content_type = re.search(r"Content-Type:\s*(\S+)", header_raw, re.IGNORECASE)
        parts.append(
            {
                "filename": filename,
                "data": body_part,
                "content_type": (
                    content_type.group(1)
                    if content_type
                    else "application/octet-stream"
                ),
            }
        )

    return parts
