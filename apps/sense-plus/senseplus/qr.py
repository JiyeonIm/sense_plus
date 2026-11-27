"""Encode the local phone URL without contacting a QR service."""

from io import BytesIO

import segno


def render_qr_png(user_url: str) -> bytes:
    image = BytesIO()
    segno.make_qr(user_url).save(image, kind="png", scale=8, border=4)
    return image.getvalue()
