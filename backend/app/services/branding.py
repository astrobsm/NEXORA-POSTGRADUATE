"""Branding asset validation.

Upload is one of the few places an institution hands the platform arbitrary bytes,
so the checks here are deliberately strict: declared content type is not trusted,
the real format is confirmed from the file's magic bytes, and SVG is scanned for
active content before it is stored.
"""

from __future__ import annotations

import hashlib
import re
import struct

from app.models.branding import ALLOWED_CONTENT_TYPES, MAX_ASSET_BYTES


class BrandingAssetError(ValueError):
    """Raised with a message intended for the person doing the upload."""


#: Magic-byte signatures. A browser sniffs content regardless of the declared
#: type, so a PNG-labelled HTML file would otherwise be a stored-XSS vector.
_SIGNATURES: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]

#: Active content in SVG. Rejected rather than stripped: silently altering an
#: institution's crest is worse than telling them to supply a clean file.
_SVG_DANGEROUS = re.compile(
    rb"<\s*script|<\s*foreignObject|javascript:|\son\w+\s*=|<\s*!ENTITY|<\s*iframe|<\s*embed|<\s*object",
    re.IGNORECASE,
)


def _detect_content_type(data: bytes) -> str | None:
    for signature, content_type in _SIGNATURES:
        if data.startswith(signature):
            return content_type
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    head = data[:1024].lstrip()
    if head.startswith(b"<?xml") or head.startswith(b"<svg") or b"<svg" in head[:512].lower():
        return "image/svg+xml"
    return None


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    # IHDR is always the first chunk: 8-byte signature, 4-byte length, "IHDR".
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _gif_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 10:
        return None
    width, height = struct.unpack("<HH", data[6:10])
    return width, height


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    index = 2
    length = len(data)
    while index < length - 9:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        # SOF0-SOF15, excluding the non-frame markers in that range.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return width, height
        if index + 4 > length:
            break
        segment = struct.unpack(">H", data[index + 2 : index + 4])[0]
        index += 2 + segment
    return None


def _svg_dimensions(data: bytes) -> tuple[int, int] | None:
    head = data[:2048].decode("utf-8", errors="ignore")
    viewbox = re.search(r'viewBox\s*=\s*"[\d.\-]+\s+[\d.\-]+\s+([\d.]+)\s+([\d.]+)"', head)
    if viewbox:
        return int(float(viewbox.group(1))), int(float(viewbox.group(2)))
    width = re.search(r'\swidth\s*=\s*"(\d+)', head)
    height = re.search(r'\sheight\s*=\s*"(\d+)', head)
    if width and height:
        return int(width.group(1)), int(height.group(1))
    return None


def dimensions_of(data: bytes, content_type: str) -> tuple[int | None, int | None]:
    """Best-effort intrinsic size, so the admin screen can warn about a
    non-square app icon. Never fatal — an unreadable size is simply unknown."""
    try:
        result = {
            "image/png": _png_dimensions,
            "image/gif": _gif_dimensions,
            "image/jpeg": _jpeg_dimensions,
            "image/svg+xml": _svg_dimensions,
        }.get(content_type, lambda _: None)(data)
    except Exception:
        return None, None
    return result if result else (None, None)


def validate(data: bytes, declared_content_type: str | None, *, kind: str) -> tuple[str, str]:
    """Validate an upload and return ``(content_type, sha256)``.

    Raises :class:`BrandingAssetError` with a message the uploader can act on.
    """
    if not data:
        raise BrandingAssetError("The uploaded file is empty.")

    if len(data) > MAX_ASSET_BYTES:
        raise BrandingAssetError(
            f"That file is {len(data) // 1024} KiB. The limit is "
            f"{MAX_ASSET_BYTES // 1024} KiB — export the logo at a smaller size, "
            "or use SVG."
        )

    detected = _detect_content_type(data)
    if detected is None:
        raise BrandingAssetError(
            "That file is not a recognised image. Accepted formats are PNG, JPEG, "
            "WebP, GIF and SVG."
        )

    if detected not in ALLOWED_CONTENT_TYPES:
        raise BrandingAssetError(f"{detected} files are not accepted for branding.")

    # The declared type is a hint only; a mismatch usually means a renamed file.
    if declared_content_type and declared_content_type.split(";")[0].strip() != detected:
        raise BrandingAssetError(
            f"The file was sent as {declared_content_type} but its contents are "
            f"{detected}. Re-export it in the format you intend to use."
        )

    if detected == "image/svg+xml" and _SVG_DANGEROUS.search(data):
        raise BrandingAssetError(
            "That SVG contains scripting or embedded content, which is not accepted. "
            "Export a flattened SVG from your design tool, or upload a PNG."
        )

    return detected, hashlib.sha256(data).hexdigest()
