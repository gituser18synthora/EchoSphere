"""Compliance-policy resolution: DB rows → immutable runtime snapshots.

Only ACTIVE policies whose ``effective_date`` has arrived are returned — a
draft or approved policy is compliance-owner work-in-progress and never
enforces. A failed lookup returns no policies (logged loudly and surfaced as
a ``compliance_policy_load_failed`` trigger by callers); the mandatory
guardrail floor is unaffected — it comes from the guardrail loader, which
fails closed independently.
"""

import logging
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select

from shared.db.mysql import get_sessionmaker
from shared.ids import new_id

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WordingTemplate:
    code: str
    language: str
    version: int
    text: str
    exact: bool = True


@dataclass(frozen=True)
class CompliancePolicySnapshot:
    policy_id: str
    code: str
    version: int
    name: str = ""
    regulator: str = ""
    jurisdiction: str = ""
    timezone: str = "UTC"
    applies_to: dict = field(default_factory=dict)
    calling_windows: tuple = ()
    contact_limits: dict = field(default_factory=dict)
    prohibited_conduct: tuple = ()
    waiver_rules: dict = field(default_factory=dict)
    escalation_rules: dict = field(default_factory=dict)
    wordings: tuple = ()

    def applies(self, *, purpose: str | None = None, channel: str | None = None,
                direction: str | None = None) -> bool:
        """Constraint semantics: a dimension restricts only when the policy
        names values for it AND the caller supplied a value. An unknown value
        (None) never exempts a call from an otherwise-matching policy."""
        spec = self.applies_to or {}
        for key, value in (("purposes", purpose), ("channels", channel),
                           ("directions", direction)):
            allowed = spec.get(key)
            if allowed and value is not None and value not in allowed:
                return False
        return True

    def effective_direction(self, direction: str | None) -> str | None:
        """The direction used for matching: the explicit one when the dialer
        supplied it, else the policy's configured assumption (e.g. a
        collections tenant whose campaigns are always outbound)."""
        if direction:
            return direction
        return (self.applies_to or {}).get("assume_direction")


def load_active_policies_sync(
    tenant_id: str | None, session=None, today: date | None = None
) -> tuple[CompliancePolicySnapshot, ...]:
    """Enforceable policies for a tenant (highest version per code)."""
    from shared.models import CompliancePolicy

    if not tenant_id:
        return ()
    own_session = session is None
    if own_session:
        session = get_sessionmaker()()
    try:
        today = today or date.today()
        rows = session.scalars(
            select(CompliancePolicy).where(
                CompliancePolicy.tenant_id == tenant_id,
                CompliancePolicy.status == "active",
                CompliancePolicy.is_deleted.is_(False),
            ).order_by(CompliancePolicy.code, CompliancePolicy.version)
        ).all()
        by_code: dict[str, CompliancePolicySnapshot] = {}
        for row in rows:
            if row.effective_date is not None and row.effective_date > today:
                continue
            by_code[row.code] = CompliancePolicySnapshot(
                policy_id=row.id,
                code=row.code,
                version=row.version,
                name=row.name,
                regulator=row.regulator or "",
                jurisdiction=row.jurisdiction or "",
                timezone=row.timezone or "UTC",
                applies_to=dict(row.applies_to or {}),
                calling_windows=tuple(row.calling_windows or ()),
                contact_limits=dict(row.contact_limits or {}),
                prohibited_conduct=tuple(row.prohibited_conduct or ()),
                waiver_rules=dict(row.waiver_rules or {}),
                escalation_rules=dict(row.escalation_rules or {}),
                wordings=tuple(
                    WordingTemplate(
                        code=w.code, language=w.language, version=w.version,
                        text=w.text, exact=bool(w.exact),
                    )
                    for w in (row.wordings or [])
                ),
            )
        return tuple(by_code.values())
    except Exception:  # noqa: BLE001 — logged loudly; guardrail floor unaffected
        logger.error(
            "compliance-policy lookup failed for tenant %s — no policy "
            "enforcement this call", tenant_id, exc_info=True,
        )
        return ()
    finally:
        if own_session:
            session.close()


def record_policy_trigger_sync(
    *,
    tenant_id: str | None,
    bot_id: str | None,
    session_id: str | None,
    rule: str,
    action: str,
    stage: str,
    outcome: str,
    detail: str = "",
    policy: CompliancePolicySnapshot | None = None,
    channel: str | None = None,
    session=None,
) -> None:
    """One tenant-scoped ledger row for a policy-level enforcement decision
    (calling-window block, contact-limit block, wording emission). Never
    raises — bookkeeping must not take down a call path."""
    from shared.models import GuardrailTrigger

    own_session = session is None
    if own_session:
        session = get_sessionmaker()()
    try:
        session.add(GuardrailTrigger(
            id=new_id("gt"),
            tenant_id=tenant_id,
            bot_id=bot_id,
            session_id=session_id,
            guardrail_id=None,
            guardrail_code=rule[:50],
            rule_name=(policy.name if policy else None),
            action=action,
            stage=stage,
            detail=(detail or "")[:300] or None,
            policy_code=policy.code if policy else None,
            policy_version=policy.version if policy else None,
            outcome=outcome,
            channel=channel,
        ))
        if own_session:
            session.commit()
        else:
            session.flush()
    except Exception:  # noqa: BLE001
        logger.warning("policy trigger persistence failed", exc_info=True)
        if own_session:
            session.rollback()
    finally:
        if own_session:
            session.close()
