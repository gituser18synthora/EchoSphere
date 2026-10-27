"""Identity confirmation modes — how a reply to the bot's identity question counts.

The bot asks "क्या मेरी बात Gaurav जी से हो रही है?"; the caller answers. Two
per-bot modes (``runtime_context_schemas.identity_confirmation_mode``):

- ``standard`` — an affirm-led reply with no negation and no other-person cue
  confirms: "haan", "haan ji", "ji", "ji haan", "haan boliye", "ji boliye",
  "haan bol raha hoon", "main hi bol raha hoon", "haan main hoon".
- ``strict`` — a reply that only invites the bot to go on ("haan boliye",
  "ji boliye", "haan bolo") earns ONE rephrased explicit re-ask ("Kya aap hi
  Gaurav ji bol rahe hain?"); any affirm to that explicit question confirms.
  A plain yes ("haan", "ji haan") or a reply that states the identity
  ("haan main Gaurav bol raha hoon") confirms immediately.

In both modes a negation, "galat number", someone speaking for the person
("main unka bhai bol raha hoon") or a different name never confirms.

Bots whose calls disclose account details behind the identity check (the
collections domain policy) default to strict; every other bot to standard.
The runtime applies the rule deterministically before the decision LLM
(voice_runtime.identity_confirmation); :func:`decision_prompt_rule` states the
same rule to the decision LLM, in the live state of every turn that answers
the identity question, so both agree.
"""

STANDARD = "standard"
STRICT = "strict"
IDENTITY_CONFIRMATION_MODES = (STANDARD, STRICT)


def default_identity_confirmation_mode(domain_policy: str | None) -> str:
    """Strict where identity gates disclosure (collections), else standard."""
    return STRICT if (domain_policy or "").strip() == "collections" else STANDARD


def resolve_identity_confirmation_mode(
    configured: str | None, domain_policy: str | None
) -> str:
    """The bot's configured mode, or the domain policy's default when unset."""
    value = str(configured or "").strip().lower()
    if value in IDENTITY_CONFIRMATION_MODES:
        return value
    return default_identity_confirmation_mode(domain_policy)


_DENIED_CUES = (
    " Denied only for a negation, 'galat number', someone answering for the "
    "person ('main unka bhai/beta bol raha hoon') or another person's name; a "
    "title or role alone ('doctor', 'sir') is not another person."
)


def decision_prompt_rule(mode: str, *, explicit_asked: bool = False) -> str:
    """The identity rule the decision LLM sees while the identity question is
    pending (appended to the live state's pending question — never to the
    static system prompt, where it shifted unrelated decisions)."""
    if mode == STRICT and not explicit_asked:
        return (
            " Identity rule (strict): a yes followed only by an invitation to "
            "go on ('haan boliye', 'ji boliye', 'haan bolo') is ambiguous — it "
            "does not say who is speaking. A plain yes ('haan', 'ji haan', "
            "'haan ji', 'haan sir/ma'am', 'haan ho rahi hai') or a yes saying "
            "it is them ('haan bol raha hoon', 'main hi hoon', 'haan main "
            "<name> bol raha hoon') is decision confirmed."
        )
    rule = (
        " Identity rule: a reply that says yes is decision confirmed (signal "
        "affirm), also when the yes is followed only by an invitation to go "
        "on ('haan boliye', 'ji boliye', 'haan bolo') or by saying it is them "
        "('haan bol raha hoon', 'main hi hoon')."
    )
    if explicit_asked:
        rule = (
            " The bot has already asked explicitly whether the caller IS that "
            "person." + rule
        )
    return rule + _DENIED_CUES


__all__ = [
    "IDENTITY_CONFIRMATION_MODES", "STANDARD", "STRICT",
    "decision_prompt_rule", "default_identity_confirmation_mode",
    "resolve_identity_confirmation_mode",
]
