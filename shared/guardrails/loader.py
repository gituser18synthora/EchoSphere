"""Server-side resolution of a tenant's effective guardrails.

Effective guardrails = every enabled MANDATORY platform guardrail (applies to
all tenants, cannot be weakened by industry, tenant or bot) ∪ the enabled
guardrails linked to the tenant's assigned profile. Resolution follows the
``tenant_flags`` pattern: resolved from the database at the moment it matters,
never trusted from a client payload, and FAIL CLOSED — a broken lookup falls
back to the built-in ``MANDATORY_FLOOR`` so the platform-critical protections
(PII redaction, secret-leakage prevention, unsafe-tool blocking,
prompt-injection protection) are enforced even when the control plane is
unreachable.

A deactivated profile keeps enforcing for tenants already assigned to it —
deactivation blocks NEW assignments only, it must never silently weaken a
live tenant.
"""

import logging
from dataclasses import dataclass, field

from sqlalchemy import select

from shared.db.mysql import get_sessionmaker
from shared.ids import new_id

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GuardrailRule:
    code: str
    name: str
    action: str  # block | flag | redact
    category: str = ""
    mandatory: bool = False
    guardrail_id: str | None = None


@dataclass(frozen=True)
class EffectiveGuardrails:
    tenant_id: str | None = None
    profile_id: str | None = None
    profile_code: str | None = None
    profile_version: int | None = None
    rules: tuple[GuardrailRule, ...] = ()
    # True when the DB lookup failed and only the built-in floor is active.
    degraded: bool = False

    def rule(self, code: str) -> GuardrailRule | None:
        for r in self.rules:
            if r.code == code:
                return r
        return None

    def has(self, code: str) -> bool:
        return self.rule(code) is not None


# Built-in floor: the mandatory platform rules with their canonical actions.
# Applied when the database is unreachable AND unioned into every resolution,
# so deleting/disabling a mandatory row can never switch the protection off.
MANDATORY_FLOOR: tuple[GuardrailRule, ...] = (
    GuardrailRule("pii_redaction", "PII redaction in transcripts", "redact",
                  category="Privacy", mandatory=True),
    GuardrailRule("secret_leakage_prevention",
                  "Secret & credential leakage prevention", "redact",
                  category="Privacy", mandatory=True),
    GuardrailRule("unsafe_tool_call_block", "Unsafe tool-call blocking", "block",
                  category="Security", mandatory=True),
    GuardrailRule("prompt_injection_protection", "Prompt-injection protection",
                  "flag", category="Security", mandatory=True),
)

_FLOOR_ONLY = EffectiveGuardrails(rules=MANDATORY_FLOOR, degraded=True)


def load_effective_guardrails_sync(
    tenant_id: str | None, bot_id: str | None = None, session=None
) -> EffectiveGuardrails:
    """The effective guardrails for a tenant (and optionally a specific bot);
    the mandatory floor on any failure.

    Profile resolution: the bot's explicit ``guardrail_profile_id`` when set,
    else the tenant's default profile — so a bot without an explicit profile
    always inherits, and an explicit assignment is unaffected by tenant-
    default changes. Mandatory rules apply regardless of either.

    Sync (SQLAlchemy) — async callers wrap with ``asyncio.to_thread`` like the
    other call-start lookups. Pass ``session`` to reuse an open one.
    """
    from shared.models import (
        Guardrail,
        GuardrailProfile,
        GuardrailProfileRule,
        Tenant,
        VoiceBot,
    )

    own_session = session is None
    if own_session:
        session = get_sessionmaker()()
    try:
        rules: dict[str, GuardrailRule] = {}

        for g in session.scalars(
            select(Guardrail).where(
                Guardrail.is_mandatory.is_(True),
                Guardrail.enabled.is_(True),
                Guardrail.is_deleted.is_(False),
                Guardrail.code.is_not(None),
            )
        ):
            rules[g.code] = GuardrailRule(
                code=g.code, name=g.name, action=g.enforcement,
                category=g.category or "", mandatory=True, guardrail_id=g.id,
            )

        profile = None
        if tenant_id:
            profile_id = None
            if bot_id:
                profile_id = session.scalar(
                    select(VoiceBot.guardrail_profile_id).where(
                        VoiceBot.id == bot_id,
                        VoiceBot.tenant_id == tenant_id,  # cross-tenant ids resolve nothing
                        VoiceBot.is_deleted.is_(False),
                    )
                )
            if not profile_id:
                profile_id = session.scalar(
                    select(Tenant.guardrail_profile_id).where(
                        Tenant.id == tenant_id, Tenant.is_deleted.is_(False)
                    )
                )
            if profile_id:
                profile = session.get(GuardrailProfile, profile_id)
            if profile is not None and not profile.is_deleted:
                linked = session.execute(
                    select(Guardrail)
                    .join(GuardrailProfileRule,
                          GuardrailProfileRule.guardrail_id == Guardrail.id)
                    .where(
                        GuardrailProfileRule.profile_id == profile.id,
                        Guardrail.enabled.is_(True),
                        Guardrail.is_deleted.is_(False),
                        Guardrail.code.is_not(None),
                    )
                ).scalars()
                for g in linked:
                    if g.code not in rules:  # mandatory wins on overlap
                        rules[g.code] = GuardrailRule(
                            code=g.code, name=g.name, action=g.enforcement,
                            category=g.category or "", mandatory=False,
                            guardrail_id=g.id,
                        )
            else:
                profile = None

        # The floor is a guarantee, not a default: mandatory codes missing
        # from the DB (row deleted, code cleared) are still enforced.
        for floor_rule in MANDATORY_FLOOR:
            rules.setdefault(floor_rule.code, floor_rule)

        return EffectiveGuardrails(
            tenant_id=tenant_id,
            profile_id=profile.id if profile is not None else None,
            profile_code=profile.code if profile is not None else None,
            profile_version=profile.version if profile is not None else None,
            rules=tuple(rules.values()),
        )
    except Exception:  # noqa: BLE001 — fail closed onto the built-in floor
        logger.warning(
            "effective-guardrail lookup failed for tenant %s — enforcing the "
            "mandatory floor only", tenant_id, exc_info=True,
        )
        return _FLOOR_ONLY
    finally:
        if own_session:
            session.close()


def persist_triggers_sync(
    hits,
    *,
    tenant_id: str | None,
    bot_id: str | None = None,
    session_id: str | None = None,
    channel: str | None = None,
    effective: EffectiveGuardrails | None = None,
    session=None,
) -> int:
    """Write GuardrailTrigger rows (+ bump ``triggers_30d``) for a batch of
    engine hits. Returns the number of rows written; never raises — trigger
    bookkeeping must not take down a call."""
    from shared.models import Guardrail, GuardrailTrigger

    hits = list(hits)
    if not hits:
        return 0
    own_session = session is None
    if own_session:
        session = get_sessionmaker()()
    try:
        for hit in hits:
            session.add(GuardrailTrigger(
                id=new_id("gt"),
                tenant_id=tenant_id,
                bot_id=bot_id,
                session_id=session_id,
                guardrail_id=hit.rule.guardrail_id,
                guardrail_code=hit.rule.code,
                rule_name=hit.rule.name,
                action=hit.action,
                stage=hit.stage,
                detail=(hit.detail or "")[:300] or None,
                profile_id=effective.profile_id if effective else None,
                profile_version=effective.profile_version if effective else None,
                policy_code=getattr(hit, "policy_code", None),
                policy_version=getattr(hit, "policy_version", None),
                outcome=getattr(hit, "outcome", None),
                channel=channel,
            ))
            if hit.rule.guardrail_id:
                row = session.get(Guardrail, hit.rule.guardrail_id)
                if row is not None:
                    row.triggers_30d = (row.triggers_30d or 0) + 1
        if own_session:
            session.commit()
        else:
            session.flush()
        return len(hits)
    except Exception:  # noqa: BLE001
        logger.warning("guardrail trigger persistence failed", exc_info=True)
        if own_session:
            session.rollback()
        return 0
    finally:
        if own_session:
            session.close()
