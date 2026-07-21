"""Declarative base and shared column mixins."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

ID_LEN = 40


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AuditByMixin:
    created_by: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)


class SoftDeleteMixin:
    """Soft delete — hard deletes are blocked while ALLOW_HARD_DELETE=false."""

    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_by: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)


class TenantOwnedMixin:
    """Row belongs to a tenant; every query on these tables must filter tenant_id."""

    tenant_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("tenants.id"), nullable=False, index=True
    )
