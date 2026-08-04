"""Runtime context — tenant-defined user/customer details for any domain.

The platform used to know exactly one context shape (the loan-collection
columns in collections_models). These tables replace that assumption with
configuration:

- :class:`RuntimeContextSchema` — one row per bot: WHICH fields exist
  (tenant-defined, any domain — patients, properties, loans, leads), where
  live values come from (a configured User Details API or a manual test
  payload), how sensitive values are masked, and how the prompt should treat
  missing information.
- :class:`RuntimeContextRecord` — optional stored per-customer payloads
  (arbitrary validated JSON), matched at call time by phone or customer_ref
  exactly like the legacy collection contexts.

Typing rules (part of the API contract):
- payloads are stored EXACTLY as validated — numbers stay numbers, booleans
  stay booleans, nested objects/arrays survive round-trips untouched;
- unknown values are absent keys or JSON null, never "" or 0;
- fields flagged sensitive are stored in full here but only ever leave the
  server masked (serializers + shared.runtime_context build masked views).

The legacy loan table (customer_contexts) remains a compatibility source:
bots without a schema row keep their current behavior bit-for-bit.
"""

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import (
    ID_LEN,
    AuditByMixin,
    Base,
    SoftDeleteMixin,
    TenantOwnedMixin,
    TimestampMixin,
)

# Where live calls get their context payload from.
CONTEXT_SOURCE_MODES = ("api", "manual")

# Domain behavior packs a tenant can opt a bot into. "generic" is pure
# prompt/workflow-driven behavior; "collections" additionally activates the
# deterministic collection-call policy (identity gating, dispute/claim
# blockers, dispositions). New domains are configuration, not code.
DOMAIN_POLICIES = ("generic", "collections")

# Field types the schema editor can declare. object/array accept any JSON of
# that container shape — nested validation is the tenant's API contract.
CONTEXT_FIELD_TYPES = (
    "string", "number", "integer", "boolean", "date", "object", "array",
)


class RuntimeContextSchema(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin,
                           TenantOwnedMixin):
    __tablename__ = "runtime_context_schemas"
    __table_args__ = (
        UniqueConstraint("bot_id", name="uq_runtime_context_schema_bot"),
        Index("ix_runtime_context_schemas_tenant_bot", "tenant_id", "bot_id"),
    )

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    bot_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("voice_bots.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), default="User details", nullable=False)
    # api → the User Details API below feeds live calls; manual → the stored
    # test payload does (pre-integration and Testing Studio runs).
    source_mode: Mapped[str] = mapped_column(String(20), default="manual", nullable=False)
    api_connection_id: Mapped[str | None] = mapped_column(
        String(ID_LEN), ForeignKey("api_connections.id"), nullable=True
    )
    # Dot-path into the API response body where the context object lives
    # (e.g. "data.customer"); NULL/"" means the whole response body.
    response_path: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Tenant-defined field definitions:
    # [{key, label?, type, required?, sensitive?, maskKeep?, description?, example?}]
    fields: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Whether payload keys outside `fields` are accepted (they pass through
    # untyped). Rejecting them turns the schema into a closed contract.
    allow_additional: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Manual Test JSON — behaves exactly as if the User Details API returned it.
    test_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Tenant instruction appended to the prompt's context section describing
    # how the bot should handle values that are missing on a call.
    missing_value_policy: Mapped[str | None] = mapped_column(String(500), nullable=True)
    domain_policy: Mapped[str] = mapped_column(String(30), default="generic", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)


class RuntimeContextRecord(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin,
                           TenantOwnedMixin):
    __tablename__ = "runtime_context_records"
    __table_args__ = (
        Index("ix_runtime_context_records_bot_phone", "bot_id", "phone"),
        Index("ix_runtime_context_records_tenant_bot", "tenant_id", "bot_id"),
    )

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    bot_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("voice_bots.id"), nullable=False, index=True
    )
    # External CRM/LMS/HIS reference — the tenant's stable customer id.
    customer_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # E.164-ish dialable number; calls match on the trailing 10 digits.
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # The tenant-shaped payload, validated against the bot's schema at write
    # time and stored with types intact.
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Mutable state the runtime records back after a call (verification,
    # dispositions, domain flags) — kept apart from tenant-owned `data`.
    call_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
