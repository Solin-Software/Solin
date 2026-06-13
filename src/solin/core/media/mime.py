from __future__ import annotations


def mime_to_ext(mime_type: str) -> str:
    """Return a safe file extension for image, video, and audio MIME types."""
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
        "image/svg+xml": ".svg",
        "image/tiff": ".tiff",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "video/x-matroska": ".mkv",
        "video/x-msvideo": ".avi",
        "video/quicktime": ".mov",
        "video/mpeg": ".mpeg",
        "video/ogg": ".ogv",
        "video/3gpp": ".3gp",
        "video/mp2t": ".ts",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "audio/ogg": ".ogg",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/webm": ".webm",
        "audio/flac": ".flac",
        "audio/x-flac": ".flac",
        "audio/aac": ".aac",
        "audio/opus": ".opus",
        "audio/x-ms-wma": ".wma",
    }
    mime = (mime_type or "").lower().strip()
    if mime in mapping:
        return mapping[mime]
    if mime.startswith("audio/"):
        return ".mp3"
    if mime.startswith("video/"):
        return ".mp4"
    return ".jpg"
