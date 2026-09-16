"""STT language-detection policy: when does a call auto-detect the caller's
language and when is the recognizer pinned to the bot's default language?

One rule, shared by the bot-config resolver, the voice runtime pipeline, the
Voice settings API and (mirrored) the Voice tab:

1. ``stt_language`` explicitly set (``hi-IN`` …) → always pinned to it.
2. ``stt_settings.auto_detect_language`` explicitly ``True``/``False`` → the
   user's choice wins.
3. Otherwise the DEFAULT is derived from the bot's effective languages:
   more than one configured language → auto-detect ON; a single language →
   OFF (pinned to the bot default, the reliable choice for short narrowband
   phone replies).

"Effective languages" = the bot's own ``bot_languages`` when it has any,
otherwise the tenant's ``default_languages`` (a bot without its own list
inherits the tenant's).

The persisted key is deliberately tri-state (absent / true / false): the
Voice tab only writes it when the user touches the control, so a fresh
multilingual bot keeps following the derived default while an explicit OFF
survives any later language change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

AUTO_DETECT_KEY = "auto_detect_language"

Source = Literal["explicit", "derived"]


@dataclass(frozen=True)
class AutoDetectDecision:
    enabled: bool
    source: Source
    #: What the rule would choose with no explicit value (shown in the UI).
    derived_default: bool
    #: The persisted value (``None`` = not set → derived).
    explicit: bool | None
    languages: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "value": self.explicit,
            "effective": self.enabled,
            "source": self.source,
            "derivedDefault": self.derived_default,
            "languages": list(self.languages),
        }


def effective_languages(
    bot_languages: Iterable[str] | None,
    tenant_default_languages: Iterable[str] | None = None,
) -> list[str]:
    """Bot-level languages when present, else the tenant's defaults.

    Order is preserved, duplicates and blanks dropped, so the first entry can
    double as the default call language.
    """
    def _clean(values: Iterable[str] | None) -> list[str]:
        out: list[str] = []
        for value in values or ():
            if not isinstance(value, str):
                continue
            code = value.strip()
            if code and code not in out:
                out.append(code)
        return out

    own = _clean(bot_languages)
    return own if own else _clean(tenant_default_languages)


def explicit_auto_detect(stt_settings: dict | None) -> bool | None:
    """The persisted tri-state: ``True``/``False`` when set, else ``None``.

    Only real booleans count — a stray string or ``None`` is "not set".
    """
    if not isinstance(stt_settings, dict):
        return None
    value = stt_settings.get(AUTO_DETECT_KEY)
    return value if isinstance(value, bool) else None


def derived_auto_detect_default(languages: Iterable[str] | None) -> bool:
    """More than one effective language → detect; otherwise pin."""
    return len(effective_languages(languages)) > 1


def resolve_auto_detect_language(
    stt_settings: dict | None,
    bot_languages: Iterable[str] | None,
    tenant_default_languages: Iterable[str] | None = None,
) -> AutoDetectDecision:
    languages = tuple(effective_languages(bot_languages, tenant_default_languages))
    derived = len(languages) > 1
    explicit = explicit_auto_detect(stt_settings)
    if explicit is None:
        return AutoDetectDecision(
            enabled=derived, source="derived", derived_default=derived,
            explicit=None, languages=languages,
        )
    return AutoDetectDecision(
        enabled=explicit, source="explicit", derived_default=derived,
        explicit=explicit, languages=languages,
    )


def stt_language_mode(
    stt_language: str | None,
    decision: AutoDetectDecision,
    default_language: str | None,
) -> tuple[str, str | None]:
    """Resolve what the recognizer is told: ``("pinned", locale)`` or
    ``("auto", None)``.

    An explicit STT language always pins (``"unknown"`` is the provider
    spelling of auto-detect and is treated as blank).
    """
    explicit_locale = (stt_language or "").strip()
    if explicit_locale and explicit_locale.lower() != "unknown":
        return "pinned", explicit_locale
    if decision.enabled:
        return "auto", None
    locale = (default_language or "").strip() or None
    return ("pinned", locale) if locale else ("auto", None)
