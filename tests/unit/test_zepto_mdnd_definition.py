"""Regression checks for the dedicated Zepto MDND reference-call config."""

import runpy

from shared.orchestration.placeholders import sanitize_spoken_text


def _config():
    module = runpy.run_path("zepto/setup/06_single_bots.py")
    spec = next(
        item for item in module["CONCERNS"]
        if item["state_key"] == "BOT_MDND"
    )
    nodes, edges = module["build_mdnd_workflow"]()
    return module, spec, {node["id"]: node for node in nodes}, edges


def test_greeting_uses_partner_name_and_is_safe_when_name_is_missing():
    _module, spec, _nodes, _edges = _config()
    named = sanitize_spoken_text(
        spec["greeting_hi"],
        {"partner_name": "Saurabh", "voice_speaker_name": "Kavya"},
    )
    missing = sanitize_spoken_text(
        spec["greeting_hi"], {"voice_speaker_name": "Kavya"},
    )
    assert "Saurabh" in named
    assert "Rajesh" not in named
    assert "नमस्ते!" in missing
    assert "delivery partner से बात" in missing


def test_known_ticket_facts_are_prefilled_and_incident_answers_are_structured():
    _module, _spec, nodes, _edges = _config()
    assert nodes["n_ask_amount"]["config"]["prefillFromContext"] == \
        "mdnd_deduction_amount"
    assert nodes["n_ask_order"]["config"]["prefillFromContext"] == \
        "mdnd_order_last4"
    assert nodes["n_ask_date"]["config"]["prefillFromContext"] == \
        "mdnd_deduction_date"
    assert nodes["n_ask_reached"]["config"]["variable"] == \
        "m_reached_location"
    assert nodes["n_ask_called"]["config"]["variable"] == \
        "m_called_customer"
    assert nodes["n_ask_handover"]["config"]["variable"] == \
        "m_handover_recipient"


def test_correction_node_updates_every_structured_mdnd_answer():
    _module, _spec, nodes, _edges = _config()
    captures = nodes["n_ask_correction"]["config"]["alsoCapture"]
    updates = [c for c in captures if c.get("clear") is not True]
    clears = [c for c in captures if c.get("clear") is True]
    assert {
        "m_deduction_amount", "m_order_last4", "m_deduction_date",
        "m_reached_location", "m_called_customer",
        "m_handover_recipient", "m_cx_support_call",
    } <= {item["variable"] for item in updates}
    # "Latest clear answer wins" for every enquiry; the guard name is only
    # ever filled, never guessed over an earlier value.
    assert all(item.get("overwrite") is True for item in updates
               if item["variable"] != "m_guard_name")
    # A field named as wrong without a value is cleared for re-asking.
    assert {item["variable"] for item in clears} == {
        "m_reached_location", "m_called_customer",
        "m_handover_recipient", "m_cx_support_call",
    }
    # The correction ask steps aside when the rejection carried the fix, and
    # the verification hub itself applies inline corrections.
    assert nodes["n_ask_correction"]["config"]["skipIfCorrectedThisTurn"] is True
    hub_vars = {c["variable"] for c in nodes["n_hub_verify"]["config"]["alsoCapture"]}
    assert {"m_called_customer", "m_handover_recipient",
            "m_cx_support_call"} <= hub_vars
    assert "m_other_deduction_note" not in hub_vars   # MDND-only line


def test_v3_flow_asks_reached_and_called_together_then_handover_then_cx():
    _module, _spec, nodes, edges = _config()
    out = {}
    for edge in edges:
        out.setdefault(edge["from"], []).append(edge)
    # Condition chain picks the single question when one half is known.
    assert nodes["n_cond_reached"]["config"]["variable"] == "m_reached_location"
    assert nodes["n_cond_called"]["config"]["variable"] == "m_called_customer"
    combined = nodes["n_ask_reached_called"]["config"]
    assert combined["variable"] == "m_reached_location"
    assert "location" in combined["question"] and "call" in combined["question"]
    assert any(c["variable"] == "m_called_customer" for c in combined["alsoCapture"])
    # Guard-name follow-up only after a guard handover with no name known.
    assert nodes["n_cond_guard"]["config"] == {
        "variable": "m_handover_recipient", "operator": "equals",
        "value": "guard / security"}
    assert {e["to"] for e in out["n_cond_guard"]} == {"n_cond_guard_name", "n_ask_cx"}
    # The CX-support question exists and feeds the verification hub.
    cx = nodes["n_ask_cx"]["config"]
    assert cx["variable"] == "m_cx_support_call" and "CX support" in cx["question"]
    assert [e["to"] for e in out["n_ask_cx"]] == ["n_hub_verify"]
    # A rejected summary re-walks the enquiry chain instead of restarting.
    assert [e["to"] for e in out["n_ask_correction"]] == ["n_cond_reached"]


def test_recipient_vocabulary_covers_every_required_handover_target():
    module, _spec, _nodes, _edges = _config()
    canonicals = set(module["MDND_RECIPIENT_ENTITY"]["synonyms"])
    assert canonicals == {
        "guard / security", "customer (direct)", "mother", "father",
        "brother", "relative (other)", "left at door", "someone else",
        "not handed over",
    }
    assert canonicals == set(module["MDND_RECIPIENT_LOOKAHEAD"]["synonyms"])


def test_summary_fields_map_every_recipient_onto_the_reporting_vocabulary():
    from shared.orchestration.goal_engine import compile_goal_policy
    from shared.post_call.structured import derive_structured_fields

    module, spec, _nodes, _edges = _config()
    policy = compile_goal_policy({"summaryFields": spec["summary_fields"]},
                                 bot_name="Zepto MDND Support")
    assert policy.source == "derived"      # post-call only, live policy untouched
    assert [f.name for f in policy.summary_fields] == [
        "call_customer", "reach_customer_location", "hand_over_product",
        "hand_over_to", "call_cx",
    ]
    expected_to = {
        "guard / security": "security_guard", "customer (direct)": "customer",
        "mother": "mother", "father": "father", "brother": "brother",
        "relative (other)": "relative", "left at door": "doorstep",
        "someone else": "someone_else", "not handed over": None,
    }
    for canonical in module["MDND_RECIPIENT_ENTITY"]["synonyms"]:
        fields = derive_structured_fields(policy, {
            "m_handover_recipient": canonical,
            "m_reached_location": "yes (reached the location)",
            "m_called_customer": "no (did not call)",
            "m_cx_support_call": "no (no CX support call)",
        })
        assert fields["hand_over_to"] == expected_to[canonical], canonical
        assert fields["hand_over_product"] == (
            "No" if canonical == "not handed over" else "Yes"), canonical
        assert fields["reach_customer_location"] == "Yes"
        assert fields["call_customer"] == "No"
        assert fields["call_cx"] == "No"


# ── engine-level replay of the built definition (no DB) ─────────────────────

import pytest
from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.workflow_engine as wfe

TICKET_CONTEXT = {
    "partner_name": "Saurabh", "ticket_id": "103",
    "mdnd_deduction_amount": "500 rupees", "mdnd_deduction_date": "25 August",
    "mdnd_order_last4": "9456",
}
GOOD = ("हा. हाँ, मैं लोकेशन पर पहुँचा था और कॉल भी किया था, तो कस्टमर बोला कि "
        "मेरे घर पर मेरी माँ है। माँ के हाथ में दे दो। तो मैंने माँ को दे दिया था।")
BAD = ("हाँ, मैंने कस्टमर को प्रोडक्ट जो था कस्टमर के घर पर जाकर डिलीवर किया और "
       "डिलीवर करने से पहले ना मैं कस्टमर को कॉल भी किया तो कस्टमर बोला कि मेरी "
       "मम्मी है मेरी मम्मी के पास ही प्रोडक्ट दे दो तो मैं उनके मम्मी को दिया, "
       "उनके माँ को प्रोडक्ट दिया और मैं चला आया।")


@pytest.fixture()
def mdnd_engine(monkeypatch):
    module, _spec, _nodes, _edges = _config()
    nodes, edges = module["build_mdnd_workflow"]()
    definition = {"id": "wf_mdnd_test", "version": 1, "name": "MDND test",
                  "nodes": nodes, "edges": edges}
    monkeypatch.setattr(wfe, "load_workflow_definition",
                        lambda tenant_id, bot_id, name: definition)
    engine = wfe.WorkflowEngine()

    async def _mem(self):
        if self._checkpointer is None:
            self._checkpointer = MemorySaver()
        return self._checkpointer

    monkeypatch.setattr(wfe.WorkflowEngine, "_get_checkpointer", _mem)
    return engine


async def _mdnd_turn(engine, text, session, **kwargs):
    return await engine.handle_turn_detailed(
        session_id=session, tenant_id="tn_x", bot_id="bot_x",
        workflow_name="mdnd_test", user_text=text, language="hi-IN",
        context_values=TICKET_CONTEXT, **kwargs,
    )


@pytest.mark.parametrize("narrative", [GOOD, BAD], ids=["cv_25e68bad6919", "cv_a00399bcc37b"])
async def test_one_narrative_answers_reached_called_and_recipient(mdnd_engine, narrative):
    session = f"mdnd-{abs(hash(narrative)) % 10000}"
    r = await _mdnd_turn(mdnd_engine, "haan bol raha hoon", session)
    assert r["trace"][-1] == "n_ask_issue_desc"
    r = await _mdnd_turn(mdnd_engine, narrative, session)
    assert r["slots"]["m_reached_location"] == "yes (reached the location)"
    assert r["slots"]["m_called_customer"] == "yes (called the customer)"
    assert r["slots"]["m_handover_recipient"] == "mother"
    # Ticket facts came from context, the story answered the enquiries: the
    # only open question is the CX-support call — nothing is re-asked.
    assert r["trace"][-1] == "n_ask_cx"
    assert "location पर पहुंचे" not in r["reply"]
    assert "किसको सौंपा" not in r["reply"]
    assert "CX support" in r["reply"]
    r = await _mdnd_turn(mdnd_engine, "nahi, koi call nahi aaya", session)
    assert r["slots"]["m_cx_support_call"] == "no (no CX support call)"
    assert r["trace"][-1] == "n_hub_verify"


async def test_denied_recipient_at_verification_is_reasked_not_looped(mdnd_engine):
    session = "mdnd-corr"
    await _mdnd_turn(mdnd_engine, "haan bol raha hoon", session)
    await _mdnd_turn(mdnd_engine, BAD, session)
    await _mdnd_turn(mdnd_engine, "nahi, koi call nahi aaya", session)
    # cv_a00399bcc37b: the LLM labelled this correction 'clarify'.
    r = await _mdnd_turn(
        mdnd_engine,
        "नहीं नहीं नहीं, मुझे इसमें थोड़ा सा चेंज करना है कि प्रोडक्ट मैंने उनकी माँ "
        "को नहीं दिया था। कार्ड को दिया था। सिक्योरिटी गार्ड को।",
        session, signal="clarify",
    )
    assert "m_handover_recipient" not in r["slots"]          # mother withdrawn
    assert "बस confirm करना है" not in r["reply"]            # no canned loop
    assert "कौन सी बात सही नहीं" not in r["reply"]           # correction ask skipped
    assert r["trace"][-1] == "n_ask_handover"                 # only this re-asked
    assert r["slots"]["m_reached_location"] == "yes (reached the location)"
    assert r["slots"]["m_called_customer"] == "yes (called the customer)"
    r = await _mdnd_turn(mdnd_engine, "security guard ko diya tha", session)
    assert r["slots"]["m_handover_recipient"] == "guard / security"
    assert r["trace"][-1] == "n_ask_guard_name_known"


def test_mdnd_line_has_no_onboarding_or_other_deduction_step():
    module, spec, nodes, edges = _config()
    assert "n_ask_other" not in nodes
    assert not any(e["to"] == "n_ask_other" for e in edges)
    yes_edges = [e for e in edges if e["from"] == "n_hub_verify" and "sahi hai" in (e.get("label") or "")]
    assert yes_edges and yes_edges[0]["to"] == "n_api"
    blob = " ".join(str(n.get("config")) for n in nodes.values()).lower()
    assert "onboarding" not in blob
    assert "उसके बारे में भी कुछ बताना" not in blob       # the removed question
    assert "m_other_deduction_note" not in blob
    # The readout directive may only FORBID other deductions, never list them.
    readout = module["MDND_READOUT_DIRECTIVE"].lower()
    assert "never mention any other deduction" in readout
    assert "name it with its amount" not in readout
    assert "other_deduction" not in spec["context_extra"]
    assert "clear करना है वो बताइए" not in module["MDND_READOUT_DIRECTIVE"]
    system = module["MDND_SYSTEM"]
    assert "record whatever the partner says" not in system      # old other-deduction rule
    assert "Never mention, read out or ask about any other deduction" in system


def test_no_english_moment_in_spoken_config():
    module, _spec, nodes, _edges = _config()
    assert "एक मिनट दीजिए" in module["REGISTER_HOLD"]
    spoken = " ".join(
        str((n.get("config") or {}).get(k) or "")
        for n in nodes.values() for k in ("text", "question", "prompt", "unmatchedReply")
    )
    assert "moment" not in spoken.lower()
    assert 'never "एक moment दीजिए"' in module["MDND_SYSTEM"]


async def _reach_guard_name_hub(engine, session):
    await _mdnd_turn(engine, "haan bol raha hoon", session)
    r = await _mdnd_turn(engine, "मैंने कस्टमर को कॉल किया, लोकेशन पर पहुँच के कॉल किया। कस्टमर ने बोला "
                                 "गार्ड को दे दो। तो मैं गार्ड के हाथों में ही हैंडओवर कर दिया था।", session)
    assert r["trace"][-1] == "n_ask_guard_name_known"
    return r


@pytest.mark.parametrize("answer", [
    "हाँ, नाम पूछा था तो गार्ड बोला उसका नाम राजू है।",
    "हाँ मैंने नाम पूछा था उसका नाम था राजू",
    "haan pucha tha, guard ka naam Raju tha",
], ids=["uska-naam-hai", "naam-tha-X", "guard-ka-naam"])
async def test_yes_with_the_name_never_asks_the_name(mdnd_engine, answer):
    session = f"gn-{abs(hash(answer)) % 10000}"
    await _reach_guard_name_hub(mdnd_engine, session)
    # cv_df9a5a870b4e: the LLM labelled exactly this kind of answer 'clarify'.
    r = await _mdnd_turn(mdnd_engine, answer, session, signal="clarify")
    assert r["slots"]["m_guard_name"] in ("राजू", "Raju")
    assert r["trace"][-1] == "n_ask_cx"                       # name ask skipped
    assert "guard का नाम क्या था" not in r["reply"]
    assert "बस इतना confirm" not in r["reply"]


async def test_bare_yes_asks_the_name_and_stores_only_the_name(mdnd_engine):
    session = "gn-bare"
    await _reach_guard_name_hub(mdnd_engine, session)
    r = await _mdnd_turn(mdnd_engine, "haan pucha tha", session, signal="affirm")
    assert r["trace"][-1] == "n_ask_guard_name"
    r = await _mdnd_turn(mdnd_engine, "राजू मैंने बताया ना अभी घाट का नाम राजू था", session)
    assert r["slots"]["m_guard_name"] == "राजू"                # not the sentence
    assert r["trace"][-1] == "n_ask_cx"


async def test_not_asked_skips_the_name(mdnd_engine):
    session = "gn-no"
    await _reach_guard_name_hub(mdnd_engine, session)
    r = await _mdnd_turn(mdnd_engine, "nahi pucha", session, signal="refusal")
    assert r["slots"]["m_guard_name"] == "not known (name not asked)"
    assert r["trace"][-1] == "n_ask_cx"


# ── replays of local calls cv_3fc5b4c31fe0 / cv_96c86eced1c4 (2026-09-03) ──
CV3FC_NARRATIVE = "मैंने प्रोडक्ट कस्टमर को दे दिया था उसके बाद भी मेरा पैसा डिडक्ट हुआ"
CV3FC_ANSWER = ("हाँ, लोकेशन पर पहुँच कर मैंने कॉल भी किया था। और प्रोडक्ट जो है "
                "कस्टमर को ही दिया था। और फिर भी मेरा पैसा डिडक्ट हुआ।")
CV96_NARRATIVE = "मैं प्रोडक्ट डिलीवरी कर दिया, फिर भी मेरे अकाउंट से पैसा कट गया।"


async def test_cv_3fc5b4c31fe0_grievance_labelled_complaint_is_the_narrative_answer(mdnd_engine):
    """The LLM labelled the partner's grievance 'complaint' (platform meaning:
    "the bot is not listening"). At the free-text "क्या हुआ था?" ask the
    narrative that names the recipient IS the answer — it must be stored and
    the flow must move on, not park off-script (which lost the reached/called
    answer of the next utterance too and let the LLM invent a guard)."""
    session = "cv3fc"
    r = await _mdnd_turn(mdnd_engine, "haan bol raha hoon", session)
    assert r["trace"][-1] == "n_ask_issue_desc"
    r = await _mdnd_turn(mdnd_engine, CV3FC_NARRATIVE, session, signal="complaint")
    assert r["offScript"] is False
    assert r["slots"]["m_issue_description"] == CV3FC_NARRATIVE
    assert r["slots"]["m_handover_recipient"] == "customer (direct)"
    assert r["trace"][-1] == "n_ask_reached_called"
    # Labelled 'question' by the LLM although it is a plain answer.
    r = await _mdnd_turn(mdnd_engine, CV3FC_ANSWER, session, signal="question")
    assert r["slots"]["m_reached_location"] == "yes (reached the location)"
    assert r["slots"]["m_called_customer"] == "yes (called the customer)"
    assert r["trace"][-1] == "n_ask_cx"
    assert "guard" not in r["reply"].lower()
    r = await _mdnd_turn(mdnd_engine, "हाँ, आया था।", session, signal="affirm")
    assert r["slots"]["m_cx_support_call"] == "yes (received CX support call)"
    assert r["trace"][-1] == "n_hub_verify"


async def test_cv_3fc5b4c31fe0_question_at_grounded_verify_hub_reaches_the_llm(mdnd_engine):
    """"हाँ सही है, CX support क्या है?" was answered three times with the
    fixed "बस confirm करना है" re-ask. A genuine question at a grounded hub
    goes off-script so the brain answers it (and re-asks the confirmation)."""
    session = "cv3fc-q"
    await _mdnd_turn(mdnd_engine, "haan bol raha hoon", session)
    await _mdnd_turn(mdnd_engine, CV3FC_NARRATIVE, session, signal="complaint")
    await _mdnd_turn(mdnd_engine, CV3FC_ANSWER, session, signal="question")
    r = await _mdnd_turn(mdnd_engine, "हाँ, आया था।", session, signal="affirm")
    assert r["trace"][-1] == "n_hub_verify"
    r = await _mdnd_turn(mdnd_engine, "हाँ सही है, सीएक्स सपोर्ट क्या है?", session,
                         signal="question")
    assert r["offScript"] is True
    assert "बस confirm करना है" not in r["reply"]
    assert r["trace"][-1] == "n_hub_verify"
    # Nothing was corrupted by the question turn; a plain yes registers.
    r = await _mdnd_turn(mdnd_engine, "हाँ सही है।", session, signal="affirm")
    assert "n_api" in r["trace"]


async def test_cv_96c86eced1c4_delivery_kar_diya_counts_as_reached(mdnd_engine):
    """"प्रोडक्ट डिलीवरी कर दिया" means the partner was at the location — the
    combined reached+called question must not be asked again in full."""
    session = "cv96"
    await _mdnd_turn(mdnd_engine, "haan bol raha hoon", session)
    r = await _mdnd_turn(mdnd_engine, CV96_NARRATIVE, session, signal="already_paid")
    assert r["slots"]["m_reached_location"] == "yes (reached the location)"
    assert r["trace"][-1] == "n_ask_called"


def test_verify_directive_attributes_family_recipients_to_the_customer():
    """cv_96c86eced1c4: recipient 'mother' was read out as 'आपकी माँ' (the
    partner's own mother) and the correction loop repeated it verbatim."""
    module, _spec, _nodes, _edges = _config()
    directive = module["MDND_VERIFY_DIRECTIVE"]
    assert "customer की माँ" in directive
    assert "आपकी माँ" in directive  # named as the forbidden wording
    assert "never the partner's own" in directive.lower() or "never 'आपकी" in directive
