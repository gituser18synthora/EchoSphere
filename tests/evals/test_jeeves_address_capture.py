"""Opt-in: the configured conversation model as the Jeeves address reader.

ECHOSPHERE_RUN_SUMMARY_EVAL=1 env/bin/python -m pytest tests/evals/test_jeeves_address_capture.py -q

Reads the local bot configuration (the model the brain hands to the
semantic-slots provider) and calls it through the same bounded request the
engine sends (shared.orchestration.extensions.jeeves_address.extract_address);
does not touch sessions, memories or settings. The verbatim check is part of
the function under test, so a passing case proves both the reading and that
nothing outside the customer's words came back.
"""

import asyncio
import os

import pytest

from shared.orchestration.extensions.jeeves_address import extract_address

pytestmark = pytest.mark.skipif(os.getenv("ECHOSPHERE_RUN_SUMMARY_EVAL") != "1", reason="Opt-in real-provider evaluation")

BOT_ID = "bot_44a50e8c4146"
QUESTION_HI = "कोई बात नहीं। आप please अपना सही address pincode के साथ बताइए, जहाँ से device का uninstallation और pickup होना है।"
QUESTION_EN = "No problem. Could you please share your correct address along with the pincode, where the device needs to be uninstalled and picked up?"

# (utterance, mode, captured, expected cleaned address or None, components that must appear)
CASES = [
    ("मेरा इस समय करंट एड्रेस है। थाने सेक्टर 16। पिन कोड है। 404041.", "capture", "", "थाने सेक्टर 16, 404041", ["थाने सेक्टर 16", "404041"]),
    ("mera naya address hai Flat 21 Lotus Apartments Sector 62 Noida, pin 201301.", "capture", "", None, ["Flat 21", "Lotus Apartments", "Sector 62", "Noida", "201301"]),
    ("हाँ जी, नया पता है B-12, Sector 15, Noida, Uttar Pradesh, pincode 201301.", "capture", "", None, ["B-12", "Sector 15", "Noida", "201301"]),
    ("My address is 14 MG Road, Pune, near the Deccan bus stop, 411004.", "capture", "", None, ["14 MG Road", "Pune", "411004"]),
    ("ji, abhi main Powai mein rehta hoon, Hiranandani Gardens, Mumbai 400076 — wahan bhej dijiye.", "capture", "", None, ["Powai", "Hiranandani Gardens", "400076"]),
    ("Andheri East, Mumbai. Pin code yaad nahi hai abhi.", "capture", "", None, ["Andheri East", "Mumbai"]),
    ("मेरा एड्रेस है ना... वो क्या है, एक मिनट", "capture", "", None, []),           # no place: ask again
    ("404041", "capture", "", None, []),                                            # a bare PIN is not an address
    ("nahi, wahi sahi hai jo aapne bola, Sector 62 Noida hi hai.", "capture", "", None, []),   # retraction
    ("baad mein bataunga, abhi yaad nahi aa raha.", "capture", "", None, []),
    ("पिन कोड 404041", "continue", "थाने सेक्टर 16", "404041", ["404041"]),
    ("कल कर दो", "continue", "थाने सेक्टर 16", None, []),                          # a day answer adds nothing
    ("कल शाम 5 बजे आ जाइए", "capture", "", None, []),                               # a time is not an address
]


async def _llm():
    from shared.bot_config import resolve_bot_config
    from shared.config import get_settings
    from shared.providers.base import ProviderConfig
    from shared.providers.factory import get_llm_provider

    assert get_settings().mysql_host in {"127.0.0.1", "localhost"}, "Evaluation requires local bot configuration"
    config = await resolve_bot_config(BOT_ID, require_published=False)
    llm_conf = dict(config.llm or {})
    extra = {k: v for k, v in (llm_conf.get("extra") or {}).items() if k != "service_tier"}
    return get_llm_provider(ProviderConfig(
        provider=llm_conf.get("provider") or "openai", model=llm_conf.get("model") or "",
        api_key_reference=llm_conf.get("api_key_reference") or "", timeout_seconds=30.0, extra=extra,
    ))


async def _run():
    llm = await _llm()
    errors = []

    async def one(case):
        text, mode, captured, exact, parts = case
        out = await extract_address(llm, text=text, mode=mode, captured=captured,
                                    pending_question=QUESTION_EN if text[0].isascii() else QUESTION_HI, timeout_seconds=30)
        if out.failed:
            errors.append(f"{text!r}: reader failed ({out.failure_reason})")
            return
        if exact is not None and out.address != exact:
            errors.append(f"{text!r}: expected {exact!r}, got {out.address!r}")
        if not parts and out.address is not None:
            errors.append(f"{text!r}: expected no address, got {out.address!r}")
        for part in parts:
            if part not in (out.address or ""):
                errors.append(f"{text!r}: {part!r} missing from {out.address!r}")
        for filler in ("मेरा इस समय", "पिन कोड है", "mera naya address", "My address is", "wahan bhej", "yaad nahi"):
            if filler in (out.address or ""):
                errors.append(f"{text!r}: filler {filler!r} kept in {out.address!r}")

    limit = asyncio.Semaphore(3)

    async def guarded(case):
        async with limit:
            await one(case)

    await asyncio.gather(*(guarded(case) for case in CASES))
    assert not errors, "\n".join(errors)


async def test_address_reader_on_realistic_utterances():
    await _run()
