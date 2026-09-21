"""Language pack registry and regex composition helpers.

The router and the workflow engine never spell a caller-language word: they
ask this module for the union of the registered packs (or of the languages a
bot supports) and compose their patterns from it. Registration order is the
alternation order inside composed patterns; every use site only tests
``search()`` truthiness, so order never changes an outcome.
"""
from __future__ import annotations

import re
import string
from typing import Iterable

from shared.orchestration.lang.base import LanguagePack

_REGISTRY: dict[str, LanguagePack] = {}
_ORDER: list[str] = []


def register(pack: LanguagePack) -> LanguagePack:
    if pack.code not in _REGISTRY:
        _ORDER.append(pack.code)
    _REGISTRY[pack.code] = pack
    _compose_cache.clear()
    return pack


def get(code: str) -> LanguagePack | None:
    """Pack for a language code or locale ("hi", "hi-IN", "ml_IN")."""
    base = (code or "").replace("_", "-").split("-")[0].strip().lower()
    return _REGISTRY.get(base)


def packs(codes: Iterable[str] | None = None) -> tuple[LanguagePack, ...]:
    """Registered packs, optionally restricted to ``codes`` (unknown codes
    are ignored; an empty selection falls back to every pack so a bot whose
    languages are not packed still gets the platform vocabulary)."""
    if codes is None:
        return tuple(_REGISTRY[c] for c in _ORDER)
    chosen = [p for p in (get(c) for c in codes) if p is not None]
    seen: list[LanguagePack] = []
    for p in chosen:
        if p not in seen:
            seen.append(p)
    return tuple(seen) or packs(None)


def codes() -> tuple[str, ...]:
    return tuple(_ORDER)


# ── composition helpers ─────────────────────────────────────────────────────
_compose_cache: dict[tuple, object] = {}


def _cached(key: tuple, build):
    value = _compose_cache.get(key)
    if value is None:
        value = build()
        _compose_cache[key] = value
    return value


def letters(codes: Iterable[str] | None = None) -> str:
    """Character-class body of every selected script's letters (for ``{L}``)."""
    return "".join(p.letters for p in packs(codes))


def fill(fragment: str, codes: Iterable[str] | None = None) -> str:
    return fragment.replace("{L}", letters(codes))


def union(attr: str, codes: Iterable[str] | None = None) -> frozenset[str]:
    """Union of a lexicon attribute across the selected packs."""
    key = ("union", attr, tuple(codes) if codes is not None else None)
    return _cached(key, lambda: frozenset().union(*(getattr(p, attr) for p in packs(codes))))


def alternatives(attr: str, codes: Iterable[str] | None = None) -> str:
    """``a|b|c`` of a fragment-tuple attribute across the selected packs
    (with ``{L}`` filled); "" when no pack contributes."""
    parts: list[str] = []
    for p in packs(codes):
        parts.extend(getattr(p, attr))
    return "|".join(fill(part, codes) for part in parts if part)


def fragments(signal: str, codes: Iterable[str] | None = None) -> str:
    """Extra alternatives the selected languages add to a signal pattern,
    each prefixed with ``|`` so it appends to a base alternation."""
    out = ""
    for p in packs(codes):
        frag = p.signal_fragments.get(signal)
        if frag:
            out += "|" + fill(frag, codes)
    return out


def compile_any(attr: str, codes: Iterable[str] | None = None, flags: int = re.I) -> list[re.Pattern]:
    """One compiled pattern per fragment of ``attr`` across the packs."""
    key = ("any", attr, tuple(codes) if codes is not None else None, flags)
    return _cached(key, lambda: [re.compile(fill(part, codes), flags)
                                 for p in packs(codes) for part in getattr(p, attr) if part])


def compile_alternation(attr: str, codes: Iterable[str] | None = None, flags: int = re.I) -> re.Pattern | None:
    key = ("alt", attr, tuple(codes) if codes is not None else None, flags)

    def build():
        alt = alternatives(attr, codes)
        return re.compile(alt, flags) if alt else None

    return _cached(key, build)


def sentence_terminators(codes: Iterable[str] | None = None) -> str:
    seen = ""
    for p in packs(codes):
        for ch in p.sentence_terminators:
            if ch not in seen:
                seen += ch
    return seen


def token_strip_chars(codes: Iterable[str] | None = None) -> str:
    extra = "".join(p.token_strip_extra for p in packs(codes))
    return string.punctuation + "".join(ch for i, ch in enumerate(extra) if ch not in extra[:i])


def short_token_floor(token: str, codes: Iterable[str] | None = None) -> int:
    """Minimum length for ``token`` to count as a topic word: the smallest
    floor among the scripts it is written in (default 3)."""
    floors = [p.short_token_floor for p in packs(codes)
              if p.letters and re.search(f"[{p.letters}]", token)]
    return min(floors) if floors else 3


# ── built-in packs ──────────────────────────────────────────────────────────
from shared.orchestration.lang import en as _en, hi as _hi, ml as _ml, ta as _ta  # noqa: E402

for _pack in (_hi.PACK, _en.PACK, _ml.PACK, _ta.PACK):
    register(_pack)
