"""Signal packs — the SEMANTIC vocabulary of caller utterances.

A signal is a context-free meaning the workflow layer reasons about
("refusal", "callback", "hardship"…). Each :class:`SignalSpec` owns its base
pattern (written for the platform's Hinglish/English baseline) plus the flags
the engine consults (may it START a workflow, may it fall back to a literal
edge token, does it park a turn off-script, is it a yes/no answer). Language
packs add how OTHER languages say the same thing
(:attr:`LanguagePack.signal_fragments`); other signal packs may extend a
signal with domain wording (``SignalPack.extends``).

Packs: ``core`` (conversation control, always on) and domain packs such as
``collections`` (lending / repayment). Today every registered pack is active
for every bot — identical to the single hard-coded list this replaced; a bot
profile may later restrict the set.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from shared.orchestration import lang as _lang


@dataclass(frozen=True)
class SignalSpec:
    name: str
    # Lower runs first: the first matching signal wins (ORDER MATTERS — a
    # complaint about the conversation outranks a refusal it may contain).
    priority: int
    # Base regex (Hinglish + English) or a template callable receiving the
    # composition context (see ``Composer``) for language-spliced shapes.
    pattern: str | None = None
    template: object = None
    flags: int = re.I
    # Engine semantics
    entry: bool = False            # the utterance that STARTED a workflow may be consumed by its first hub
    literal_fallback: bool = False  # may still advance via a literal edge token when no edge names the signal
    off_script: bool = False       # parks the turn for the brain when the node has no edge for it
    yes_no: bool = False           # a bare yes/no answer
    generic_answer: bool = False   # never a CONTENT answer for lookahead hubs
    compatible: tuple[str, ...] = ()  # edges naming these signals also accept this one


@dataclass(frozen=True)
class SignalPack:
    name: str
    specs: tuple[SignalSpec, ...] = ()
    # signal name → extra alternatives (no leading '|') appended to a spec
    # owned by another pack.
    extends: Mapping[str, str] = field(default_factory=dict)
    # Domain words that make a question-shaped utterance a knowledge lookup.
    knowledge_terms: tuple[str, ...] = ()


class Composer:
    """Language-side helpers available to pattern templates."""

    def __init__(self, codes: Iterable[str] | None):
        self.codes = tuple(codes) if codes is not None else None

    def alternatives(self, attr: str) -> str:
        return _lang.alternatives(attr, self.codes)

    def alternatives_of(self, attr: str, only: Iterable[str]) -> str:
        return _lang.alternatives(attr, tuple(only))

    def letters(self) -> str:
        return _lang.letters(self.codes)

    def fragments(self, signal: str) -> str:
        return _lang.fragments(signal, self.codes)

    def other_codes(self, baseline: Iterable[str]) -> tuple[str, ...]:
        base = set(baseline)
        return tuple(p.code for p in _lang.packs(self.codes) if p.code not in base)


_REGISTRY: dict[str, SignalPack] = {}
_ORDER: list[str] = []
_cache: dict[tuple, object] = {}


def register(pack: SignalPack) -> SignalPack:
    if pack.name not in _REGISTRY:
        _ORDER.append(pack.name)
    _REGISTRY[pack.name] = pack
    _cache.clear()
    return pack


def packs(names: Iterable[str] | None = None) -> tuple[SignalPack, ...]:
    if names is None:
        return tuple(_REGISTRY[n] for n in _ORDER)
    chosen = [_REGISTRY[n] for n in names if n in _REGISTRY]
    return tuple(chosen) or packs(None)


def specs(names: Iterable[str] | None = None) -> tuple[SignalSpec, ...]:
    out: list[SignalSpec] = []
    for pack in packs(names):
        out.extend(pack.specs)
    return tuple(sorted(out, key=lambda s: s.priority))


def compiled(names: Iterable[str] | None = None, languages: Iterable[str] | None = None) -> tuple[tuple[str, re.Pattern], ...]:
    """(signal name, compiled pattern) in priority order — the classifier's table."""
    key = ("compiled", tuple(names) if names is not None else None,
           tuple(languages) if languages is not None else None)
    cached = _cache.get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    ctx = Composer(languages)
    active = packs(names)
    table: list[tuple[str, re.Pattern]] = []
    for spec in specs(names):
        base = spec.template(ctx) if spec.template is not None else (spec.pattern or "")
        extra = ctx.fragments(spec.name)
        for pack in active:
            ext = pack.extends.get(spec.name)
            if ext:
                extra += "|" + ext
        table.append((spec.name, re.compile(base + extra, spec.flags)))
    result = tuple(table)
    _cache[key] = result
    return result


def names_where(flag: str, names: Iterable[str] | None = None) -> tuple[str, ...]:
    return tuple(s.name for s in specs(names) if getattr(s, flag))


def compatible_map(names: Iterable[str] | None = None) -> dict[str, tuple[str, ...]]:
    """Symmetric compatibility ("affirm" edges accept "payment_intent" and back)."""
    out: dict[str, set[str]] = {}
    for spec in specs(names):
        for other in spec.compatible:
            out.setdefault(spec.name, set()).add(other)
            out.setdefault(other, set()).add(spec.name)
    return {k: tuple(sorted(v)) for k, v in out.items()}


def knowledge_terms(names: Iterable[str] | None = None) -> tuple[str, ...]:
    out: list[str] = []
    for pack in packs(names):
        out.extend(pack.knowledge_terms)
    return tuple(out)


def classify(text: str, names: Iterable[str] | None = None, languages: Iterable[str] | None = None) -> str | None:
    stripped = (text or "").strip()
    if not stripped:
        return None
    for name, pattern in compiled(names, languages):
        if pattern.search(stripped):
            return name
    return None


# ── built-in packs ──────────────────────────────────────────────────────────
from shared.orchestration.signals import collections as _collections, core as _core, insurance as _insurance  # noqa: E402

for _pack in (_core.PACK, _collections.PACK, _insurance.PACK):
    register(_pack)
