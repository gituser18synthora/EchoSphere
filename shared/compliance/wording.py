"""Legally-exact wording templates.

Authored content (workflow messages, scripted phrases, greetings) references
a template as ``{{wording:code}}``. Substitution inserts the approved text
VERBATIM — the LLM never generates or paraphrases it (wordings flow through
the fixed-phrase speech path, not through generation) — and reports each use
so the call record pins the exact template version spoken.

Language resolution: exact locale ("hi-IN") → base language ("hi") → "en".
Within a language, the highest version wins (templates are immutable; a
correction is a new version).
"""

import logging
import re
from collections.abc import Callable, Iterable

from shared.compliance.policy import CompliancePolicySnapshot, WordingTemplate

logger = logging.getLogger(__name__)

_WORDING_RE = re.compile(r"\{\{\s*wording:([A-Za-z0-9_.-]{1,60})\s*\}\}")


def resolve_wording(
    policies: Iterable[CompliancePolicySnapshot],
    code: str,
    language: str | None = None,
) -> tuple[WordingTemplate, CompliancePolicySnapshot] | None:
    """The best (language, highest-version) template for ``code``."""
    locale = (language or "en").strip()
    base = locale.split("-")[0].lower()
    best: tuple[int, WordingTemplate, CompliancePolicySnapshot] | None = None
    for policy in policies:
        for w in policy.wordings:
            if w.code != code:
                continue
            wl = w.language.lower()
            if wl == locale.lower():
                rank = 3
            elif wl == base:
                rank = 2
            elif wl == "en":
                rank = 1
            else:
                continue
            key = (rank * 1_000_000) + w.version
            if best is None or key > best[0]:
                best = (key, w, policy)
    if best is None:
        return None
    return best[1], best[2]


def substitute_wordings(
    text: str,
    policies: Iterable[CompliancePolicySnapshot],
    language: str | None = None,
    on_use: Callable[[WordingTemplate, CompliancePolicySnapshot], None] | None = None,
) -> str:
    """Replace every ``{{wording:code}}`` reference with the approved text.

    An unresolvable reference is REMOVED (never spoken as a raw placeholder,
    never improvised) and logged — the surrounding authored sentence still
    plays, so the call degrades to less content rather than wrong content.
    """
    if not text or "{{" not in text:
        return text
    policies = tuple(policies)

    def _replace(match: re.Match) -> str:
        code = match.group(1)
        resolved = resolve_wording(policies, code, language)
        if resolved is None:
            logger.warning("no approved wording for '%s' — reference dropped", code)
            return ""
        template, policy = resolved
        if on_use is not None:
            try:
                on_use(template, policy)
            except Exception:  # noqa: BLE001 — reporting must not break speech
                pass
        return template.text

    return _WORDING_RE.sub(_replace, text)
