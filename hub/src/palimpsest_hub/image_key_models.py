"""Image API keys (`ppi_v1_`): a key kind separate from `ppk_v1` package keys."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CHAR, JSON
from sqlalchemy.dialects.mysql import DATETIME, VARCHAR
from sqlalchemy.orm import Mapped, mapped_column

from palimpsest_hub.models import Base, _binary_name, _now

IMAGE_ACTIONS = frozenset({"images:read", "images:publish"})
MAX_EXPIRY_DAYS = 90


class PalimpsestImageKey(Base):
    """Only the SHA-256 of the 32-byte secret is stored; the wire secret is shown once."""

    __tablename__ = "palimpsest_image_keys"
    id: Mapped[str] = mapped_column(CHAR(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(_binary_name(64), nullable=False, index=True)
    owner_user_id: Mapped[str] = mapped_column(_binary_name(64), nullable=False, index=True)
    secret_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    name: Mapped[str] = mapped_column(VARCHAR(128), nullable=False)
    actions: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=_now)
    expires_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
