"""Bounded monitoring history and collector coordination, separate from business data."""

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, ID_LEN

# Lease comparisons require the same precision as the application's UTC clock.
# MySQL's default DATETIME(0) rounds a freshly inserted lease into the future.
_TIMESTAMP = DateTime().with_variant(DATETIME(fsp=6), "mysql")


class MonitoringSample(Base):
    __tablename__ = "monitoring_samples"
    __table_args__ = (
        Index("ix_monitoring_samples_service_time", "service_key", "checked_at"),
        Index("ix_monitoring_samples_time", "checked_at"),
    )
    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    service_key: Mapped[str] = mapped_column(String(96), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(_TIMESTAMP, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)


class MonitoringServiceState(Base):
    __tablename__ = "monitoring_service_states"
    service_key: Mapped[str] = mapped_column(String(96), primary_key=True)
    last_status: Mapped[str] = mapped_column(String(20), nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    alert_id: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(_TIMESTAMP, nullable=False)


class MonitoringCollectorLease(Base):
    __tablename__ = "monitoring_collector_leases"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime] = mapped_column(_TIMESTAMP, nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(_TIMESTAMP, nullable=True)
