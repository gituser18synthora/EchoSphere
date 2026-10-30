"""Deterministic collections-compliance enforcement.

Policies are DATA (``compliance_policies`` rows) — calling windows, contact
limits, prohibited conduct, waiver rules and legally-exact wordings — loaded
per tenant at the enforcement points and applied deterministically. Only
``status='active'`` policies whose effective date has arrived are enforced;
draft/approved policies exist for compliance-owner review and never gate a
call. Prompt guidance remains a conversational layer on top, never the
enforcement mechanism.
"""

from shared.compliance.calling_hours import CallWindowDecision, check_calling_window
from shared.compliance.contact_limits import (
    check_and_count_contact,
)
from shared.compliance.policy import (
    CompliancePolicySnapshot,
    WordingTemplate,
    load_active_policies_sync,
    record_policy_trigger_sync,
)
from shared.compliance.wording import resolve_wording, substitute_wordings

__all__ = [
    "CallWindowDecision",
    "CompliancePolicySnapshot",
    "WordingTemplate",
    "check_and_count_contact",
    "check_calling_window",
    "load_active_policies_sync",
    "record_policy_trigger_sync",
    "resolve_wording",
    "substitute_wordings",
]
