from __future__ import annotations

import io


def generate_qr_png(value: str) -> bytes:
    """Render a value as PNG bytes using the configured QR backend."""
    if not value:
        raise ValueError("QR value cannot be empty")

    import qrcode  # type: ignore[import]
    from qrcode.constants import ERROR_CORRECT_M  # type: ignore[import]

    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=6,
        border=2,
    )
    qr.add_data(value)
    qr.make(fit=True)
    buffer = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(buffer, "PNG")
    return buffer.getvalue()
