"""Authored follow-up/resume contracts from the two local call regressions."""
from __future__ import annotations

from copy import deepcopy
import json

from langgraph.checkpoint.memory import MemorySaver
import pytest

from haier.setup.haier_call_audit_repair import BOT_ID, WORKFLOW_ID
from haier.setup.haier_dialogue_regression_repair import (
    INTRO_PROFILES, MARKER, RESUME_PROMPTS, apply_dialogue_regression_repair,
)
import shared.orchestration.workflow_engine as wfe


def fixture():
    questions = {
        "call_or_visit": "May I know whether you received any call or visit from a Haier technician?",
        "visit": "May I know whether a Haier technician visited your place?",
        "call": "May I know whether you received a call from a Haier technician?",
    }
    nodes = [
        {"id": "start", "kind": "start"},
        {"id": "n_identity_gate", "kind": "ask", "config": {
            "prompt": "Am I speaking with [customer_name]?", "variable": "identity_answer",
            "entity": {"dataType": "text", "synonymPatterns": {
                "yes": ["^yes$"], "no": ["^no$"], "service_declined": ["service nahi chahiye"],
            }},
            "haierFollowupRepairVersion": 1, "haierFollowupConsentRepairVersion": 1,
        }},
        {"id": "n_wp_identity_ask", "kind": "ask", "config": {
            "prompt": "Please hand the phone to [customer_name].", "variable": "identity_answer_2",
            "entity": {"dataType": "text", "synonymPatterns": {"yes": ["^yes$"], "no": ["^no$"]}},
        }},
        {"id": "end", "kind": "end", "config": {"text": "Thank you."}},
    ]
    edges = [{"from": "start", "to": "n_intro_not_haier"}]
    for node_id, profile in INTRO_PROFILES.items():
        en = "This call is regarding the complaint request for your Washing Machine, which was rejected by our field team. " + questions[profile]
        hi = "ये call आपके Washing Machine की complaint request के regarding है, जो हमारी field team की तरफ़ से reject हुई है। " + RESUME_PROMPTS[profile]["hi"]
        nodes.append({"id": node_id, "kind": "intent", "config": {
            "prompt": en, "textByLanguage": {"en": en, "hi": hi},
            "responseMode": "fixed", "elseIsAnswer": True,
            "readback": {"en": {"groups": [{"template": en}]},
                         "hi": {"groups": [{"template": hi}]}},
            "alsoCapture": [{"variable": "existing_fact", "entity": {"dataType": "text"}}],
        }})
        edges.extend([{"from": node_id, "to": "end", "label": "yes"},
                      {"from": node_id, "to": "end", "label": "no"}])
    return {"id": WORKFLOW_ID, "bot_id": BOT_ID, "name": "Haier follow-up", "version": 89,
            "nodes": nodes, "edges": edges}


def test_repair_only_adds_authored_contracts_and_preserves_previous_routes_and_speech():
    original = fixture()
    backup = deepcopy(original)
    repaired = apply_dialogue_regression_repair(original)
    assert original == backup
    assert repaired["edges"] == original["edges"]
    assert len(repaired["nodes"]) == len(original["nodes"])
    changed = {before["id"] for before, after in zip(original["nodes"], repaired["nodes"]) if before != after}
    assert changed == {*INTRO_PROFILES, "n_identity_gate"}
    for before, after in zip(original["nodes"], repaired["nodes"]):
        after = deepcopy(after)
        for key in ("resumePrompt", "resumePromptByLanguage", "confirmationRole", "confirmationValues", MARKER):
            after.get("config", {}).pop(key, None)
        assert after == before
    assert apply_dialogue_regression_repair(repaired) == repaired
    assert apply_dialogue_regression_repair(json.loads(json.dumps(repaired, sort_keys=True))) == repaired


def test_identity_role_is_opt_in_only_on_the_named_customer_question():
    repaired = apply_dialogue_regression_repair(fixture())
    nodes = {node["id"]: node for node in repaired["nodes"]}
    primary = nodes["n_identity_gate"]["config"]
    assert primary["confirmationRole"] == "identity"
    assert primary["confirmationValues"] == {"confirmed": "yes", "denied": "no"}
    assert "service_declined" in primary["entity"]["synonymPatterns"]
    assert "confirmationRole" not in nodes["n_wp_identity_ask"]["config"]


@pytest.mark.parametrize("consent_version", (1, 2, 3, 4))
def test_reviewed_consent_versions_support_repair_and_idempotent_reapplication(consent_version):
    definition = fixture()
    gate = next(node for node in definition["nodes"] if node["id"] == "n_identity_gate")["config"]
    gate["haierFollowupConsentRepairVersion"] = consent_version
    repaired = apply_dialogue_regression_repair(definition)
    assert apply_dialogue_regression_repair(repaired) == repaired
    assert next(node for node in repaired["nodes"] if node["id"] == "n_identity_gate")["config"][
        "haierFollowupConsentRepairVersion"
    ] == consent_version


@pytest.mark.parametrize("consent_version", (0, 5, "3", None))
@pytest.mark.parametrize("already_repaired", (False, True))
def test_unknown_consent_version_is_rejected_before_idempotent_return(consent_version, already_repaired):
    definition = apply_dialogue_regression_repair(fixture()) if already_repaired else fixture()
    gate = next(node for node in definition["nodes"] if node["id"] == "n_identity_gate")["config"]
    gate["haierFollowupConsentRepairVersion"] = consent_version
    with pytest.raises(ValueError, match="follow-up and consent repairs"):
        apply_dialogue_regression_repair(definition)


@pytest.mark.parametrize("node_id", INTRO_PROFILES)
@pytest.mark.parametrize("language", ["en", "hi-IN"])
async def test_initial_speech_keeps_the_reason_and_pending_resume_is_only_its_question(monkeypatch, node_id, language):
    definition = apply_dialogue_regression_repair(fixture())
    definition["edges"][0]["to"] = node_id
    monkeypatch.setattr(wfe, "load_workflow_definition", lambda *args: definition)
    engine = wfe.WorkflowEngine()
    engine._checkpointer = MemorySaver()
    args = dict(session_id="dialogue-regression", tenant_id="tn_test", bot_id=BOT_ID,
                workflow_name=WORKFLOW_ID, language=language)
    initial = await engine.handle_turn_detailed(**args, user_text="")
    locale = "hi" if language.startswith("hi") else "en"
    expected = RESUME_PROMPTS[INTRO_PROFILES[node_id]][locale]
    assert "Washing Machine" in initial["reply"]
    assert ("reject" in initial["reply"] if locale == "hi" else "rejected" in initial["reply"])
    assert initial["nodeResumePrompt"] == expected
    assert initial["nodePrompt"] != expected
    paused = await engine.handle_turn_detailed(**args, user_text="Where did that happen?", pause_for_context=True)
    assert paused["reply"] == ""
    assert paused["nodeResumePrompt"] == expected
    assert paused["slots"] == initial["slots"]
    assert paused["nodePrompt"] == initial["nodePrompt"]
    assert paused["awaitingKind"] == initial["awaitingKind"] == "intent"


@pytest.mark.parametrize("use_api_alias", [False, True])
def test_workflow_identity_accepts_database_and_api_serialization(use_api_alias):
    definition = fixture()
    if use_api_alias:
        definition["botId"] = definition.pop("bot_id")
    assert apply_dialogue_regression_repair(definition)["id"] == WORKFLOW_ID


@pytest.mark.parametrize("mutation", [
    "wrong_bot", "wrong_workflow", "missing_prior_repair", "future_marker", "missing_intro",
    "wrong_question", "wrong_translation", "wrong_identity_role", "wrong_identity_values",
    "missing_yes", "existing_resume",
])
def test_unreviewed_workflow_contracts_require_review(mutation):
    definition = fixture()
    nodes = {node["id"]: node for node in definition["nodes"]}
    gate = nodes["n_identity_gate"]["config"]
    if mutation == "wrong_bot":
        definition["bot_id"] = "bot_other"
    elif mutation == "wrong_workflow":
        definition["id"] = "wf_other"
    elif mutation == "missing_prior_repair":
        gate.pop("haierFollowupConsentRepairVersion")
    elif mutation == "future_marker":
        gate[MARKER] = 2
    elif mutation == "missing_intro":
        definition["nodes"].remove(nodes["n_intro_bill"])
    elif mutation == "wrong_question":
        nodes["n_intro_bill"]["config"]["prompt"] = "Was the payment made?"
    elif mutation == "wrong_translation":
        nodes["n_intro_bill"]["config"]["textByLanguage"]["hi"] = "क्या payment हो चुका है?"
    elif mutation == "wrong_identity_role":
        gate["confirmationRole"] = "payment"
    elif mutation == "wrong_identity_values":
        gate["confirmationValues"] = {"confirmed": "no", "denied": "yes"}
    elif mutation == "missing_yes":
        gate["entity"]["synonymPatterns"].pop("yes")
    else:
        nodes["n_intro_bill"]["config"]["resumePrompt"] = "An unreviewed question?"
    with pytest.raises(ValueError):
        apply_dialogue_regression_repair(definition)
