"""Real-model check of the MDND final confirmation wording (opt-in).

Runs only with ``RUN_LLM_TESTS=1`` — it calls the configured LLM exactly the
way the live brain's constrained grounded path does (short wording-only
system, script as the sole message, no history) and then applies the
speaker-grammar adapter for a male and a female voice, as ``_say`` does.
Semantic constraints, not byte equality: the four facts, the recorded place
verbatim, the natural phrasing, no ticket recap when the readout was heard,
no invented recipient, the speaker's own verb form.
"""

import asyncio
import os
import runpy

import pytest

from shared.orchestration.response_modes import (
    grounded_delivery_instruction,
    resolve_response_directive,
    resolve_response_must_include,
    validate_grounded_reply,
)
from shared.orchestration.voice_identity import VoiceIdentity, adapt_authored_speaker_grammar

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LLM_TESTS") != "1", reason="set RUN_LLM_TESTS=1 to call the model"
)

SLOTS = {
    "m_deduction_amount": "400 rupees", "m_deduction_date": "4 August", "m_order_last4": "9203",
    "m_reached_location": "yes (reached the location)", "m_called_customer": "yes (called the customer)",
    "m_handover_recipient": "place (kept at a spot)", "m_drop_location": "इन्वर्टर के ऊपर",
    "m_cx_support_call": "no (no CX support call)",
    "m_issue_description": "मेरा पैसा कट गया",
}
CONTEXT = ("\n\n# Call context\npartner_name: Saurabh\nmdnd_deduction_amount: 400 rupees\n"
           "mdnd_deduction_date: 4 August\nmdnd_order_last4: 9203\n")


_CACHE: dict = {}


def _generate(heard: bool) -> str:
    """Both variants are generated in ONE event loop (the provider's HTTP
    client is bound to the loop it was created in) and cached."""
    if not _CACHE:
        from shared.config import get_settings
        from shared.providers.base import ProviderConfig
        from shared.providers.factory import get_llm_provider
        from voice_runtime.transcript_gate import script_supports_language

        module = runpy.run_path("zepto/setup/06_single_bots.py")
        nodes, _ = module["build_mdnd_workflow"]()
        hub = {n["id"]: n for n in nodes}["n_hub_verify"]["config"]
        script = hub["prompt"]
        must = resolve_response_must_include(hub, "hi-IN")

        async def _run():
            s = get_settings()
            llm = get_llm_provider(ProviderConfig(provider=s.llm_provider, model=s.llm_model,
                                                  api_key_reference=s.llm_api_key_reference))
            out = {}
            for flag in (True, False):
                directive = resolve_response_directive(hub, ["n_ask_issue_desc"] if flag else [])
                instruction = grounded_delivery_instruction(
                    directives=[directive], script=script, workflow_values=SLOTS, response_language="hi-IN")
                system = ("You word one step of a phone call flow for a voice assistant. Rewrite the "
                          "script below per the rules; output ONLY the spoken reply." + CONTEXT
                          + instruction + "\nRespond in natural spoken Hindi.")
                r = await llm.generate([{"role": "user", "content": script}], system=system,
                                       temperature=0, max_tokens=400)
                text = r.text.strip()
                assert validate_grounded_reply(script, text, "hi-IN", must_include=must,
                                               language_check=script_supports_language,
                                               verified_context=SLOTS), text
                out[flag] = text
            return out

        _CACHE.update(asyncio.run(_run()))
    return _CACHE[heard]


def _semantic_checks(text: str) -> None:
    # 1. the four facts, caller-grounded and affirmative
    assert "customer की location" in text, text
    assert "पहुँचे थे" in text or "पहुंचे थे" in text, text
    assert "call किया था" in text or "कॉल किया था" in text, text
    assert "इन्वर्टर के ऊपर" in text, text                       # 6. place preserved verbatim
    assert "नहीं आया" in text, text                                 # CX negative, caller-facing
    assert "सही है" in text, text
    # 2/3. no invented recipient, no stale person, no passive generalisation
    for banned in ("guard", "गार्ड", "customer को दिया", "doorstep", "someone else", "रखा गया"):
        assert banned not in text, (banned, text)
    # 4. natural grammar preferences
    assert "customer के location" not in text, text
    assert "पहुँचने की बात" not in text and "पहुंचने की बात" not in text, text
    # 7. no re-asked question
    for question in ("पहुंचे थे?", "call किया था?", "किसको सौंपा", "कोई call आया था?"):
        assert question not in text, text


def test_final_confirmation_wording_when_the_readout_was_heard():
    text = _generate(heard=True)
    _semantic_checks(text)
    # 8. no ticket recap
    for ticket in ("record", "रिकॉर्ड", "400", "9203", "अगस्त", "August"):
        assert ticket not in text, (ticket, text)
    # 5. speaker gender follows the voice at delivery
    male = adapt_authored_speaker_grammar(text, VoiceIdentity("Abhishek", "male"))
    female = adapt_authored_speaker_grammar(text, VoiceIdentity("Priya", "female"))
    assert "कर लेता हूँ" in male and "कर लेती हूँ" not in male, male
    assert "कर लेती हूँ" in female and "कर लेता हूँ" not in female, female
    assert "इन्वर्टर के ऊपर" in male and "इन्वर्टर के ऊपर" in female


def test_final_confirmation_keeps_the_recap_only_when_the_readout_was_not_heard():
    text = _generate(heard=False)
    _semantic_checks(text)
    assert "9203" in text or "नौ दो शून्य तीन" in text, text      # recovery path keeps the facts
