"""Durable idle auto-shelve state for `lifecycle="dev"` instances.

One row per dev instance. It holds the persisted session/grace state machine, the hash of the
guest's session-report capability, the last accepted guest report, and a reference to the
requester-created Keystone Trust that lets the Hub shelve the server without the owner's token.
Nothing here is a credential: the Trust is an id plus verified scope, and the capability is a hash.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, String
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from palimpsest_hub.models import Base


class PalimpsestHubInstanceLifecycle(Base):
    __tablename__ = "palimpsest_hub_instance_lifecycle"

    instance_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # SessionMachine.to_state(); times are wall-clock epoch seconds so they survive restarts.
    machine_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    session_capability_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Only the parsed session identities of the last accepted guest report; never raw probe text.
    reported_sessions_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    report_received_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
    # Verified Keystone Trust reference: trust_id, project_id, trustor/trustee user ids, role_names.
    trust_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    trust_expires_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
    # Set when the instance is being deleted: the worker revokes the trust and stops observing.
    retired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_error: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=lambda: datetime.now(UTC))
