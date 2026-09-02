"""Runtime identity derived from the selected TTS voice.

Voice names and genders come from the provider voice catalog.  This module
contains no speaker-name allowlist: changing a bot's selected voice changes the
identity seen by the prompt on the next call (the voice-settings save already
invalidates the bot-config cache).

The identity has two jobs:

* expose the selected catalog display name through prompt placeholders; and
* tell the LLM which first-person grammatical gender belongs to *itself* in
  gendered languages such as Hindi/Hinglish.

Generated text is not rewritten: morphological agreement there belongs in the
model instruction.  Author-written first-person lines (especially the fixed
greeting, which bypasses the model) are normalized separately so changing the
selected catalog voice also changes forms such as ``raha/rahi``.
"""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class VoiceIdentity:
    name: str = ""
    gender: str = "neutral"


# Canonical prompt variables plus the spelling used in an early tenant prompt.
# The compatibility alias can be removed after that prompt is migrated.
VOICE_IDENTITY_CONTEXT_KEYS = (
    "assistant_voice_name",
    "assistant_voice_gender",
    "voice_speaker_name",
    "voice_speaker_gender",
    "voice_bot_spiker_name",
)


def _gender(value: object) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in ("male", "female") else "neutral"


def resolve_language_engine(
    language_map: dict | None,
    locale: str | None,
    default_engine: dict | None,
) -> dict:
    """Resolve exact locale -> base language -> default TTS engine.

    This is the single lookup rule shared by the streaming router and voice
    identity. A mapping must be an engine object; legacy strings and malformed
    entries safely fall through instead of producing a partial identity.
    """
    mapping = language_map or {}
    code = str(locale or "").strip()
    if code:
        exact = mapping.get(code)
        if isinstance(exact, dict):
            return exact
        base = re.split(r"[-_]", code, maxsplit=1)[0]
        if base and base != code:
            fallback = mapping.get(base)
            if isinstance(fallback, dict):
                return fallback
    return default_engine or {}


def resolve_tts_engine(tts: dict | None, locale: str | None) -> dict:
    """Resolve the engine a configured runtime will actually synthesize with."""
    tts = tts or {}
    if tts.get("streaming") is False:
        return tts
    return resolve_language_engine(tts.get("language_map"), locale, tts)


def active_voice_identity(tts: dict | None, locale: str | None) -> VoiceIdentity:
    """Return the voice the TTS pipeline actually uses for ``locale``.

    Streaming engines can carry a per-language override.  A non-streaming
    primary engine cannot switch voices mid-call, so its default identity wins
    even when a stale language map is present.
    """
    engine = resolve_tts_engine(tts, locale)
    name = str(engine.get("voice_name") or engine.get("voice") or "").strip()
    return VoiceIdentity(name=name, gender=_gender(engine.get("voice_gender")))


def voice_context_values(identity: VoiceIdentity) -> dict[str, str]:
    """System-trusted placeholders for authored prompts and greetings."""
    if not identity.name and identity.gender == "neutral":
        return {}
    values = {
        "assistant_voice_gender": identity.gender,
        "voice_speaker_gender": identity.gender,
    }
    if identity.name:
        values["assistant_voice_name"] = identity.name
        values["voice_speaker_name"] = identity.name
        # Backward compatible with the placeholder supplied in the request.
        values["voice_bot_spiker_name"] = identity.name
    return values


def voice_identity_state(identity: VoiceIdentity) -> dict[str, str]:
    """Small trusted state block for LLM paths that do not render prompts.

    The Stage-A conversation decision model receives structured live state
    rather than the full runtime system prompt. Keeping these canonical keys
    here ensures it uses the same catalog-derived identity as Stage B and the
    Testing Studio, without teaching any call site about individual voices.
    """
    state = {"assistant_voice_gender": identity.gender}
    if identity.name:
        state["assistant_voice_name"] = identity.name
    return state


def voice_identity_instruction(
    identity: VoiceIdentity, locale: str | None = None
) -> str:
    """System-prompt suffix enforcing the selected speaker's own grammar.

    No provider/voice name maps to a gender here; the database catalog is the
    sole source of that metadata.  Caller gender never affects this rule.

    ``locale`` is the language the generated text must use.  Hindi morphology
    examples are useful when Hindi/Hinglish is the target, but they are a
    strong competing language signal for small models when the target is
    English (the model can copy the examples instead of translating the
    workflow line).  Keep the examples target-language scoped; callers may
    switch language on any turn without the speaker-gender rule switching the
    response back to Hindi.

    A missing locale preserves the original generic instruction for callers
    that do not yet have a resolved response language.
    """
    if not identity.name and identity.gender == "neutral":
        return ""

    lines = ["\n\n# Active TTS speaker identity (runtime-selected)"]
    if identity.name:
        lines.append(f"- Selected speaker name: {identity.name}")
    lines.append(f"- `assistant_voice_gender = {identity.gender}`")
    if identity.gender in ("male", "female"):
        lines.extend([
            f"- The voice catalog marks this speaker as {identity.gender}.",
            f"- For EVERY first-person reference made by the assistant, use "
            f"grammatically {identity.gender} forms in any language where "
            "speaker gender affects verbs, auxiliaries, participles, adjectives "
            "or other agreement. This runtime value overrides contrary gender "
            "forms in persona text, authored examples and conversation history.",
            "- This is the bot speaker's gender only. Never infer it from, or "
            "change it to match, the caller's gender.",
        ])
        target_base = re.split(r"[-_]", str(locale or ""), maxsplit=1)[0].lower()
        if locale and target_base != "hi":
            lines.append(
                f"- The required response language is {locale}. Do not switch "
                "languages or introduce foreign-language words merely to "
                "express the speaker's gender."
            )
        elif identity.gender == "female":
            lines.append(
                "- Hindi/Hinglish self-reference must use feminine forms such "
                "as ‘मैं समझ सकती हूँ’, ‘मैं करती हूँ’, ‘मैं बताती हूँ’, "
                "‘मैं चाहती हूँ’ and ‘मैं आपकी मदद कर सकती हूँ’; never use "
                "masculine सकता/करता/बताता/समझता/चाहता for the assistant."
            )
        else:
            lines.append(
                "- Hindi/Hinglish self-reference must use masculine forms such "
                "as ‘मैं समझ सकता हूँ’, ‘मैं करता हूँ’, ‘मैं बताता हूँ’, "
                "‘मैं चाहता हूँ’ and ‘मैं आपकी मदद कर सकता हूँ’; never use "
                "feminine सकती/करती/बताती/समझती/चाहती for the assistant."
            )
    else:
        lines.append(
            "- The catalog does not specify a male/female speaker gender. Use "
            "natural gender-neutral self-references wherever the language allows."
        )
    return "\n".join(lines)


# Hindi routinely drops the subject pronoun ("कल call कर रही हूँ") — the
# first-person auxiliary हूँ/hoon is equally definitive evidence, so gender
# agreement is applied to those sentences too.
_FIRST_PERSON = re.compile(
    r"(?<!\w)(?:मैं|मै|main|mai|hoon|hun)(?!\w)|हूँ|हूं", re.IGNORECASE
)
_SENTENCE_PARTS = re.compile(r"(?<=[.!?।])|\n")

# These are grammatical forms, not speaker names.  Voice → gender remains
# entirely catalog-driven; adding or renaming a provider voice needs no code
# change.  The replacements are deliberately limited to text containing a
# first-person pronoun so a greeting such as "क्या आप तैयार हैं?" never has
# the customer's grammar altered.
_DEVANAGARI_FORMS = {
    "male": (
        (r"(?<!\w)रही(?!\w)", "रहा"),
        (r"(?<!\w)चुकी(?!\w)", "चुका"),
        (r"(?<!\w)(?:गई|गयी)(?!\w)", "गया"),
        (r"(?<!\w)करती(?!\w)", "करता"),
        (r"(?<!\w)सकती(?!\w)", "सकता"),
        (r"(?<!\w)बैठी(?!\w)", "बैठा"),
        (r"(?<!\w)खड़ी(?!\w)", "खड़ा"),
        (r"(?<!\w)वाली(?!\w)", "वाला"),
        (r"(?<!\w)देती(?!\w)", "देता"),
        (r"(?<!\w)लेती(?!\w)", "लेता"),
        (r"(?<!\w)जाती(?!\w)", "जाता"),
        (r"(?<!\w)पाती(?!\w)", "पाता"),
        (r"(?<!\w)जानती(?!\w)", "जानता"),
        (r"(?<!\w)लगती(?!\w)", "लगता"),
        (r"(?<!\w)बोलती(?!\w)", "बोलता"),
        (r"(?<!\w)बताती(?!\w)", "बताता"),
        (r"(?<!\w)चाहती(?!\w)", "चाहता"),
        (r"(?<!\w)समझती(?!\w)", "समझता"),
        (r"(?<!\w)रखती(?!\w)", "रखता"),
        (r"(?<!\w)थी(?!\w)", "था"),
        (r"([\u0900-\u097f]+)ूँगी(?!\w)", r"\1ूँगा"),
    ),
    "female": (
        (r"(?<!\w)रहा(?!\w)", "रही"),
        (r"(?<!\w)चुका(?!\w)", "चुकी"),
        (r"(?<!\w)गया(?!\w)", "गई"),
        (r"(?<!\w)करता(?!\w)", "करती"),
        (r"(?<!\w)सकता(?!\w)", "सकती"),
        (r"(?<!\w)बैठा(?!\w)", "बैठी"),
        (r"(?<!\w)खड़ा(?!\w)", "खड़ी"),
        (r"(?<!\w)वाला(?!\w)", "वाली"),
        (r"(?<!\w)देता(?!\w)", "देती"),
        (r"(?<!\w)लेता(?!\w)", "लेती"),
        (r"(?<!\w)जाता(?!\w)", "जाती"),
        (r"(?<!\w)पाता(?!\w)", "पाती"),
        (r"(?<!\w)जानता(?!\w)", "जानती"),
        (r"(?<!\w)लगता(?!\w)", "लगती"),
        (r"(?<!\w)बोलता(?!\w)", "बोलती"),
        (r"(?<!\w)बताता(?!\w)", "बताती"),
        (r"(?<!\w)चाहता(?!\w)", "चाहती"),
        (r"(?<!\w)समझता(?!\w)", "समझती"),
        (r"(?<!\w)रखता(?!\w)", "रखती"),
        (r"(?<!\w)था(?!\w)", "थी"),
        (r"([\u0900-\u097f]+)ूँगा(?!\w)", r"\1ूँगी"),
    ),
}

_ROMAN_FORMS = {
    "male": (
        ("rahi", "raha"),
        ("chuki", "chuka"),
        ("gayi", "gaya"),
        ("karti", "karta"),
        ("sakti", "sakta"),
        ("baithi", "baitha"),
        ("khadi", "khada"),
        ("wali", "wala"),
        ("dekhti", "dekhta"),
        ("samajhti", "samajhta"),
        ("bolti", "bolta"),
        ("batati", "batata"),
        ("chahti", "chahta"),
    ),
    "female": (
        ("raha", "rahi"),
        ("chuka", "chuki"),
        ("gaya", "gayi"),
        ("karta", "karti"),
        ("sakta", "sakti"),
        ("baitha", "baithi"),
        ("khada", "khadi"),
        ("wala", "wali"),
        ("dekhta", "dekhti"),
        ("samajhta", "samajhti"),
        ("bolta", "bolti"),
        ("batata", "batati"),
        ("chahta", "chahti"),
    ),
}


def _match_case(replacement: str, source: str) -> str:
    if source.isupper():
        return replacement.upper()
    if source[:1].isupper():
        return replacement.capitalize()
    return replacement


def _adapt_first_person_part(part: str, gender: str) -> str:
    if not _FIRST_PERSON.search(part):
        return part

    for pattern, replacement in _DEVANAGARI_FORMS[gender]:
        part = re.sub(pattern, replacement, part)
    for source, replacement in _ROMAN_FORMS[gender]:
        part = re.sub(
            rf"(?<![A-Za-z]){source}(?![A-Za-z])",
            lambda match, value=replacement: _match_case(value, match.group(0)),
            part,
            flags=re.IGNORECASE,
        )

    # Romanized first-person futures: karunga/karungi, lunga/lungi, etc.
    if gender == "female":
        part = re.sub(
            r"(?<![A-Za-z])([A-Za-z]+)(u|oo)nga(?![A-Za-z])",
            lambda match: f"{match.group(1)}{match.group(2)}ngi",
            part,
            flags=re.IGNORECASE,
        )
    else:
        part = re.sub(
            r"(?<![A-Za-z])([A-Za-z]+)(u|oo)ngi(?![A-Za-z])",
            lambda match: f"{match.group(1)}{match.group(2)}nga",
            part,
            flags=re.IGNORECASE,
        )
    return part


def adapt_authored_speaker_grammar(text: str, identity: VoiceIdentity) -> str:
    """Align fixed first-person Hindi/Hinglish text with the active voice.

    Fixed greetings do not pass through the LLM, so its gender instruction
    cannot correct an author-entered ``main ... bol raha hoon``.  This narrow
    normalization handles that deterministic path in both directions.  A
    neutral/unknown catalog gender leaves authored text untouched.
    """
    if identity.gender not in ("male", "female"):
        return text
    return "".join(
        _adapt_first_person_part(part, identity.gender)
        for part in _SENTENCE_PARTS.split(text)
    )
