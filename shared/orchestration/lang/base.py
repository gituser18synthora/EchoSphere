"""Language pack contract.

A pack holds the SURFACE FORMS of one language: lexicons (whole tokens), regex
fragments (alternatives without a leading ``|``) and per-script conventions
(letter ranges for word-end guards, sentence terminators). It carries no
domain meaning — what a phrase MEANS is a signal pack's business
(``shared/orchestration/signals``); a language pack only says how it is said.

Fragments may use ``{L}`` for "every registered script's letters" (word-end
guards across mixed-script utterances). Packs are plain data: adding a
language is a new module registered in ``shared/orchestration/lang``, never
an edit to the router or the engine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class LanguagePack:
    code: str
    label: str
    # Regex character-class body of the script's letters and combining marks
    # ("" for Latin, where ``\\w`` already works).
    letters: str = ""
    # Callers of this language often answer in short tokens: minimum token
    # length for a word to count as knowledge-topic vocabulary.
    short_token_floor: int = 3

    # ── lexicons (lower-case whole tokens) ──────────────────────────────────
    bare_yes: frozenset[str] = frozenset()
    bare_no: frozenset[str] = frozenset()
    fillers: frozenset[str] = frozenset()           # ignorable in a bare answer
    strip_fillers: frozenset[str] = frozenset()     # discourse words trimmed at the edges
    generic_answer_tokens: frozenset[str] = frozenset()  # never a CONTENT answer
    canonical_yes: frozenset[str] = frozenset()     # words that name a YES canonical
    canonical_no: frozenset[str] = frozenset()
    stop_tokens: frozenset[str] = frozenset()       # never a knowledge topic

    # ── regex fragments ─────────────────────────────────────────────────────
    affirm_tokens: tuple[str, ...] = ()             # bare confirmations
    leading_affirm_tokens: tuple[str, ...] = ()     # confirmations that may OPEN an utterance
    no_tokens: tuple[str, ...] = ()                 # bare negations
    courtesy_tails: tuple[str, ...] = ()            # "please", "thanks", …
    contradiction: tuple[str, ...] = ()             # words that cancel a leading yes
    question_markers: tuple[str, ...] = ()          # interrogatives, anywhere in a clause
    question_aux: tuple[str, ...] = ()              # clause-initial auxiliaries (English)
    question_subjects: tuple[str, ...] = ()         # subject words after an auxiliary
    clause_connectors: tuple[str, ...] = ()         # "waise", "and", "but", …
    sentence_terminators: str = ""                  # characters that end a sentence
    token_strip_extra: str = ""                     # punctuation stripped off tokens
    digits_restart: tuple[str, ...] = ()            # "start again", "dobara"
    digits_readback: tuple[str, ...] = ()           # "what did you note?"
    hangup_negation: tuple[str, ...] = ()           # "don't hang up" (negations win)
    hangup_patterns: tuple[str, ...] = ()           # full regexes, any one ends the call
    dnc_patterns: tuple[str, ...] = ()              # "never call me again"
    emergency: tuple[str, ...] = ()
    consent_refusal: tuple[str, ...] = ()           # "don't record"
    smalltalk: str = ""                             # whole-utterance smalltalk regex
    call_control_patterns: tuple[tuple[str, str], ...] = ()  # (regex, action)
    handoff_words: str = ""                         # "human|agent|…"
    kb_question_words: tuple[str, ...] = ()         # "what", "how", "can i", …
    # Extra alternatives this language contributes to a named SIGNAL pattern
    # ("hardship", "payment_intent", …) — the semantic pattern is owned by the
    # signal pack; the pack only adds how this language says it.
    signal_fragments: Mapping[str, str] = field(default_factory=dict)
