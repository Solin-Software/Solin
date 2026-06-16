from __future__ import annotations

import io


def generate_qr_png(url: str) -> bytes:
    """Render a URL as PNG bytes using the configured qrcode backend."""
    import qrcode  # type: ignore[import]

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=6,
        border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)
    buffer = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(
        buffer,
        format="PNG",
    )
    return buffer.getvalue()
