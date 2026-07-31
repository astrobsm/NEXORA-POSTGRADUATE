"""Institution branding assets — logo, app icon, favicon, sign-in backdrop.

Stored as bytes in the database rather than object storage. That is a deliberate
choice for *these* assets specifically:

* They are small (capped at 512 KiB) and change perhaps once a year.
* Branding must work on a deployment with no object storage configured at all —
  a single teaching hospital running the Compose stack should be able to upload
  its crest without standing up MinIO first.
* They are read on nearly every page load, so a database round-trip with an
  ETag beats a signed-URL round-trip to a separate service.

Logbook attachments and generated reports do *not* follow this pattern — they are
large, numerous, and belong in object storage.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.tenancy import Tenant


class BrandingAssetKind(StrEnum):
    """Where each asset is used."""

    #: Wordmark or crest, shown in the sidebar and on the sign-in screen.
    LOGO = "logo"
    #: Square mark for the installed app icon and the PWA manifest.
    ICON = "icon"
    #: Browser tab icon. Falls back to ICON when not set.
    FAVICON = "favicon"
    #: Optional image behind the sign-in panel.
    LOGIN_BACKDROP = "login_backdrop"


#: Content types accepted on upload.
#:
#: SVG is permitted because most institutions hold their crest as one, but it is
#: an active document format: an SVG can carry <script>. The serving endpoint
#: therefore returns every asset with `Content-Security-Policy: sandbox` and
#: `X-Content-Type-Options: nosniff`, and the client only ever renders branding
#: through an <img> tag, where scripts do not execute.
ALLOWED_CONTENT_TYPES: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "image/gif": "gif",
}

#: 512 KiB. Generous for a crest, small enough that the row stays cheap to read.
MAX_ASSET_BYTES = 512 * 1024


class BrandingAsset(Base, IdMixin, TimestampMixin):
    __tablename__ = "branding_assets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", name="uq_branding_asset_tenant_kind"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)

    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(255), default=None)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    #: SHA-256 of the bytes. Doubles as the ETag, so an unchanged logo is served
    #: from the browser cache with a 304 rather than re-sent on every page.
    checksum: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    #: Intrinsic dimensions when they could be determined; used by the admin
    #: preview to warn about a non-square app icon.
    width: Mapped[int | None] = mapped_column(default=None)
    height: Mapped[int | None] = mapped_column(default=None)

    uploaded_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    tenant: Mapped[Tenant] = relationship()

    @property
    def etag(self) -> str:
        return f'"{self.checksum}"'
