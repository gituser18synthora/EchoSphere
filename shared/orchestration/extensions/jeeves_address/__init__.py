"""Jeeves extension: the ``jeeves_address_v1`` semantic-slots provider.

The Airtel Broadband uninstallation flow (bot_44a50e8c4146) asks for the
customer's correct address when the registered one is wrong. Its two address
hubs are ``elseIsAnswer`` intent nodes with ``captureVariable: new_address``:
without this provider the engine stores the WHOLE utterance, and the final
readback repeated the caller's filler as the address — "हमारे इंजीनियर कल
आपके नए पते, मेरा इस समय करंट एड्रेस है। थाने सेक्टर 16। पिन कोड है। 404041,
पर …" (cv_21df0c56facf, 2026-10-29).

With ``semanticSlots: "jeeves_address_v1"`` on the start node and
``semanticCapture: true`` on the address hubs, the engine hands each address
answer to :meth:`extract` (started at dispatch, in parallel with the Goal
Engine decision, so it adds no latency on the usual turn). The model's only
job is to COPY the address components out of the utterance; the code then
proves every component is a verbatim span of what the customer said, orders
the spans as spoken and stores the cleaned value — so nothing can be
invented, translated or "corrected", only left out. An answer with no
place in it (filler only, a bare PIN, a refusal) is reported as decided-
empty, and the engine asks again instead of accepting the raw utterance.

One continuation turn is read as well: when the next ask (the visit day)
is answered right after an address was captured, a PIN or landmark said in
a second breath ("pin code 404041") is appended to the stored address.
The decision entities the Goal Engine emits were considered and not used:
their key names are not configured anywhere (the model picked "address"/
"pincode" itself), they are absent whenever the decision times out (two of
six turns in that call), and the decision prompt does not promise verbatim
values. One small dedicated reader with a verbatim check is simpler to
trust.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any

from shared.orchestration.extensions import register_semantic_provider

logger = logging.getLogger(__name__)

KEY = "jeeves_address_v1"
CAPTURE_VARIABLE = "new_address"
RAW_VARIABLE = "new_address_raw"
#: Asks answered right after the address where a trailing PIN/landmark may
#: still arrive (the flow moves to the visit-day ask the moment an address
#: is stored).
CONTINUATION_PENDING = frozenset({"visit_day"})
TIMEOUT_SECONDS = 4.0
MAX_ADDRESS_CHARS = 300
_QUOTE_WRAPPERS = "\"'“”‘’«»`"
_EDGE_PUNCTUATION = " \t.,;:!?।॥-–—()"
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)

_SYSTEM = """You are a reading-comprehension extractor, not a speaking assistant.
An Airtel Broadband agent asked the customer for the address where an engineer should come to uninstall and collect the broadband device. You read the LATEST customer utterance (Hindi, English or Hinglish, often transcribed with Devanagari script and sentence marks) and copy out ONLY the address components.

Address components: house/flat/plot number, building or society name, floor, street/road/lane, area/locality/colony/sector/phase/block, landmark ("near …", "ke paas"), village/town/city/district, state, PIN code.
Not address components (never include): conversational filler and framing ("mera address hai", "is samay", "current address", "pin code hai", "bata raha hoon", "ji", "haan", "yahi hai"), the words "address"/"pata"/"pin code" themselves when they only introduce a value, a visit day or time, a phone number, names of people.

Rules:
1. Copy each component EXACTLY as it appears in the utterance — same script, same spelling, same digits. Never translate (Devanagari stays Devanagari), never fix a spelling or a PIN, never add a city, state or number that was not said, never reorder words inside a component.
2. Return the components as a list of verbatim spans in the order spoken. A span may be several adjacent components ("Thane Sector 16") or one ("404041"). A 6-digit PIN code is ALWAYS a component: copy the digits even when the customer introduces them with "pin code hai" / "pincode" — the introducing words are left out, the digits are kept.
3. If the utterance contains NO address component at all — only filler, a refusal ("baad mein bataunga"), a yes/no, a question, a day or a time — return an empty list.
4. If the customer says the registered/earlier address is actually correct ("wahi sahi hai", "same address"), return an empty list and "same_as_registered": true.
5. When an "already_captured_address" is given, the customer may be ADDING to it (a PIN, a landmark) or answering a different question (a visit day). Return only NEW address components; an empty list when there are none.

Examples (utterance → spans):
- "मेरा इस समय करंट एड्रेस है। थाने सेक्टर 16। पिन कोड है। 404041." → ["थाने सेक्टर 16", "404041"]
- "mera naya address hai Flat 21 Lotus Apartments Sector 62 Noida, pin 201301." → ["Flat 21 Lotus Apartments Sector 62 Noida", "201301"]
- "ji, abhi main Powai mein rehta hoon, Hiranandani Gardens, Mumbai 400076 — wahan bhej dijiye." → ["Powai", "Hiranandani Gardens", "Mumbai 400076"]
- "पिन कोड 404041" → ["404041"]
- "404041" → ["404041"]
- "मेरा एड्रेस है ना... वो क्या है, एक मिनट" → []
- "nahi, wahi sahi hai jo aapne bola" → [] with "same_as_registered": true
- "कल शाम 5 बजे आ जाइए" → []

Output ONLY this JSON object, no prose, no fences:
{"spans": [<verbatim span>, ...], "same_as_registered": <true|false>}"""


@dataclass
class AddressExtraction:
    """What the provider decided for one turn (a plain dict in the state)."""

    mode: str = ""                       # "capture" | "continue" | ""
    raw_text: str = ""
    spans: list[str] = field(default_factory=list)
    address: str | None = None           # the cleaned value, when usable
    same_as_registered: bool = False
    patch: dict[str, str] = field(default_factory=dict)
    evidence: dict[str, str] = field(default_factory=dict)
    failed: bool = False
    failure_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0


def _normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", str(text or "")).casefold().split())


def verbatim_span(value: Any, text: str) -> tuple[int, str] | None:
    """``(position, span)`` when ``value`` occurs verbatim in ``text`` (up to
    case, Unicode form, whitespace and quote/punctuation at the edges); the
    span is returned as it reads in the utterance."""
    if not isinstance(value, str):
        return None
    wanted = _normalized(value.strip().strip(_QUOTE_WRAPPERS).strip(_EDGE_PUNCTUATION))
    if not wanted:
        return None
    collapsed = " ".join(str(text or "").split())
    folded = _normalized(collapsed)
    if len(folded) != len(collapsed):
        return (0, collapsed) if wanted == folded else None
    start = folded.find(wanted)
    if start < 0:
        return None
    return start, collapsed[start:start + len(wanted)]


def clean_address(spans: list, text: str, *, require_place: bool = True) -> tuple[list[str], str | None]:
    """Validate the model's spans against the utterance and compose the value.

    Keeps a span only when it is verbatim in ``text``; orders by position,
    drops a span already covered by another; with ``require_place`` (the
    capture turn) the result is usable only when at least one span names a
    place (contains a letter), so a bare PIN or a list of nothing never
    becomes the address — a continuation turn may add a PIN alone.
    """
    found: list[tuple[int, str]] = []
    for item in spans if isinstance(spans, list) else []:
        hit = verbatim_span(item, text)
        if hit is None:
            continue
        if any(_normalized(hit[1]) in _normalized(other) for _, other in found):
            continue
        found = [(pos, span) for pos, span in found if _normalized(span) not in _normalized(hit[1])]
        found.append(hit)
    found.sort(key=lambda item: item[0])
    kept = [span.strip(_EDGE_PUNCTUATION) for _, span in found]
    kept = [span for span in kept if span]
    if not kept or (require_place and not any(_LETTER.search(span) for span in kept)):
        return kept, None
    address = ", ".join(kept)
    return kept, address[:MAX_ADDRESS_CHARS].rstrip(" ,") or None


def _parse(raw: str) -> dict | None:
    content = (raw or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, flags=re.DOTALL | re.IGNORECASE)
    try:
        data = json.loads(fenced.group(1) if fenced else content)
    except (ValueError, AttributeError):
        match = re.search(r"\{.*\}", content, re.S)
        if match is None:
            return None
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


async def extract_address(llm: Any, *, text: str, mode: str, captured: str = "",
                          pending_question: str = "", history: Any = None,
                          timeout_seconds: float = TIMEOUT_SECONDS) -> AddressExtraction:
    """One bounded reader call; a failure never changes state (``failed``)."""
    if not text or not text.strip():
        return AddressExtraction(mode=mode, raw_text=text or "")
    recent = [
        {"role": item["role"], "content": str(item.get("content") or "")[-400:]}
        for item in (history or [])[-4:]
        if isinstance(item, dict) and item.get("role") in {"assistant", "user"}
    ]
    payload = {
        "bot_question": pending_question or "",
        "recent_history": recent,
        "latest_customer_utterance": text,
    }
    if mode == "continue" and captured:
        payload["already_captured_address"] = captured
    try:
        result = await asyncio.wait_for(
            llm.generate(
                [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                system=_SYSTEM, temperature=0.0, max_tokens=200,
            ),
            timeout=max(0.001, min(float(timeout_seconds), 15.0)),
        )
    except TimeoutError:
        return AddressExtraction(mode=mode, raw_text=text, failed=True, failure_reason="timeout", requests=1)
    except Exception:  # noqa: BLE001 — the turn keeps its existing capture path
        logger.exception("jeeves address extraction failed")
        return AddressExtraction(mode=mode, raw_text=text, failed=True, failure_reason="provider_error", requests=1)
    usage = {
        "input_tokens": int(getattr(result, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(result, "output_tokens", 0) or 0),
        "requests": 1,
    }
    data = _parse(getattr(result, "text", "") or "")
    if data is None:
        return AddressExtraction(mode=mode, raw_text=text, failed=True, failure_reason="invalid_response", **usage)
    spans, address = clean_address(data.get("spans"), text, require_place=(mode != "continue"))
    same = data.get("same_as_registered") is True
    if same:
        address = None
    extraction = AddressExtraction(
        mode=mode, raw_text=text, spans=spans, address=address, same_as_registered=same, **usage,
    )
    if address and mode == "capture":
        extraction.patch = {CAPTURE_VARIABLE: address}
        extraction.evidence = {CAPTURE_VARIABLE: address}
    return extraction


class JeevesAddressSlots:
    """SemanticSlotsProvider for the one free-text address of the Jeeves flow."""

    key = KEY
    fields = {CAPTURE_VARIABLE: CAPTURE_VARIABLE}

    def llm_extraction_enabled(self, definition: dict, language: str = "") -> bool:
        return any((node.get("config") or {}).get("semanticSlots") == KEY
                   for node in (definition or {}).get("nodes") or [])

    def canonical_slots(self, slots: dict) -> dict:
        return {name: slots[name] for name in (CAPTURE_VARIABLE, RAW_VARIABLE)
                if isinstance(slots.get(name), str)}

    def pending_fields(self, slots: dict) -> tuple[str, ...]:
        return ()

    def merge_extraction(self, slots: dict, result: dict, audit: list, node: str | None) -> set[str]:
        """Store the cleaned address; report the capture variable as DECIDED
        whenever the reader ran in capture mode (also when it found nothing —
        the engine then asks again rather than storing the utterance). A
        failed reader decides nothing here, and the engine treats the answer
        as unverified all the same (clarification, retries, fallback): on a
        ``semanticCapture`` hub the utterance is never the value."""
        if not isinstance(result, dict) or result.get("failed"):
            return set()
        mode = result.get("mode")
        address = result.get("address")
        raw_text = str(result.get("raw_text") or "")
        if mode == "capture":
            if address:
                before = slots.get(CAPTURE_VARIABLE)
                slots[CAPTURE_VARIABLE] = address
                slots[RAW_VARIABLE] = " ".join(raw_text.split())[:MAX_ADDRESS_CHARS]
                audit.append({"action": "also_updated" if before else "also_captured",
                              "node": node, "variable": CAPTURE_VARIABLE,
                              "source": KEY, "evidence": address})
            else:
                audit.append({"action": "capture_empty", "node": node,
                              "variable": CAPTURE_VARIABLE, "source": KEY,
                              "same_as_registered": bool(result.get("same_as_registered"))})
            return {CAPTURE_VARIABLE}
        if mode == "continue" and address:
            existing = str(slots.get(CAPTURE_VARIABLE) or "").strip()
            extra = [span for span in (result.get("spans") or [])
                     if span and _normalized(span) not in _normalized(existing)]
            if existing and extra:
                combined = ", ".join([existing, *extra])[:MAX_ADDRESS_CHARS].rstrip(" ,")
                slots[CAPTURE_VARIABLE] = combined
                slots[RAW_VARIABLE] = (
                    " ".join(f"{slots.get(RAW_VARIABLE) or ''} {raw_text}".split())[:MAX_ADDRESS_CHARS]
                )
                audit.append({"action": "also_updated", "node": node, "variable": CAPTURE_VARIABLE,
                              "source": KEY, "evidence": ", ".join(extra), "continuation": True})
                return {CAPTURE_VARIABLE}
        return set()

    def semantic_node(self, node: dict, decided: set[str] | None = None) -> dict:
        return node

    def summary_fallback(self, slots: dict, language: str) -> str:
        return ""

    def owns_variable(self, variable: str) -> bool:
        return False   # the asks of the flow keep their deterministic resolution

    def is_summary_hub(self, node: dict) -> bool:
        return False

    def is_narrative_ask(self, node: dict, variable: str) -> bool:
        return False

    async def extract(self, llm: Any, *, text: str, slots: dict, pending_question: str,
                      pending_variable: str, joint_variables: list[str], history: Any,
                      language: str = "") -> dict:
        captured = str(slots.get(CAPTURE_VARIABLE) or "").strip()
        if pending_variable == CAPTURE_VARIABLE:
            mode = "capture"
        elif pending_variable in CONTINUATION_PENDING and captured:
            mode = "continue"
        else:
            return asdict(AddressExtraction())   # not an address turn: no model call
        extraction = await extract_address(
            llm, text=text, mode=mode, captured=captured,
            pending_question=pending_question, history=history,
        )
        return asdict(extraction)


PROVIDER = JeevesAddressSlots()
register_semantic_provider(PROVIDER)
