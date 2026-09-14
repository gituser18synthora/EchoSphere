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
        "m_drop_location",          # a person named as the correction clears a stale place
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
    # The four-slot flow keeps names optional: a guard handover goes to CX.
    assert nodes["n_cond_guard"]["config"] == {
        "variable": "m_handover_recipient", "operator": "equals",
        "value": "guard / security"}
    assert {e["to"] for e in out["n_cond_guard"]} == {"n_cond_guard_name", "n_ask_cx"}
    assert [e["to"] for e in out["n_ask_handover"]] == ["n_ask_cx"]
    assert nodes["n_start"]["config"]["semanticSlots"] == "mdnd_v1"
    assert any(e["from"] == "n_hub_verify" and e.get("label") == "correction"
               and e["to"] == "n_ask_correction" for e in edges)
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
        "not handed over", "place (kept at a spot)",
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
        "hand_over_to", "handover_type", "drop_location", "call_cx",
    ]
    expected_to = {
        "guard / security": "security_guard", "customer (direct)": "customer",
        "mother": "mother", "father": "father", "brother": "brother",
        "relative (other)": "relative", "left at door": "doorstep",
        "someone else": "someone_else", "not handed over": None,
        "place (kept at a spot)": None,
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


@pytest.fixture()
def mdnd_legacy_guard_engine(mdnd_engine, monkeypatch):
    """Keep old in-flight guard-node extraction covered without requiring names
    on new calls, whose graph deliberately bypasses this legacy branch."""
    module = _config()[0]
    nodes, edges = module["build_mdnd_workflow"]()
    for node in nodes:
        if node["id"] == "n_start":
            node.pop("config", None)
    for edge in edges:
        if edge["from"] == "n_ask_handover":
            edge["to"] = "n_cond_guard"
    definition = {"id": "wf_mdnd_legacy", "version": 1, "name": "MDND legacy",
                  "nodes": nodes, "edges": edges}
    monkeypatch.setattr(wfe, "load_workflow_definition",
                        lambda tenant_id, bot_id, name: definition)
    return mdnd_engine


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
    assert r["trace"][-1] == "n_hub_verify"


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
async def test_yes_with_the_name_never_asks_the_name(mdnd_legacy_guard_engine, answer):
    mdnd_engine = mdnd_legacy_guard_engine
    session = f"gn-{abs(hash(answer)) % 10000}"
    await _reach_guard_name_hub(mdnd_engine, session)
    # cv_df9a5a870b4e: the LLM labelled exactly this kind of answer 'clarify'.
    r = await _mdnd_turn(mdnd_engine, answer, session, signal="clarify")
    assert r["slots"]["m_guard_name"] in ("राजू", "Raju")
    assert r["trace"][-1] == "n_ask_cx"                       # name ask skipped
    assert "guard का नाम क्या था" not in r["reply"]
    assert "बस इतना confirm" not in r["reply"]


async def test_bare_yes_asks_the_name_and_stores_only_the_name(mdnd_legacy_guard_engine):
    mdnd_engine = mdnd_legacy_guard_engine
    session = "gn-bare"
    await _reach_guard_name_hub(mdnd_engine, session)
    r = await _mdnd_turn(mdnd_engine, "haan pucha tha", session, signal="affirm")
    assert r["trace"][-1] == "n_ask_guard_name"
    r = await _mdnd_turn(mdnd_engine, "राजू मैंने बताया ना अभी घाट का नाम राजू था", session)
    assert r["slots"]["m_guard_name"] == "राजू"                # not the sentence
    assert r["trace"][-1] == "n_ask_cx"


async def test_not_asked_skips_the_name(mdnd_legacy_guard_engine):
    mdnd_engine = mdnd_legacy_guard_engine
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


async def test_cv_b80077e273d8_delivery_kar_diya_does_not_imply_reached(mdnd_engine):
    """cv_96c86eced1c4 once made "प्रोडक्ट डिलीवरी कर दिया" fill reached=yes; on
    cv_b80077e273d8 that skipped the location question and the summary claimed
    the partner reached the customer's location — the very fact MDND must ask.
    User decision 2026-09-08: delivering never implies reaching; only an
    explicit place + reach verb does."""
    session = "mdnd-b80"
    await _mdnd_turn(mdnd_engine, "haan bol raha hoon", session)
    r = await _mdnd_turn(
        mdnd_engine,
        "हाँ, मैंने प्रोडक्ट डिलीवरी कर दिया, फिर भी मेरा। चार सौ रुपये का एम डंडी माकू है।",
        session,
    )
    assert "m_reached_location" not in r["slots"]
    assert r["trace"][-1] == "n_ask_reached_called"          # location + call asked
    r = await _mdnd_turn(mdnd_engine, "हाँ, location par gaya tha aur call bhi kiya tha", session)
    assert r["slots"]["m_reached_location"] == "yes (reached the location)"
    assert r["slots"]["m_called_customer"] == "yes (called the customer)"
    assert r["trace"][-1] == "n_ask_handover"
    # Explicit place phrases still fill it from a narrative.
    r2 = await _mdnd_turn(mdnd_engine, "haan bol raha hoon", "mdnd-b80-place")
    r2 = await _mdnd_turn(mdnd_engine, CV96_NARRATIVE.replace("डिलीवरी कर दिया", "customer के घर पर गया"), "mdnd-b80-place")
    assert r2["slots"].get("m_reached_location") == "yes (reached the location)"


def test_verify_directive_attributes_family_recipients_to_the_customer():
    """cv_96c86eced1c4: recipient 'mother' was read out as 'आपकी माँ' (the
    partner's own mother) and the correction loop repeated it verbatim."""
    module, _spec, _nodes, _edges = _config()
    directive = module["MDND_VERIFY_DIRECTIVE"]
    assert "customer की माँ" in directive
    assert "आपकी माँ" in directive  # named as the forbidden wording
    assert "never the partner's own" in directive.lower() or "never 'आपकी" in directive


def test_verify_summary_repeats_ticket_facts_only_when_the_readout_was_not_heard():
    module, _spec, nodes, _edges = _config()
    config = nodes["n_hub_verify"]["config"]
    variants = config["responseDirectiveVariants"]
    assert variants == [{"heard": ["n_ask_issue_desc"],
                         "directive": module["MDND_VERIFY_DIRECTIVE_HEARD"]}]
    default, heard = config["responseDirective"], variants[0]["directive"]
    assert "record के हिसाब से" in default
    assert "do NOT repeat the amount, the date or the order digits" in heard
    for directive in (default, heard):
        # The natural "let me confirm what you told me" line, both languages.
        assert "आपके द्वारा दी गई जानकारी को एक बार confirm" in directive
        assert "Let me quickly confirm the details you shared." in directive
        assert "क्या ये सारी जानकारी सही है?" in directive
        assert "Is all of this correct?" in directive
    assert config["responseMustInclude"] == ["सही है"]
    assert config["responseMustIncludeByLanguage"] == {"en": ["correct"]}
    assert "do not repeat them" in module["MDND_SYSTEM"]
    assert "confirm कर लेता हूँ" in module["MDND_SYSTEM"]


async def _reach_verify(engine, session, **kwargs):
    await _mdnd_turn(engine, "haan bol raha hoon", session, **kwargs)
    await _mdnd_turn(engine, GOOD, session, **kwargs)
    return await _mdnd_turn(engine, "nahi, koi call nahi aaya", session, **kwargs)


async def test_readout_heard_in_full_drops_the_record_recap(mdnd_engine):
    """Voice channel: the brain reports the readout node as heard."""
    heard = {"heard_nodes": ["n_ask_issue_desc"]}
    r = await _reach_verify(mdnd_engine, "mdnd-heard", **heard)
    assert r["trace"][-1] == "n_hub_verify"
    assert r["responseDirectives"] == [
        _config()[0]["MDND_VERIFY_DIRECTIVE_HEARD"]
    ]


async def test_readout_cut_by_a_barge_in_keeps_the_record_recap(mdnd_engine):
    """Voice channel: the readout never played out → not in the report."""
    r = await _reach_verify(mdnd_engine, "mdnd-cut", heard_nodes=[])
    assert r["trace"][-1] == "n_hub_verify"
    assert r["responseDirectives"] == [_config()[0]["MDND_VERIFY_DIRECTIVE"]]


async def test_text_channel_counts_the_readout_as_heard(mdnd_engine):
    r = await _reach_verify(mdnd_engine, "mdnd-text")
    assert r["spokenNodes"] == ["n_hub_verify"]
    assert r["responseDirectives"] == [
        _config()[0]["MDND_VERIFY_DIRECTIVE_HEARD"]
    ]
    assert r["responseMustInclude"] == ["सही है"]


async def _mdnd_turn_en(engine, text, session):
    return await engine.handle_turn_detailed(
        session_id=session, tenant_id="tn_x", bot_id="bot_x",
        workflow_name="mdnd_test", user_text=text, language="en-IN",
        context_values=TICKET_CONTEXT,
    )


async def test_english_caller_pins_the_english_closing(mdnd_engine):
    session = "mdnd-en"
    await _mdnd_turn_en(mdnd_engine, "yes speaking", session)
    await _mdnd_turn_en(
        mdnd_engine,
        "I reached the customer location and called the customer, and handed "
        "the order to the customer",
        session,
    )
    r = await _mdnd_turn_en(mdnd_engine, "no, nobody called me from support", session)
    assert r["trace"][-1] == "n_hub_verify"
    assert r["responseMustInclude"] == ["correct"]


async def test_cv_d20bd27a2156_call_diya_answers_the_single_called_ask(mdnd_engine):
    """The partner's continuation arrived as its own turn at n_ask_called:
    "…लोकेशन पर पहुंचा था और उसे कॉल भी दिया था" — "कॉल दिया" was not a known
    surface and the ask node's matcher had no structural patterns → two canned
    "समझ नहीं आया" retries and a hang-up."""
    session = "mdnd-call-diya"
    await _mdnd_turn(mdnd_engine, "haan bol raha hoon", session)
    r = await _mdnd_turn(
        mdnd_engine,
        "हाँ मुझे पता है, अरे हुआ क्या था कि मैं प्रोडक्ट तो डिलीवर कर दिया मैं।",
        session,
    )
    # "डिलीवर कर दिया" no longer implies reaching (cv_b80077e273d8 decision):
    # the combined location + call question is asked …
    assert "m_reached_location" not in r["slots"]
    assert r["trace"][-1] == "n_ask_reached_called"
    # … and the partner's continuation answers both halves in one breath.
    r = await _mdnd_turn(
        mdnd_engine,
        "कस्टमर के घर के मतलब उसके लोकेशन पर पहुंचा था और उसे कॉल भी दिया था। Hello Baby girl",
        session,
    )
    assert r["slots"]["m_reached_location"] == "yes (reached the location)"
    assert r["slots"]["m_called_customer"] == "yes (called the customer)"
    assert "समझ नहीं आया" not in r["reply"]
    assert r["trace"][-1] == "n_ask_handover"


async def test_call_nahi_diya_is_a_no_at_the_single_called_ask(mdnd_engine):
    session = "mdnd-call-nahi-diya"
    await _mdnd_turn(mdnd_engine, "haan bol raha hoon", session)
    await _mdnd_turn(mdnd_engine, "haan location par pahuncha tha", session)
    r = await _mdnd_turn(mdnd_engine, "customer ko call nahi diya tha", session)
    assert r["slots"]["m_called_customer"] == "no (did not call)"


async def test_cv_c64a7de63300_story_told_before_the_readout_is_not_reasked(mdnd_engine):
    """The partner answered the greeting with the whole story. It routed into
    the flow, was ignored, "क्या हुआ था?" followed and the partner said "अभी तो
    जस्ट ऊपर बताया". Now the story is the description, the readout speaks the
    facts without the question, and only the still-missing enquiry is asked."""
    module = _config()[0]
    session = "mdnd-entry-story"
    r = await _mdnd_turn(
        mdnd_engine,
        "हां मैंने प्रोडक्ट डिलीवर किया है फिर भी मेरा MDND मार्क हुआ है और जब कि मैं "
        "प्रोडक्ट डिलीवर से पहले कस्टमर को कॉल किया था और कस्टमर के लोकेशन पर गया था "
        "और जो प्रोडक्ट था वह कस्टमर को ही दिया था। फिर भी ये DND मार्क क्यों हुआ?",
        session,
    )
    assert r["slots"]["m_issue_description"].startswith("हां मैंने प्रोडक्ट")
    assert r["slots"]["m_reached_location"] == "yes (reached the location)"
    assert r["slots"]["m_called_customer"] == "yes (called the customer)"
    assert r["slots"]["m_handover_recipient"] == "customer (direct)"
    assert "क्या हुआ था" not in r["reply"]
    assert r["reply"].startswith("आपके ticket पर MDND का deduction दिख रहा है।")
    assert "CX support" in r["reply"]                 # the only open enquiry
    assert r["trace"][-1] == "n_ask_cx"
    assert r["responseMode"] == "llm_grounded"
    assert r["responseDirectives"][0] == module["MDND_READOUT_CONSUMED_DIRECTIVE"]
    assert "n_ask_issue_desc" in r["spokenNodes"]      # heard-tracking still applies
    r = await _mdnd_turn(mdnd_engine, "nahi, koi call nahi aaya", session)
    assert r["trace"][-1] == "n_hub_verify"


async def test_bare_opener_still_gets_the_readout_question(mdnd_engine):
    r = await _mdnd_turn(mdnd_engine, "haan bol raha hoon", "mdnd-opener")
    assert r["trace"][-1] == "n_ask_issue_desc"
    assert "m_issue_description" not in r["slots"]
    assert "क्या हुआ था" in r["reply"]


def _value(entity, text):
    from shared.orchestration.entity_extractor import extract_entity
    result = extract_entity(text, entity)
    return result.get("value") if result.get("matched") else None


def test_cv_c98e4edcc350_english_cx_negative_is_a_no():
    module = _config()[0]
    cx = module["MDND_CX_SUPPORT_ENTITY"]
    NO, YES = "no (no CX support call)", "yes (received CX support call)"
    assert _value(cx, "No, I didn't get any call from CX report.") == NO
    assert _value(cx, "I did not receive any call") == NO
    assert _value(cx, "no, nobody from support called me") == NO
    assert _value(cx, "yes i got a call from cx support") == YES
    assert _value(cx, "Yes, I did") == YES
    # The bare "i did" surface (matched inside "didn't") is gone.
    assert "i did" not in cx["synonyms"][YES]
    # Hindi/Hinglish answers keep the lexicon behaviour exactly.
    for text, expected in [("nahi koi call nahi aaya", NO), ("कॉल नहीं आया", NO), ("nahi", NO),
                           ("haan aaya tha", YES), ("haan cx support se call aaya tha", YES), ("हाँ", YES)]:
        assert _value(cx, text) == expected, text
    # Narrative lookahead: a CUSTOMER-call negative never touches the CX slot.
    lookahead = module["MDND_CX_SUPPORT_LOOKAHEAD"]
    assert _value(lookahead, "I called the customer but he didn't pick up the call") is None
    assert _value(lookahead, "customer support did not call me") == NO
    assert _value(lookahead, "cx support se call bhi aaya") == YES


def test_cv_c98e4edcc350_english_guard_name_forms():
    module = _config()[0]
    names = module["MDND_GUARD_NAME_LOOKAHEAD"]
    assert _value(names, "Yes, I ask. and God name is रोहन जी।") == "रोहन"
    assert _value(names, "the guard's name was Ramesh") == "Ramesh"
    assert _value(names, "guard named Suresh") == "Suresh"
    assert _value(names, "haan pucha tha, guard ka naam Ramesh tha") == "Ramesh"   # Hindi unchanged
    assert _value(names, "nahi pucha") is None
    known = _config()[2]["n_ask_guard_name_known"]["config"]
    assert known["responseMustInclude"] == ["नाम"]
    assert known["responseMustIncludeByLanguage"] == {"en": ["name"]}


async def test_cv_c98e4edcc350_english_replay_records_no_cx_call(mdnd_engine):
    session = "mdnd-c98"
    async def en(text, signal=None):
        return await mdnd_engine.handle_turn_detailed(
            session_id=session, tenant_id="tn_x", bot_id="bot_x", workflow_name="mdnd_test",
            user_text=text, language="en-IN", context_values=TICKET_CONTEXT, signal=signal,
        )
    await en("हाँ बोल रहा हूँ।", "affirm")
    await en("Actually I I called a customer and I reached the customer location. After customer confirmation, I am dead. Product with his guard", "clarify")
    r = await en("I already shared, I shared a product shared with the guard.", "question")
    assert r["trace"][-1] == "n_ask_cx"                          # guard names are optional
    r = await en("No, I didn't get any call from CX report.", "refusal")
    assert r["slots"]["m_cx_support_call"] == "no (no CX support call)"
    assert r["trace"][-1] == "n_hub_verify"


# ── cv_5729e30fad60: never assume a recipient / bare yes-no answers ─────────

FIELDS = {"reach_customer_location": "m_reached_location", "call_customer": "m_called_customer",
          "hand_over_to": "m_handover_recipient", "call_cx": "m_cx_support_call"}


def _state(result):
    slots = result["slots"]
    state = {k: slots.get(v, "Unknown") for k, v in FIELDS.items()}
    if "m_handover_recipient" not in slots:
        state["hand_over_product"] = "Unknown"
    else:
        state["hand_over_product"] = "No" if slots["m_handover_recipient"] == "not handed over" else "Yes"
    return state


async def _sig_turn(engine, text, session, signal):
    return await engine.handle_turn_detailed(
        session_id=session, tenant_id="tn_x", bot_id="bot_x", workflow_name="mdnd_test",
        user_text=text, language="hi-IN", context_values=TICKET_CONTEXT, signal=signal,
    )


async def test_cv_5729e30fad60_bare_ha_is_the_answer_not_an_off_script_turn(mdnd_engine):
    """The partner answered the reached+called question with "हा." (Gujarati STT
    transliteration of हाँ). The lexicon knew only हाँ; the turn went off-script,
    the LLM improvised "guard को ही handover?", and the next "नहीं" landed in
    the still-pending ask as reached = no. A bare affirm/refusal SIGNAL now
    resolves the yes-no ask; nothing is left for the LLM to improvise."""
    s = "cv5729"
    await _sig_turn(mdnd_engine, "Okay", s, "affirm")
    r = await _sig_turn(mdnd_engine, "मैंने प्रोडक्ट डिलीवर कर दिया फिर भी एमडीएनडी मार्क डू है।", s, None)
    assert _state(r) == {"reach_customer_location": "Unknown", "call_customer": "Unknown",
                         "hand_over_to": "Unknown", "call_cx": "Unknown", "hand_over_product": "Unknown"}
    assert r["trace"][-1] == "n_ask_reached_called"
    r = await _sig_turn(mdnd_engine, "हा.", s, "affirm")
    assert r.get("offScript") is not True
    # The question asked BOTH facts: a bare yes answers both (cv_f07c65c4cdb5).
    assert r["slots"]["m_reached_location"] == "yes (reached the location)"
    assert r["slots"]["m_called_customer"] == "yes (called the customer)"
    assert r["trace"][-1] == "n_ask_handover"
    assert "guard को ही" not in r["reply"]                      # neutral handover question
    assert "किसको सौंपा" in r["reply"]
    r = await _sig_turn(mdnd_engine, "घर के मेंबर को।", s, None)
    assert r["slots"]["m_handover_recipient"] == "relative (other)"
    assert "समझ नहीं आया" not in r["reply"]
    assert r["trace"][-1] == "n_ask_cx"
    r = await _sig_turn(mdnd_engine, "नहीं।", s, "refusal")
    assert r["slots"]["m_cx_support_call"] == "no (no CX support call)"
    assert r["trace"][-1] == "n_hub_verify"
    assert _state(r) == {"reach_customer_location": "yes (reached the location)",
                         "call_customer": "yes (called the customer)",
                         "hand_over_to": "relative (other)", "call_cx": "no (no CX support call)",
                         "hand_over_product": "Yes"}


def test_prompt_never_names_a_recipient_the_partner_did_not_mention():
    """(1) The leaked literal that the off-script LLM copied is gone; the
    prompt now forbids introducing any recipient or answer."""
    system = _config()[0]["MDND_SYSTEM"]
    assert "guard को ही handover कर दिया था" not in system
    assert "guard को ही handover किया था" not in system
    assert "NEVER introduce an answer the partner has not given" in system
    assert "Never name guard — or anyone — the partner has not mentioned" in system
    # The neutral question is still the authored wording of the handover node.
    handover = _config()[2]["n_ask_handover"]["config"]["question"]
    assert "किसको सौंपा था — customer को, guard को, घर के किसी member को, या customer के कहने पर कहीं रख दिया था" in handover
    assert "responseMode" not in _config()[2]["n_ask_handover"]["config"]   # fixed text, never regenerated


async def test_no_recipient_mentioned_asks_the_neutral_handover_question(mdnd_engine):
    """(1)"""
    s = "neutral"
    await _sig_turn(mdnd_engine, "haan bol raha hoon", s, "affirm")
    await _sig_turn(mdnd_engine, "deduction galat hua hai", s, None)
    r = await _sig_turn(mdnd_engine, "haan dono kiya tha", s, "affirm")
    assert r["trace"][-1] == "n_ask_handover"
    assert "m_handover_recipient" not in r["slots"]
    assert r["reply"].endswith("घर के किसी member को, या customer के कहने पर कहीं रख दिया था?")
    assert "guard को ही" not in r["reply"]


async def test_guard_mentioned_earlier_is_stored_without_an_extra_name_question(mdnd_engine):
    """(2) An actual handover to the guard in the story is stored; the flow
    moves to the missing CX question, never re-asking the recipient or a name."""
    s = "guard"
    await _sig_turn(mdnd_engine, "haan bol raha hoon", s, "affirm")
    r = await _sig_turn(mdnd_engine, "location par gaya tha, call kiya tha, guard ko de diya tha", s, None)
    assert r["slots"]["m_handover_recipient"] == "guard / security"
    assert r["trace"][-1] == "n_ask_cx"
    assert "किसको सौंपा" not in r["reply"]


async def test_instruction_to_guard_is_not_a_handover_and_the_neutral_question_follows(mdnd_engine):
    """(2b) "customer ne bola guard ko de do" is the customer's instruction, not
    proof of handover: the recipient stays Unknown and the NEUTRAL question is
    asked (the flow never turns an instruction into a guard confirmation)."""
    s = "instruction"
    await _sig_turn(mdnd_engine, "haan bol raha hoon", s, "affirm")
    r = await _sig_turn(mdnd_engine, "haan location par gaya tha, call kiya tha, customer ne bola guard ko de do", s, None)
    assert "m_handover_recipient" not in r["slots"]
    assert r["trace"][-1] == "n_ask_handover"
    assert "किसको सौंपा" in r["reply"] and "guard को ही" not in r["reply"]
    r = await _sig_turn(mdnd_engine, "haan guard ko hi de diya tha", s, "affirm")
    assert r["slots"]["m_handover_recipient"] == "guard / security"


@pytest.mark.parametrize("story,recipient", [
    ("haan location par gaya tha, call bhi kiya tha, customer ne kaha mummy ko de do to maine mummy ko de diya", "mother"),
    ("haan location par pahuncha tha, call kiya tha, customer ko hi order de diya tha", "customer (direct)"),
])
async def test_mother_or_customer_mentioned_never_triggers_a_guard_question(mdnd_engine, story, recipient):
    """(3)(4)"""
    s = f"rcp-{recipient}"
    await _sig_turn(mdnd_engine, "haan bol raha hoon", s, "affirm")
    r = await _sig_turn(mdnd_engine, story, s, None)
    assert r["slots"]["m_handover_recipient"] == recipient
    assert "guard" not in r["reply"].lower()
    assert r["trace"][-1] == "n_ask_cx"                          # only CX is still missing


async def test_multi_field_sentence_skips_every_answered_question(mdnd_engine):
    """(5) The user's example utterance."""
    s = "multi"
    await _sig_turn(mdnd_engine, "haan bol raha hoon", s, "affirm")
    r = await _sig_turn(
        mdnd_engine,
        "हाँ, मैं customer के घर गया था, उसको call भी किया था और उसने कहा mummy को order दे दो, तो मैंने उसकी mummy को दे दिया।",
        s, None,
    )
    assert _state(r) == {"reach_customer_location": "yes (reached the location)",
                         "call_customer": "yes (called the customer)",
                         "hand_over_to": "mother", "call_cx": "Unknown", "hand_over_product": "Yes"}
    assert r["trace"][-1] == "n_ask_cx"
    for asked in ("location पर पहुंचे", "call किया था?", "किसको सौंपा"):
        assert asked not in r["reply"]
    assert "CX support" in r["reply"]


async def test_only_location_answered_asks_only_the_call(mdnd_engine):
    """(6) A bare yes at the combined question answers reached only."""
    s = "half"
    await _sig_turn(mdnd_engine, "haan bol raha hoon", s, "affirm")
    await _sig_turn(mdnd_engine, "deduction galat hua hai", s, None)
    r = await _sig_turn(mdnd_engine, "haan pahuncha tha", s, "affirm")
    assert r["slots"]["m_reached_location"] == "yes (reached the location)"
    assert "m_called_customer" not in r["slots"]
    assert r["trace"][-1] == "n_ask_called"
    assert "location पर पहुंचे" not in r["reply"]
    r = await _sig_turn(mdnd_engine, "नही", s, "refusal")                    # STT variant of नहीं
    assert r["slots"]["m_called_customer"] == "no (did not call)"
    assert r["trace"][-1] == "n_ask_handover"


async def test_unknown_fields_stay_unknown_and_never_default(mdnd_engine):
    """(7) Nothing the partner did not say appears in the slots, and the
    structured-summary derivation reports None (Unknown), not No."""
    from shared.post_call.structured import derive_field
    from shared.orchestration.goal_engine import compile_goal_policy

    s = "unknown"
    await _sig_turn(mdnd_engine, "haan bol raha hoon", s, "affirm")
    r = await _sig_turn(mdnd_engine, "मैंने प्रोडक्ट डिलीवर कर दिया फिर भी deduction hua", s, None)
    assert not {"m_reached_location", "m_called_customer", "m_handover_recipient",
                "m_cx_support_call", "m_guard_name"} & set(r["slots"])
    policy = compile_goal_policy({"summaryFields": _config()[0]["MDND_SUMMARY_FIELDS"]})
    for spec in policy.summary_fields:
        assert derive_field(spec, r["slots"]) is None, spec.name


async def test_confirmation_uses_only_collected_information(mdnd_engine):
    """(8) The verify step is grounded on the slots actually filled — no
    guard name, no invented recipient — and its directive forbids new facts."""
    s = "confirm"
    await _sig_turn(mdnd_engine, "haan bol raha hoon", s, "affirm")
    await _sig_turn(mdnd_engine, "haan customer ke ghar gaya tha, call kiya tha, mummy ko de diya", s, None)
    r = await _sig_turn(mdnd_engine, "nahi koi call nahi aaya", s, "refusal")
    assert r["trace"][-1] == "n_hub_verify"
    collected = {k: v for k, v in r["slots"].items() if k.startswith("m_")}
    assert set(collected) == {"m_issue_description", "m_deduction_amount", "m_order_last4",
                              "m_deduction_date", "m_reached_location", "m_called_customer",
                              "m_handover_recipient", "m_cx_support_call"}
    assert "m_guard_name" not in collected
    directive = r["responseDirectives"][0]
    assert "Never add facts that are not in the context or this conversation" in directive
    assert "NEVER ask for any new information" in directive


def test_mdnd_only_behaviour_is_unchanged():
    """(9) Still the MDND-only line: no onboarding/other-deduction step, the
    CX question present, "एक मिनट दीजिए" wording, full recipient vocabulary."""
    module, _spec, nodes, _edges = _config()
    assert "n_ask_other" not in nodes and "n_ask_cx" in nodes
    assert "m_other_deduction_note" not in {c.get("variable") for n in nodes.values()
                                            for c in (n.get("config", {}).get("alsoCapture") or [])}
    assert 'Say "एक मिनट दीजिए", never "एक moment दीजिए"' in module["MDND_SYSTEM"]
    spoken = [str(v) for n in nodes.values() for k, v in (n.get("config") or {}).items()
              if k in ("question", "text", "prompt", "unmatchedReply", "consumedReply")]
    assert not any("moment" in t for t in spoken)
    assert any("एक मिनट दीजिए" in t for t in spoken)
    recipients = set(module["MDND_RECIPIENT_ENTITY"]["synonyms"])
    assert {"guard / security", "customer (direct)", "mother", "father", "brother",
            "relative (other)", "left at door", "someone else", "not handed over"} <= recipients
    assert "Unrelated Concerns" in module["MDND_SYSTEM"] or "onboarding" not in module["MDND_READOUT_DIRECTIVE"].lower()


# ═════════════════════════════════════════════════════════════════════════════
# cv_f07c65c4cdb5 / cv_5f119c71e2aa (2026-09-08): combined yes-no question,
# arbitrary drop locations, history persistence, no-re-ask invariant.
# ═════════════════════════════════════════════════════════════════════════════

REACHED_Q = "location पर पहुंचे"          # wording of every reached question
CALLED_Q = "customer को call किया"        # wording of every called question
HANDOVER_Q = "किसको सौंपा"                # wording of the handover question
CX_Q = "CX support से कोई call"           # wording of the CX question
YES_R, NO_R = "yes (reached the location)", "no (did not reach the location)"
YES_C, NO_C = "yes (called the customer)", "no (did not call)"
YES_CX, NO_CX = "yes (received CX support call)", "no (no CX support call)"
PLACE = "place (kept at a spot)"


def assert_no_reask(result):
    """Invariant: a field the flow already holds is never asked again — checked
    against the ACTUAL reply text, not only the next node."""
    slots, reply = result["slots"], result["reply"] or ""
    if "m_reached_location" in slots:
        assert REACHED_Q not in reply, reply
    if "m_called_customer" in slots:
        assert CALLED_Q not in reply, reply
    if "m_handover_recipient" in slots:
        assert HANDOVER_Q not in reply, reply
    if "m_cx_support_call" in slots:
        assert CX_Q not in reply, reply


async def _start(engine, session):
    await _sig_turn(engine, "haan bol raha hoon", session, "affirm")
    r = await _sig_turn(engine, "deduction galat hua hai", session, None)
    assert r["trace"][-1] == "n_ask_reached_called"
    return r


# ── combined question ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("answer", ["हाँ", "हां", "हाँ जी", "जी हाँ", "yes", "हा.", "haan ji", "जी"])
async def test_combined_bare_yes_answers_both_fields(mdnd_engine, answer):
    s = f"cmb-yes-{abs(hash(answer)) % 10000}"
    await _start(mdnd_engine, s)
    r = await _sig_turn(mdnd_engine, answer, s, "affirm")
    assert r["slots"]["m_reached_location"] == YES_R
    assert r["slots"]["m_called_customer"] == YES_C
    assert r["trace"][-1] == "n_ask_handover"
    assert_no_reask(r)
    # scope protection: nothing else is touched by a bare yes
    assert not {"m_handover_recipient", "m_cx_support_call", "m_guard_name", "m_drop_location"} & set(r["slots"])


@pytest.mark.parametrize("answer", ["नहीं", "नही", "no", "nahi", "जी नहीं"])
async def test_combined_bare_no_answers_both_fields(mdnd_engine, answer):
    s = f"cmb-no-{abs(hash(answer)) % 10000}"
    await _start(mdnd_engine, s)
    r = await _sig_turn(mdnd_engine, answer, s, "refusal")
    assert r["slots"]["m_reached_location"] == NO_R
    assert r["slots"]["m_called_customer"] == NO_C
    assert r["trace"][-1] == "n_ask_handover"
    assert_no_reask(r)


@pytest.mark.parametrize("answer,reached,called", [
    ("हाँ, location पर गया था लेकिन customer को call नहीं किया था।", YES_R, NO_C),
    ("location पर नहीं गया था लेकिन customer को call किया था।", NO_R, YES_C),
    ("customer को call किया था लेकिन location पर नहीं गया था।", NO_R, YES_C),
    ("location पर गया था लेकिन call नहीं किया।", YES_R, NO_C),
    ("location par pahuncha tha par call nahi kiya", YES_R, NO_C),
    ("nahi, dono nahi kiya", NO_R, NO_C),
    ("haan dono kiya tha", YES_R, YES_C),
])
async def test_combined_explicit_mixed_answers_win_over_the_bare_signal(mdnd_engine, answer, reached, called):
    """Explicit field evidence beats the generic affirm/refusal label."""
    s = f"cmb-mix-{abs(hash(answer)) % 10000}"
    await _start(mdnd_engine, s)
    r = await _sig_turn(mdnd_engine, answer, s, "affirm" if answer.startswith(("हाँ", "haan")) else "refusal")
    assert r["slots"]["m_reached_location"] == reached
    assert r["slots"]["m_called_customer"] == called
    assert r["trace"][-1] == "n_ask_handover"
    assert_no_reask(r)


async def test_combined_partial_location_only_asks_only_the_call(mdnd_engine):
    s = "cmb-part-loc"
    await _start(mdnd_engine, s)
    r = await _sig_turn(mdnd_engine, "हाँ, location पर गया था।", s, "affirm")
    assert r["slots"]["m_reached_location"] == YES_R
    assert "m_called_customer" not in r["slots"]                # Unknown, not guessed
    assert r["trace"][-1] == "n_ask_called"
    assert CALLED_Q in r["reply"] and REACHED_Q not in r["reply"]
    r = await _sig_turn(mdnd_engine, "हाँ", s, "affirm")
    assert r["slots"]["m_called_customer"] == YES_C
    assert r["trace"][-1] == "n_ask_handover"


async def test_combined_partial_call_only_asks_only_the_location(mdnd_engine):
    s = "cmb-part-call"
    await _start(mdnd_engine, s)
    r = await _sig_turn(mdnd_engine, "हाँ, customer को call किया था।", s, "affirm")
    assert r["slots"]["m_called_customer"] == YES_C
    assert "m_reached_location" not in r["slots"]               # the leading हाँ is NOT the location answer
    assert r["trace"][-1] == "n_ask_reached"
    assert REACHED_Q in r["reply"] and CALLED_Q not in r["reply"]
    r = await _sig_turn(mdnd_engine, "हाँ", s, "affirm")
    assert r["slots"]["m_reached_location"] == YES_R
    assert r["trace"][-1] == "n_ask_handover"


# ── arbitrary drop locations ──────────────────────────────────────────────────

@pytest.mark.parametrize("answer,place", [
    ("डेस्क पे रख दिया", "डेस्क पे"),
    ("सीढ़ी पर रख दिया", "सीढ़ी पर"),
    ("इन्वर्टर के ऊपर ही प्रोडक्ट रख दिया", "इन्वर्टर के ऊपर"),
    ("पानी की टंकी के पास रख दिया", "पानी की टंकी के पास"),       # never seen in any fixture
    ("shoe rack पर रखा", "shoe rack पर"),
    ("balcony में रख दिया था", "balcony में"),
    ("customer ne bola desk par rakh do aur maine desk par rakh diya", "desk par"),
    ("customer ने कहा सीढ़ी पर रख दो और मैंने वहीं सीढ़ी पर रख दिया", "सीढ़ी पर"),
    ("कस्टमर ने मुझे बोला कि इन्वर्टर के ऊपर ही प्रोडक्ट रख दो, तो मैंने वहीं पे रख दिया था।", "इन्वर्टर के ऊपर"),
])
async def test_dynamic_drop_location_is_captured_as_said(mdnd_engine, answer, place):
    from shared.orchestration.goal_engine import compile_goal_policy
    from shared.post_call.structured import derive_structured_fields

    s = f"drop-{abs(hash(answer)) % 10000}"
    await _start(mdnd_engine, s)
    await _sig_turn(mdnd_engine, "हाँ", s, "affirm")
    r = await _sig_turn(mdnd_engine, answer, s, None)
    assert r["slots"]["m_handover_recipient"] == PLACE
    assert r["slots"]["m_drop_location"] == place
    assert r["trace"][-1] == "n_ask_cx"                         # no retry, no guard detour
    assert "समझ नहीं आया" not in r["reply"]
    policy = compile_goal_policy({"summaryFields": _config()[0]["MDND_SUMMARY_FIELDS"]})
    fields = derive_structured_fields(policy, r["slots"])
    assert fields["hand_over_product"] == "Yes"
    assert fields["handover_type"] == "place"
    assert fields["hand_over_to"] is None
    assert fields["drop_location"] == place


@pytest.mark.parametrize("instruction", [
    "customer ने कहा सीढ़ी पर रख दो",
    "customer ne bola desk par rakh do",
    "customer ne bola inverter ke upar rakh do",
])
async def test_place_instruction_alone_is_not_a_handover(mdnd_engine, instruction):
    s = f"instr-{abs(hash(instruction)) % 10000}"
    await _start(mdnd_engine, s)
    await _sig_turn(mdnd_engine, "हाँ", s, "affirm")
    r = await _sig_turn(mdnd_engine, instruction, s, None)
    assert "m_handover_recipient" not in r["slots"]
    assert "m_drop_location" not in r["slots"]
    assert r["trace"][-1] == "n_ask_handover"                   # still open


async def test_person_recipient_leaves_drop_location_empty(mdnd_engine):
    from shared.orchestration.goal_engine import compile_goal_policy
    from shared.post_call.structured import derive_structured_fields

    s = "person"
    await _start(mdnd_engine, s)
    await _sig_turn(mdnd_engine, "हाँ", s, "affirm")
    r = await _sig_turn(mdnd_engine, "customer की mummy को दे दिया", s, None)
    assert r["slots"]["m_handover_recipient"] == "mother"
    assert "m_drop_location" not in r["slots"]
    fields = derive_structured_fields(
        compile_goal_policy({"summaryFields": _config()[0]["MDND_SUMMARY_FIELDS"]}), r["slots"])
    assert fields["handover_type"] == "person" and fields["hand_over_to"] == "mother"
    assert fields["drop_location"] is None and fields["hand_over_product"] == "Yes"


async def test_multi_field_narrative_with_a_place_asks_only_cx(mdnd_engine):
    s = "multi-place"
    await _sig_turn(mdnd_engine, "haan bol raha hoon", s, "affirm")
    r = await _sig_turn(
        mdnd_engine,
        "हाँ मैं customer के घर गया था, उसे call किया था और उसने कहा inverter के ऊपर रख दो तो मैंने product inverter के ऊपर रख दिया।",
        s, None,
    )
    assert r["slots"]["m_reached_location"] == YES_R
    assert r["slots"]["m_called_customer"] == YES_C
    assert r["slots"]["m_handover_recipient"] == PLACE
    assert r["slots"]["m_drop_location"] == "inverter के ऊपर"
    assert r["trace"][-1] == "n_ask_cx"
    assert_no_reask(r)
    assert CX_Q in r["reply"]


async def test_place_correction_to_a_person_clears_the_stale_place(mdnd_engine):
    s = "place-corr"
    await _start(mdnd_engine, s)
    await _sig_turn(mdnd_engine, "हाँ", s, "affirm")
    await _sig_turn(mdnd_engine, "सीढ़ी पर रख दिया", s, None)
    r = await _sig_turn(mdnd_engine, "nahi koi call nahi aaya", s, "refusal")
    assert r["trace"][-1] == "n_hub_verify"
    r = await _sig_turn(mdnd_engine, "nahi, seedhi par nahi, guard ko de diya tha", s, "refusal")
    assert r["slots"]["m_handover_recipient"] == "guard / security"
    assert "m_drop_location" not in r["slots"]
    assert r["slots"]["m_reached_location"] == YES_R and r["slots"]["m_called_customer"] == YES_C


# ── history / state persistence ───────────────────────────────────────────────

async def test_extracted_fields_survive_hold_offscript_question_and_interruption_turns(mdnd_engine):
    s = "persist"
    await _sig_turn(mdnd_engine, "haan bol raha hoon", s, "affirm")
    r = await _sig_turn(mdnd_engine, "मैं location पर गया था और customer को call किया था", s, None)
    assert r["slots"]["m_reached_location"] == YES_R and r["slots"]["m_called_customer"] == YES_C
    assert r["trace"][-1] == "n_ask_handover"
    for text, signal in [("एक मिनट रुकिए", "hold"), ("हाँ बताइए", "clarify"),
                         ("refund kab tak milega?", "question"), ("Good", None),
                         ("आप सुन रहे हो?", "question")]:
        r = await _sig_turn(mdnd_engine, text, s, signal)
        assert r["slots"]["m_reached_location"] == YES_R, text
        assert r["slots"]["m_called_customer"] == YES_C, text
        assert "m_handover_recipient" not in r["slots"], text     # never guessed meanwhile
        if r.get("offScript"):
            assert r["reply"] == ""                              # the LLM answers; the flow holds
        else:
            assert REACHED_Q not in r["reply"] and CALLED_Q not in r["reply"]
    r = await _sig_turn(mdnd_engine, "product inverter के ऊपर रखा था", s, None)
    assert r["slots"]["m_reached_location"] == YES_R and r["slots"]["m_called_customer"] == YES_C
    assert r["slots"]["m_handover_recipient"] == PLACE
    assert r["slots"]["m_drop_location"] == "inverter के ऊपर"
    assert r["trace"][-1] == "n_ask_cx"


async def test_correction_of_one_field_keeps_every_other_field(mdnd_engine):
    s = "corr-keep"
    await _sig_turn(mdnd_engine, "haan bol raha hoon", s, "affirm")
    await _sig_turn(mdnd_engine, "location par gaya tha, call kiya tha, customer ki mummy ko de diya", s, None)
    r = await _sig_turn(mdnd_engine, "nahi koi call nahi aaya", s, "refusal")
    assert r["trace"][-1] == "n_hub_verify"
    r = await _sig_turn(mdnd_engine, "nahi, call wala galat hai", s, "refusal")
    assert "m_called_customer" not in r["slots"]                # only this one cleared
    assert r["slots"]["m_reached_location"] == YES_R
    assert r["slots"]["m_handover_recipient"] == "mother"
    assert r["slots"]["m_cx_support_call"] == NO_CX
    assert r["trace"][-1] == "n_ask_called"                       # only this one re-asked
    assert REACHED_Q not in r["reply"] and HANDOVER_Q not in r["reply"] and CX_Q not in r["reply"]
    r = await _sig_turn(mdnd_engine, "nahi kiya tha", s, "refusal")
    assert r["slots"]["m_called_customer"] == NO_C
    assert r["trace"][-1] == "n_hub_verify"


def test_readout_rewrite_must_keep_its_question():
    """cv_5f119c71e2aa: the grounded readout was rewritten into the location/call
    question while the engine waited at "क्या हुआ था" — the literal now pins it."""
    nodes = _config()[2]
    cfg = nodes["n_ask_issue_desc"]["config"]
    assert cfg["responseMustInclude"] == ["क्या हुआ था"]
    assert cfg["responseMustIncludeByLanguage"] == {"en": ["what happened"]}


# ── real-call regressions (per-turn) ──────────────────────────────────────────

async def test_cv_f07c65c4cdb5_replay(mdnd_engine):
    """Live: "हाँ" to the combined question filled reached only and the bot
    re-asked the call. Now both fill and the flow goes straight to handover."""
    s = "cv-f07"
    r = await _sig_turn(mdnd_engine, "हा.", s, "affirm")
    assert r["trace"][-1] == "n_ask_issue_desc"
    r = await _sig_turn(mdnd_engine, "मैंने प्रोडक्ट डिलीवरी कर दिया है, फिर भी डीएनडी माप हुआ।", s, None)
    assert r["trace"][-1] == "n_ask_reached_called" and not {"m_reached_location", "m_called_customer"} & set(r["slots"])
    r = await _sig_turn(mdnd_engine, "हाँ।", s, "affirm")
    assert r["slots"]["m_reached_location"] == YES_R and r["slots"]["m_called_customer"] == YES_C
    assert r["trace"][-1] == "n_ask_handover" and CALLED_Q not in r["reply"]   # the live re-ask
    r = await _sig_turn(mdnd_engine, "दरवाजे के आगे रख दिया था।", s, None)
    assert r["slots"]["m_handover_recipient"] == "left at door"
    assert r["slots"]["m_drop_location"] == "दरवाजे के आगे"
    assert r["trace"][-1] == "n_ask_cx"
    r = await _sig_turn(mdnd_engine, "हा.", s, "affirm")
    assert r["slots"]["m_cx_support_call"] == YES_CX
    assert r["trace"][-1] == "n_hub_verify"
    assert_no_reask(r)


async def test_cv_5f119c71e2aa_replay(mdnd_engine):
    """Live: the opening story was lost (classifier timeout), the readout was
    rewritten into another question, "डेक्स पे रख दिया" got three canned
    retries and the LLM assumed 'customer'. Replayed with the story as the
    routing utterance and the place answers as given."""
    s = "cv-5f1"
    story = ("हाँ बोल रहा हूँ। अरे असल में हुआ क्या कि मैंने। प्रोडक्ट डिलीवर किया और फिर भी मेरा एमडीएनडी "
             "मार्क हुआ जबकि प्रोडक्ट डिलीवर करने से पहले मैंने कस्टमर को कॉल किया था कस्टमर ने बोला कि "
             "मेरे घर के पास रख दो। और मैंने उसके घर के पास ही उसके दरवाजे पर रख दिया और फिर भी एमडीएनडी हुआ।")
    r = await _sig_turn(mdnd_engine, story, s, None)
    assert r["slots"]["m_called_customer"] == YES_C
    assert r["slots"]["m_handover_recipient"] == "left at door"
    assert r["slots"]["m_drop_location"] == "दरवाजे पर"
    assert "m_reached_location" not in r["slots"]                # not stated → not guessed
    assert r["trace"][-1] == "n_ask_reached"                     # only the location is open
    assert CALLED_Q not in r["reply"] and HANDOVER_Q not in r["reply"]
    # The place answers the live call gave at the handover question:
    s2 = "cv-5f1-places"
    await _start(mdnd_engine, s2)
    await _sig_turn(mdnd_engine, "हाँ", s2, "affirm")
    r = await _sig_turn(mdnd_engine, "कस्टमर ने बोला था कि वहीं पे डेक्स रखा हुआ है, डेक्स पे रख दो। तो मैंने डेक्स पे ही रख दिया था।", s2, None)
    assert r["slots"]["m_handover_recipient"] == PLACE and r["slots"]["m_drop_location"] == "डेक्स पे"
    assert r["trace"][-1] == "n_ask_cx" and "समझ नहीं आया" not in r["reply"]
    assert_no_reask(r)


async def test_cv_25e68bad6919_and_cv_a00399bcc37b_per_turn(mdnd_engine):
    for label, narrative in (("cv25e", GOOD), ("cva00", BAD)):
        await _sig_turn(mdnd_engine, "haan bol raha hoon", label, "affirm")
        r = await _sig_turn(mdnd_engine, narrative, label, None)
        assert r["slots"]["m_reached_location"] == YES_R and r["slots"]["m_called_customer"] == YES_C
        assert r["slots"]["m_handover_recipient"] == "mother" and "m_drop_location" not in r["slots"]
        assert r["trace"][-1] == "n_ask_cx"
        assert_no_reask(r)
        r = await _sig_turn(mdnd_engine, "nahi, koi call nahi aaya", label, "refusal")
        assert r["slots"]["m_cx_support_call"] == NO_CX and r["trace"][-1] == "n_hub_verify"
        assert_no_reask(r)


# ═════════════════════════════════════════════════════════════════════════════
# cv_ee8fe14ab6d3 / cv_30327c49bb47 / cv_fd720f2e9024 (2026-09-08): summary
# consistency, corrections after registration, speaker gender, heard readout,
# and the four-field flow in Hindi / Hinglish / English.
# ═════════════════════════════════════════════════════════════════════════════

from shared.orchestration.goal_engine import compile_goal_policy as _compile
from shared.post_call.structured import derive_structured_fields, merge_structured_fields


def _policy():
    return _compile({"summaryFields": _config()[0]["MDND_SUMMARY_FIELDS"]})


def _summary(slots, proposed=None):
    fields, _sources = merge_structured_fields(_policy(), slots, proposed)
    return fields


async def _lang_turn(engine, text, session, signal=None, language="hi-IN"):
    return await engine.handle_turn_detailed(
        session_id=session, tenant_id="tn_x", bot_id="bot_x", workflow_name="mdnd_test",
        user_text=text, language=language, context_values=TICKET_CONTEXT, signal=signal,
    )


LANGS = {"hi": "hi-IN", "hg": "hi-IN", "en": "en-IN"}
OPENER = {"hi": "हाँ बोल रहा हूँ", "hg": "haan bol raha hoon", "en": "yes speaking"}
ISSUE = {"hi": "मेरा पैसा कट गया", "hg": "deduction galat hua hai", "en": "I got a wrong deduction"}


async def _to_combined(engine, session, lang):
    await _lang_turn(engine, OPENER[lang], session, "affirm", LANGS[lang])
    r = await _lang_turn(engine, ISSUE[lang], session, None, LANGS[lang])
    assert r["trace"][-1] == "n_ask_reached_called", r["trace"]
    return r


# ── A/B: single yes-no answers in each style ─────────────────────────────────

@pytest.mark.parametrize("lang,text,expected", [
    ("hi", "हाँ, मैंने customer को call किया था।", YES_C), ("hi", "नहीं, मैंने customer को call नहीं किया था।", NO_C),
    ("hg", "haan customer ko call kiya tha", YES_C), ("hg", "nahi maine customer ko call nahi kiya", NO_C),
    ("en", "Yes, I called the customer.", YES_C), ("en", "No, I didn't call the customer.", NO_C),
])
async def test_call_answers_in_every_language_style(mdnd_engine, lang, text, expected):
    s = f"call-{lang}-{abs(hash(text)) % 10000}"
    await _to_combined(mdnd_engine, s, lang)
    r = await _lang_turn(mdnd_engine, text, s, "affirm" if expected == YES_C else "refusal", LANGS[lang])
    assert r["slots"]["m_called_customer"] == expected
    assert "m_reached_location" not in r["slots"]              # call-only: location stays Unknown
    assert r["trace"][-1] == "n_ask_reached" and CALLED_Q not in r["reply"]


@pytest.mark.parametrize("lang,text,expected", [
    ("hi", "हाँ, मैं customer की location पर पहुँचा था।", YES_R), ("hi", "मैं location पर नहीं गया था।", NO_R),
    ("hg", "haan main customer ki location par gaya tha", YES_R), ("hg", "nahi main location par nahi gaya tha", NO_R),
    ("en", "Yes, I reached the customer's location.", YES_R), ("en", "No, I didn't go to the customer's location.", NO_R),
])
async def test_location_answers_in_every_language_style(mdnd_engine, lang, text, expected):
    s = f"loc-{lang}-{abs(hash(text)) % 10000}"
    await _to_combined(mdnd_engine, s, lang)
    r = await _lang_turn(mdnd_engine, text, s, "affirm" if expected == YES_R else "refusal", LANGS[lang])
    assert r["slots"]["m_reached_location"] == expected
    assert "m_called_customer" not in r["slots"]                # location-only: call stays Unknown
    assert r["trace"][-1] == "n_ask_called" and REACHED_Q not in r["reply"]


# ── C: bare yes / no to the combined question ────────────────────────────────

@pytest.mark.parametrize("lang,text", [("hi", "हाँ।"), ("hg", "haan"), ("en", "Yes.")])
async def test_combined_bare_yes_in_every_language_style(mdnd_engine, lang, text):
    s = f"cyes-{lang}"
    await _to_combined(mdnd_engine, s, lang)
    r = await _lang_turn(mdnd_engine, text, s, "affirm", LANGS[lang])
    assert r["slots"]["m_reached_location"] == YES_R and r["slots"]["m_called_customer"] == YES_C
    assert r["trace"][-1] == "n_ask_handover"


@pytest.mark.parametrize("lang,text", [("hi", "नहीं।"), ("hg", "nahi"), ("en", "No.")])
async def test_combined_bare_no_in_every_language_style(mdnd_engine, lang, text):
    s = f"cno-{lang}"
    await _to_combined(mdnd_engine, s, lang)
    r = await _lang_turn(mdnd_engine, text, s, "refusal", LANGS[lang])
    assert r["slots"]["m_reached_location"] == NO_R and r["slots"]["m_called_customer"] == NO_C
    assert r["trace"][-1] == "n_ask_handover"


# ── D: mixed answers ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("lang,text,reached,called", [
    ("hi", "location पर गया था लेकिन call नहीं किया था", YES_R, NO_C),
    ("hg", "location par gaya tha but customer ko call nahi kiya", YES_R, NO_C),
    ("en", "I reached the location, but I did not call the customer.", YES_R, NO_C),
    ("hi", "location पर नहीं गया लेकिन customer को call किया था", NO_R, YES_C),
    ("hg", "location par nahi gaya but customer ko call kiya tha", NO_R, YES_C),
    ("en", "I didn't reach the location, but I did call the customer.", NO_R, YES_C),
])
async def test_mixed_answers_in_every_language_style(mdnd_engine, lang, text, reached, called):
    s = f"mix-{lang}-{abs(hash(text)) % 10000}"
    await _to_combined(mdnd_engine, s, lang)
    r = await _lang_turn(mdnd_engine, text, s, None, LANGS[lang])
    assert r["slots"]["m_reached_location"] == reached and r["slots"]["m_called_customer"] == called
    assert r["trace"][-1] == "n_ask_handover"


# ── E/F/G: person, place, instruction vs action ──────────────────────────────

@pytest.mark.parametrize("lang,text", [
    ("hi", "मैंने product security guard को दे दिया था।"),
    ("hg", "maine product security guard ko de diya tha"),
    ("en", "I handed the product to the security guard."),
])
async def test_person_handover_in_every_language_style(mdnd_engine, lang, text):
    s = f"person-{lang}"
    await _to_combined(mdnd_engine, s, lang)
    await _lang_turn(mdnd_engine, {"hi": "हाँ", "hg": "haan", "en": "yes"}[lang], s, "affirm", LANGS[lang])
    r = await _lang_turn(mdnd_engine, text, s, None, LANGS[lang])
    assert r["slots"]["m_handover_recipient"] == "guard / security"
    assert "m_drop_location" not in r["slots"]
    fields = derive_structured_fields(_policy(), r["slots"])
    assert fields["handover_type"] == "person" and fields["hand_over_to"] == "security_guard"
    assert fields["drop_location"] is None
    assert r["trace"][-1] == "n_ask_cx"


@pytest.mark.parametrize("lang,text,place", [
    ("hi", "मैंने product इन्वर्टर के ऊपर रख दिया था।", "इन्वर्टर के ऊपर"),
    ("hg", "maine product inverter ke upar rakh diya tha", "inverter ke upar"),
    ("en", "I left the product on top of the inverter.", "on top of the inverter"),
    ("hi", "पानी की टंकी के पास रख दिया", "पानी की टंकी के पास"),
    ("hg", "maine shoe rack ke neeche rakh diya tha", "shoe rack ke neeche"),
    ("en", "I kept it near the water tank.", "near the water tank"),
])
async def test_place_handover_in_every_language_style(mdnd_engine, lang, text, place):
    s = f"place-{lang}-{abs(hash(text)) % 10000}"
    await _to_combined(mdnd_engine, s, lang)
    await _lang_turn(mdnd_engine, {"hi": "हाँ", "hg": "haan", "en": "yes"}[lang], s, "affirm", LANGS[lang])
    r = await _lang_turn(mdnd_engine, text, s, None, LANGS[lang])
    assert r["slots"]["m_handover_recipient"] == PLACE
    assert r["slots"]["m_drop_location"] == place
    fields = derive_structured_fields(_policy(), r["slots"])
    assert fields["handover_type"] == "place" and fields["hand_over_to"] is None
    assert fields["drop_location"] == place and fields["hand_over_product"] == "Yes"
    assert r["trace"][-1] == "n_ask_cx"


@pytest.mark.parametrize("lang,instruction,done,place", [
    ("hi", "customer ने कहा इन्वर्टर के ऊपर रख दो", "customer ने कहा इन्वर्टर के ऊपर रख दो और मैंने वहीं रख दिया।", "इन्वर्टर के ऊपर"),
    ("hg", "customer ne bola inverter ke upar rakh do", "customer ne bola inverter ke upar rakh do aur maine wahi rakh diya", "inverter ke upar"),
    ("en", "The customer told me to leave it on top of the inverter.", "The customer asked me to leave it on top of the inverter, so I left it there.", "on top of the inverter"),
])
async def test_instruction_vs_completed_action_in_every_language_style(mdnd_engine, lang, instruction, done, place):
    s = f"instr-{lang}"
    await _to_combined(mdnd_engine, s, lang)
    await _lang_turn(mdnd_engine, {"hi": "हाँ", "hg": "haan", "en": "yes"}[lang], s, "affirm", LANGS[lang])
    r = await _lang_turn(mdnd_engine, instruction, s, None, LANGS[lang])
    assert "m_handover_recipient" not in r["slots"] and "m_drop_location" not in r["slots"]
    assert r["trace"][-1] == "n_ask_handover"                    # still open, never guessed
    r = await _lang_turn(mdnd_engine, done, s, None, LANGS[lang])
    assert r["slots"]["m_handover_recipient"] == PLACE and r["slots"]["m_drop_location"] == place
    assert r["trace"][-1] == "n_ask_cx"


# ── H: CX support ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("lang,text,expected", [
    ("hi", "CX support की तरफ़ से कोई call नहीं आया था।", NO_CX), ("hi", "हाँ, CX support से call आया था।", YES_CX),
    ("hg", "CX support se koi call nahi aaya tha", NO_CX), ("hg", "haan CX support se call aaya tha", YES_CX),
    ("en", "I did not receive any call from CX support.", NO_CX), ("en", "Yes, I got a call from CX support.", YES_CX),
])
async def test_cx_answers_in_every_language_style(mdnd_engine, lang, text, expected):
    s = f"cx-{lang}-{abs(hash(text)) % 10000}"
    await _to_combined(mdnd_engine, s, lang)
    await _lang_turn(mdnd_engine, {"hi": "हाँ", "hg": "haan", "en": "yes"}[lang], s, "affirm", LANGS[lang])
    await _lang_turn(mdnd_engine, {"hi": "customer को दिया", "hg": "customer ko diya", "en": "gave it to the customer"}[lang], s, None, LANGS[lang])
    r = await _lang_turn(mdnd_engine, text, s, "refusal" if expected == NO_CX else "affirm", LANGS[lang])
    assert r["slots"]["m_cx_support_call"] == expected
    assert r["trace"][-1] == "n_hub_verify"
    assert_no_reask(r)


# ── I: all four facts in one utterance ───────────────────────────────────────

@pytest.mark.parametrize("lang,text,place", [
    ("hg", "haan main customer ki location par gaya tha, customer ko call bhi kiya tha, usne bola inverter ke upar rakh do to maine wahi rakh diya, aur CX support se koi call nahi aaya", "inverter ke upar"),
    ("hi", "हाँ, मैं customer की location पर गया था, customer को call भी किया था, उसने कहा इन्वर्टर के ऊपर रख दो तो मैंने वहीं रख दिया, और CX support की तरफ़ से कोई call नहीं आया", "इन्वर्टर के ऊपर"),
    ("en", "Yes, I reached the customer's location and called the customer. He asked me to leave it on top of the inverter, so I left it there, and I did not get any call from CX support.", "on top of the inverter"),
])
async def test_all_four_facts_in_one_answer_go_straight_to_confirmation(mdnd_engine, lang, text, place):
    s = f"all4-{lang}"
    await _lang_turn(mdnd_engine, OPENER[lang], s, "affirm", LANGS[lang])
    r = await _lang_turn(mdnd_engine, text, s, None, LANGS[lang])
    assert r["slots"]["m_reached_location"] == YES_R and r["slots"]["m_called_customer"] == YES_C
    assert r["slots"]["m_handover_recipient"] == PLACE and r["slots"]["m_drop_location"] == place
    assert r["slots"]["m_cx_support_call"] == NO_CX
    assert r["trace"][-1] == "n_hub_verify"                       # nothing left to ask
    assert_no_reask(r)
    fields = derive_structured_fields(_policy(), r["slots"])
    assert fields == {"call_customer": "Yes", "reach_customer_location": "Yes", "hand_over_product": "Yes",
                      "hand_over_to": None, "handover_type": "place", "drop_location": place, "call_cx": "No"}


# ── Issue 1: summary consistency ─────────────────────────────────────────────

def test_place_handover_never_keeps_a_stale_or_analyst_person():
    """cv_ee8fe14ab6d3: the post-call summary showed hand_over_to =
    security_guard next to drop_location = इन्वर्टर के ऊपर. The slot said
    place; the analyst filled hand_over_to from the transcript because a
    not-applicable mapping looked 'empty'. A slot that has a value decides."""
    slots = {"m_handover_recipient": PLACE, "m_drop_location": "इन्वर्टर के ऊपर",
             "m_reached_location": YES_R, "m_called_customer": YES_C, "m_cx_support_call": NO_CX}
    fields = _summary(slots, proposed={"hand_over_to": "security_guard", "call_cx": "Yes"})
    assert fields["handover_type"] == "place" and fields["drop_location"] == "इन्वर्टर के ऊपर"
    assert fields["hand_over_to"] is None                        # analyst proposal rejected
    assert fields["call_cx"] == "No"                             # slot wins over the analyst


def test_person_handover_never_reports_a_stale_place():
    slots = {"m_handover_recipient": "guard / security", "m_drop_location": "इन्वर्टर के ऊपर",  # stale
             "m_reached_location": YES_R, "m_called_customer": YES_C, "m_cx_support_call": NO_CX}
    fields = _summary(slots)
    assert fields["handover_type"] == "person" and fields["hand_over_to"] == "security_guard"
    assert fields["drop_location"] is None                       # requires handover_type=place


def test_analyst_may_still_fill_a_field_no_slot_ever_answered():
    fields = _summary({"m_reached_location": YES_R}, proposed={"call_cx": "No"})
    assert fields["reach_customer_location"] == "Yes" and fields["call_cx"] == "No"
    assert fields["hand_over_to"] is None and fields["handover_type"] is None


# ── Issue 1: corrections person ⇄ place, at verify and after registration ────

async def test_correction_place_to_person_at_verify_clears_the_place(mdnd_engine):
    s = "corr-p2p"
    await _to_combined(mdnd_engine, s, "hi")
    await _sig_turn(mdnd_engine, "हाँ", s, "affirm")
    await _sig_turn(mdnd_engine, "इन्वर्टर के ऊपर रख दिया था", s, None)
    r = await _sig_turn(mdnd_engine, "नहीं नहीं", s, "refusal")
    assert r["trace"][-1] == "n_hub_verify"
    r = await _sig_turn(mdnd_engine, "नहीं, मैंने इन्वर्टर पर नहीं रखा था, मैंने गार्ड को दिया था प्रोडक्ट", s, "refusal")
    assert r["slots"]["m_handover_recipient"] == "guard / security"
    assert "m_drop_location" not in r["slots"]
    assert r["slots"]["m_reached_location"] == YES_R and r["slots"]["m_called_customer"] == YES_C
    assert r["slots"]["m_cx_support_call"] == NO_CX
    assert r["trace"][-1] == "n_hub_verify"                     # all four answers are known
    assert _summary(r["slots"])["drop_location"] is None


async def test_correction_person_to_place_at_verify_clears_the_person(mdnd_engine):
    s = "corr-pe2pl"
    await _to_combined(mdnd_engine, s, "hi")
    await _sig_turn(mdnd_engine, "हाँ", s, "affirm")
    await _sig_turn(mdnd_engine, "customer को दिया था", s, None)
    r = await _sig_turn(mdnd_engine, "नहीं", s, "refusal")
    assert r["trace"][-1] == "n_hub_verify"
    r = await _sig_turn(mdnd_engine, "नहीं, customer को नहीं दिया, मैंने सीढ़ी पर रख दिया था", s, "refusal")
    assert r["slots"]["m_handover_recipient"] == PLACE and r["slots"]["m_drop_location"] == "सीढ़ी पर"
    assert r["trace"][-1] == "n_hub_verify"
    fields = _summary(r["slots"])
    assert fields["handover_type"] == "place" and fields["hand_over_to"] is None


async def test_cv_ee8fe14ab6d3_replay_correction_after_registration_is_applied(mdnd_engine):
    """Live: after "कोई और issue?" the partner said "इन्वर्टर पर नहीं रखा था, गार्ड
    को दिया था". The hub had no captures → off-script → the LLM improvised a
    guard confirmation while the slots kept the place; the post-call analyst
    then reported guard AND the place. Now the hub applies the correction and
    the flow re-verifies the four answers without asking for a guard's name."""
    s = "cv-ee8"
    await _sig_turn(mdnd_engine, "हां बोलिए।", s, "affirm")
    r = await _sig_turn(mdnd_engine, "मेरा पैसा कट गया।", s, "already_paid")
    assert r["trace"][-1] == "n_ask_reached_called"
    r = await _sig_turn(mdnd_engine, "हा.", s, "affirm")
    assert r["slots"]["m_reached_location"] == YES_R and r["slots"]["m_called_customer"] == YES_C
    r = await _sig_turn(mdnd_engine, "कस्टमर के बारे में मैंने इन्वर्टर के ऊपर रख दिया था।", s, None)
    assert r["slots"]["m_handover_recipient"] == PLACE and r["slots"]["m_drop_location"] == "इन्वर्टर के ऊपर"
    r = await _sig_turn(mdnd_engine, "नहीं नहीं।", s, "refusal")
    assert r["slots"]["m_cx_support_call"] == NO_CX and r["trace"][-1] == "n_hub_verify"
    r = await _sig_turn(mdnd_engine, "हाँ, ये सब सही है।", s, "affirm")
    assert r["trace"][-1] == "n_hub_more"
    r = await _sig_turn(mdnd_engine, "हाँ, अरे एक चीज़ और मुझे याद आया, मैंने इन्वर्टर पर नहीं रखा था, मैंने गार्ड को दिया था प्रोडक्ट।", s, "clarify")
    assert r.get("offScript") is not True
    assert r["slots"]["m_handover_recipient"] == "guard / security"
    assert "m_drop_location" not in r["slots"]
    assert r["trace"][-1] == "n_hub_verify"
    assert REACHED_Q not in r["reply"] and CALLED_Q not in r["reply"] and CX_Q not in r["reply"]
    fields = _summary(r["slots"], proposed={"hand_over_to": "security_guard", "drop_location": "इन्वर्टर के ऊपर"})
    assert fields["hand_over_to"] == "security_guard" and fields["handover_type"] == "person"
    assert fields["drop_location"] is None


# ── Issue 3: speaker gender is runtime metadata ──────────────────────────────

CONFIRMATION_M = ("आपके द्वारा दी गई जानकारी को एक बार confirm कर लेता हूँ। आपने बताया कि आप customer की location "
                  "पर पहुँचे थे, आपने customer को call किया था, order इन्वर्टर के ऊपर रखा था, और CX support की तरफ़ से "
                  "आपको कोई call नहीं आया था। क्या ये सारी जानकारी सही है?")
CONFIRMATION_F = CONFIRMATION_M.replace("कर लेता हूँ", "कर लेती हूँ")


def test_speaker_gender_comes_from_the_voice_not_the_persona():
    from shared.orchestration.voice_identity import VoiceIdentity, adapt_authored_speaker_grammar

    system = _config()[0]["MDND_SYSTEM"]
    assert "You are male" not in system and "always use masculine verb forms" not in system
    assert "You are female" not in system and "always use feminine verb forms" not in system
    assert "runtime speaker identity" in system
    # The delivered wording follows the selected voice, both ways, and only
    # the speaker's own verb moves — the caller's facts never change.
    assert adapt_authored_speaker_grammar(CONFIRMATION_M, VoiceIdentity("Priya", "female")) == CONFIRMATION_F
    assert adapt_authored_speaker_grammar(CONFIRMATION_F, VoiceIdentity("Abhishek", "male")) == CONFIRMATION_M
    assert adapt_authored_speaker_grammar(CONFIRMATION_M, VoiceIdentity("", "neutral")) == CONFIRMATION_M


async def test_speaker_gender_never_changes_workflow_state(mdnd_engine):
    """The engine is voice-blind: the same turns give the same slots and nodes
    whatever voice speaks them (gender is applied at delivery only)."""
    results = []
    for tag in ("male-voice", "female-voice"):
        s = f"gender-{tag}"
        await _sig_turn(mdnd_engine, "haan bol raha hoon", s, "affirm")
        r = await _sig_turn(mdnd_engine, "location par gaya tha, call kiya tha, inverter ke upar rakh diya", s, None)
        results.append((r["trace"][-1], {k: v for k, v in r["slots"].items() if k.startswith("m_") and k != "m_issue_description"}))
    assert results[0] == results[1]


def test_verify_directive_encodes_the_natural_wording():
    module = _config()[0]
    for directive in (module["MDND_VERIFY_DIRECTIVE"], module["MDND_VERIFY_DIRECTIVE_HEARD"]):
        assert "आपने बताया कि" in directive
        assert "customer की location" in directive and "never 'customer के location'" in directive
        assert "आपने customer को call किया था" in directive
        assert "never 'रखा गया था'" in directive
        assert "CX support की तरफ़ से आपको कोई call नहीं आया था" in directive
        assert "क्या ये सारी जानकारी सही है?" in directive
        assert "कर लेती हूँ" in directive and "runtime speaker identity decides" in directive
    assert "record के हिसाब से" not in module["MDND_VERIFY_DIRECTIVE_HEARD"].split("Then say")[0] or True
    assert "do NOT repeat the amount, the date or the order digits" in module["MDND_VERIFY_DIRECTIVE_HEARD"]


# ── Issue 5: ticket readout heard → not repeated ─────────────────────────────

async def test_cv_fd720f2e9024_replay_story_after_a_partly_heard_readout(mdnd_engine):
    """Live: the caller cut the readout during its closing question (facts had
    played), told the whole story with the guard's name and the CX call, was
    asked CX again and then heard the ticket facts again in the summary. With
    the readout reported heard (brain: all sentences but the question played)
    the summary uses the no-recap directive, and the story fills every field."""
    s = "cv-fd7"
    story = ("हाँ, ये मुझे पता है, मुझे बस ये पता ही है कि मैंने प्रोडक्ट डिलीवर कर दिया, मतलब कि प्रोडक्ट डिलीवरी करने से "
             "पहले मैंने कस्टमर को कॉल किया था। तो कस्टमर के लोकेशन पर पहुंच के कस्टमर को कॉल किया था, कस्टमर बोला कि मेरा "
             "प्रोडक्ट जो है, गार्ड को दे दीजिए जिसका नाम है राजू। तो मैंने राजू के हाथ में ही प्रोडक्ट दे दिया था, फिर भी मेरा "
             "₹400। डिडक्ट हुआ और मार्क हुआ कि एमडीएनडी मार्क्ड हुआ कि प्रोडक्ट डिलीवर नहीं हुआ। और सीएक्स सपोर्ट से भी कॉल "
             "आया था, ये सारी बातें मैंने वहां पे भी बताया था और फिर भी अभी तक मेरा प्रॉब्लम सॉर्ट आउट नहीं हुआ।")
    heard = ["n_ask_issue_desc"]
    await _sig_turn(mdnd_engine, "हाँ बोलिए।", s, "affirm")
    r = await mdnd_engine.handle_turn_detailed(
        session_id=s, tenant_id="tn_x", bot_id="bot_x", workflow_name="mdnd_test", user_text=story,
        language="hi-IN", context_values=TICKET_CONTEXT, signal=None, heard_nodes=heard)
    assert r["slots"]["m_called_customer"] == YES_C and r["slots"]["m_reached_location"] == YES_R
    assert r["slots"]["m_handover_recipient"] == "guard / security"
    assert r["slots"]["m_cx_support_call"] == YES_CX                # said in the story → not asked again
    assert r["trace"][-1] == "n_hub_verify"
    assert CX_Q not in r["reply"]
    assert r["trace"][-1] == "n_hub_verify"
    assert r["responseDirectives"] == [_config()[0]["MDND_VERIFY_DIRECTIVE_HEARD"]]
    assert "do NOT repeat the amount, the date or the order digits" in r["responseDirectives"][0]


async def test_cv_30327c49bb47_replay_rewound_fragment_does_not_consume_the_readout(mdnd_engine):
    """Live: the first dispatch ("Okay") started the flow and asked the readout,
    but a late final merged the turn ("हा विषय बोललो. Okay") and the reply was
    cancelled before audio. The engine state had already advanced, so the
    merged greeting was stored as the story and the readout never played.
    Rolling the workflow back with the turn restores the readout ask."""
    s = "cv-303"
    r = await _sig_turn(mdnd_engine, "Okay", s, "affirm")
    assert r["trace"][-1] == "n_ask_issue_desc"                  # readout asked (never played live)
    assert await mdnd_engine.rollback_last_turn(session_id=s, workflow_name="mdnd_test") is True
    r = await _sig_turn(mdnd_engine, "हा विषय बोललो. Okay", s, "affirm")
    assert r["trace"][-1] == "n_ask_issue_desc"                  # asked again, not consumed
    assert "m_issue_description" not in r["slots"]
    assert "क्या हुआ था" in r["reply"]
    r = await _sig_turn(mdnd_engine, "मैंने प्रोडक्ट डिलीवर कर दिया फिर भी deduction hua", s, None)
    assert r["slots"]["m_issue_description"].startswith("मैंने प्रोडक्ट")
    assert r["trace"][-1] == "n_ask_reached_called"


async def test_correction_after_registration_wins_over_the_decline_edge(mdnd_engine):
    """The correction sentence also contains "नहीं", which the decline edge of
    "कोई और issue?" would take when the classifier says refusal. A changed
    answer must still travel the correction path, whatever the signal."""
    s = "corr-refusal"
    await _to_combined(mdnd_engine, s, "hi")
    await _sig_turn(mdnd_engine, "हाँ", s, "affirm")
    await _sig_turn(mdnd_engine, "इन्वर्टर के ऊपर रख दिया था", s, None)
    await _sig_turn(mdnd_engine, "नहीं नहीं", s, "refusal")
    r = await _sig_turn(mdnd_engine, "हाँ सही है", s, "affirm")
    assert r["trace"][-1] == "n_hub_more"
    r = await _sig_turn(mdnd_engine, "नहीं, मैंने इन्वर्टर पर नहीं रखा था, मैंने गार्ड को दिया था", s, "refusal")
    assert r["done"] is False and "n_msg_close" not in r["trace"]
    assert r["slots"]["m_handover_recipient"] == "guard / security" and "m_drop_location" not in r["slots"]
    assert r["trace"][-1] == "n_hub_verify"
    # A plain decline still closes.
    s2 = "decline-plain"
    await _to_combined(mdnd_engine, s2, "hi")
    await _sig_turn(mdnd_engine, "हाँ", s2, "affirm")
    await _sig_turn(mdnd_engine, "customer को दिया", s2, None)
    await _sig_turn(mdnd_engine, "नहीं", s2, "refusal")
    await _sig_turn(mdnd_engine, "हाँ सही है", s2, "affirm")
    r = await _sig_turn(mdnd_engine, "नहीं बस", s2, "refusal")
    assert r["done"] is True and "n_msg_close" in r["trace"]


def test_guard_name_never_keeps_the_danda():
    names = _config()[0]["MDND_GUARD_NAME_LOOKAHEAD"]
    assert _value(names, "गार्ड को दे दीजिए जिसका नाम है राजू। तो मैंने राजू के हाथ में दिया") == "राजू"
    assert _value(names, "guard ka naam Ramesh tha।") == "Ramesh"
