"""Visit-permission regressions from DEMO conversation cv_f5293164ece8."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import pytest

from haier.setup.haier_followup_audit_repair import apply_followup_audit_repair
from haier.setup.haier_followup_consent_repair import HUBS, MARKER, VERSION, apply_followup_consent_repair
from tests.unit.test_haier_followup_audit_repair import fixture as previous_fixture, replay


def fixture():
    definition = apply_followup_audit_repair(previous_fixture())
    definition["bot_id"] = "bot_450a4f7b4832"
    outcome = next(node for node in definition["nodes"] if node["id"] == "m_n_v2_end_unverified")
    outcome.update(kind="message", config={"silent": True, "terminalOutcome": True,
                                           "setSlots": {"outcome": "unverified_no_action"}})
    definition["nodes"].append({"id": "n_v2_end_unverified", "kind": "end", "config": {
        "text": "Call Haier customer care to check the request status.", "responseMode": "fixed",
        "textByLanguage": {"en": "Call Haier customer care to check the request status.",
                           "hi": "Haier customer care पर call करके request का status check कर सकते हैं।"},
    }})
    definition["edges"].append({"id": "unverified_end", "from": outcome["id"], "to": "n_v2_end_unverified"})
    return definition


@pytest.mark.parametrize("hub", HUBS)
@pytest.mark.parametrize("text", [
    "हाँ, बढ़ा दीजिए।", "बढ़ा दीजिए।", "हाँ, आगे बढ़ा दीजिए।", "जी, request आगे बढ़ा दीजिए।",
    "मेरी request को आगे बढ़ा दीजिए।", "हाँ बढ़ा दीजिये", "बढा दीजिए", "बढ़ाइए।",
    "haan, badha dijiye", "badha do", "aage badha dijiye", "ji meri request aage badha dijiye",
    "yes badhaa dijiye", "badhaakar bhej dijiye", "haan aage badhakar bhej dijiye",
    "बढ़ा कर भेज दीजिए", "badhaakar dijiye", "badhaiye",
    "हाँ, बढ़ा देना।", "haan aage badha dena", "request आगे बढ़ा दें।", "aage badha de do",
    "हाँ, आगे forward कर दीजिए।", "हाँ, forward कर दो।", "request forward कर दें।",
    "जी, मेरी request आगे फ़ॉरवर्ड कर दीजिए।", "फॉरवर्ड करिए।", "फॉरवर्ड कर दीजिए।", "आगे forword कर देना।",
    "haan forward kar dijiye", "request aage forward kar do", "meri request ko forward karein",
    "ji aage forward kar dein", "yes forward kariye", "please forword kar dena",
])
def test_explicit_forward_permission_completes_without_reasking(hub, text):
    candidate = apply_followup_consent_repair(fixture())
    for definition in (candidate, json.loads(json.dumps(candidate, sort_keys=True))):
        result = asyncio.run(replay(definition, hub, text, "affirm"))
        assert result["slots"]["outcome"] == "redispatch"
        assert result["slots"][f"haier_followup_{hub}_route"] == "consent_command"
        assert "m_n_v2_end_unverified" not in result["trace"]
        assert HUBS[1] not in result["trace"][1:]


@pytest.mark.parametrize("hub", HUBS)
@pytest.mark.parametrize("text", [
    "हाँ कर दीजिए।", "हाँ, कर दीजिए।", "कर दीजिए।", "कर दीजिये।", "कर दो।", "कर देना।",
    "हाँ, कर दें।", "जी, कर दे दो।", "कीजिए।", "कीजिये।", "कीजिएगा।",
    "haan kar dijiye", "kar do", "kar dena", "kar de do", "ji kar dein",
    "kiijiye", "kijiye", "kijiyega", "keejiye", "please kiijiyega",
    "हाँ, आप कर दीजिए।", "aap kar dijiye", "haan aap kar do", "जी, आप कीजिए।",
    "कर दीजिए please", "yes kar do!",
])
def test_elided_action_imperatives_answer_the_authored_permission_without_reasking(hub, text):
    candidate = apply_followup_consent_repair(version_three_fixture())
    for definition in (candidate, json.loads(json.dumps(candidate, sort_keys=True))):
        result = asyncio.run(replay(definition, hub, text, "affirm"))
        assert result["slots"]["outcome"] == "redispatch"
        assert result["slots"][f"haier_followup_{hub}_route"] == "consent_command"
        assert "m_n_v2_end_unverified" not in result["trace"]
        assert HUBS[1] not in result["trace"][1:]


@pytest.mark.parametrize("hub", HUBS)
@pytest.mark.parametrize("text", [
    "नहीं कर दीजिए।", "कर मत दीजिए।", "मत कीजिए।", "आप मत कीजिए।",
    "nahi kar dijiye", "kar mat do", "na kar dena", "don't kar do",
    "कर दीजिए क्या?", "क्या कर दीजिए", "कब कर दीजिए?", "kar do kya",
    "kijiye?", "aap kar dijiye kyun", "can you kar do",
    "हाँ कर दिया।", "कर दिया।", "haan kar diya", "मैंने कर दिया।", "आपने कर दिया।",
    "हाँ payment कर दीजिए।", "payment kar do", "हाँ service कर दीजिए।",
    "हाँ visit हुई थी।", "हाँ payment हो गया।", "हाँ service कर दिया।", "हाँ काम कर दिया।",
    "कर दीजिए लेकिन service नहीं चाहिए।", "कर दीजिए next week", "कर दो stop calling",
])
def test_elided_action_capture_rejects_refusals_questions_reports_and_other_actions(hub, text):
    result = asyncio.run(replay(apply_followup_consent_repair(version_three_fixture()), hub, text, "affirm"))
    assert result["slots"].get("outcome") != "redispatch"
    assert result["slots"].get(f"haier_followup_{hub}_route") != "consent_command"


@pytest.mark.parametrize("hub", HUBS)
def test_new_capture_rejects_fullwidth_question_without_changing_the_prior_audit_route(hub):
    # The old audit capture already accepts this punctuation variant. The new
    # permission capture must reject it while this bounded upgrade preserves
    # the independently authored prior audit capture and its routing contract.
    original = version_three_fixture()
    before = asyncio.run(replay(original, hub, "कर दीजिए？", "affirm"))
    after = asyncio.run(replay(apply_followup_consent_repair(original), hub, "कर दीजिए？", "affirm"))
    assert after["slots"].get(f"haier_followup_{hub}_route") != "consent_command"
    assert after["trace"] == before["trace"]
    assert after["slots"] == before["slots"]


@pytest.mark.parametrize("hub", HUBS)
@pytest.mark.parametrize("text", [
    "नहीं, बढ़ा दीजिए।", "आगे मत बढ़ा दीजिए।", "badha mat dijiye", "nahi badha dijiye",
    "हाँ, बढ़ा दीजिए क्या?", "क्या आगे बढ़ा दीजिए", "badha dijiye kya", "aage badha dijiye?",
    "forward kar dijiye？", "forward कर दीजिए क्या?",
    "हाँ, पैसे बढ़ा दीजिए।", "हाँ, payment हो गया।", "हाँ, visit हुई थी।", "हाँ problem अभी भी है।",
    "मैंने request आगे बढ़ा दी।", "हाँ, request आगे बढ़ा दी थी।", "badha diya", "haan payment paid",
    "आगे forward मत कर दीजिए।", "नहीं, forward कर दीजिए।", "forward kar do kya",
    "क्या request forward कर दीजिए?", "haan forward kar do?", "forward nahi karna",
    "request forward कर दी है।", "मैंने request forward कर दी।", "haan forward kar diya",
    "haan payment forward kar dijiye", "forward kar dijiye lekin service nahi chahiye",
])
def test_negation_question_and_factual_affirmation_never_become_forward_consent(hub, text):
    result = asyncio.run(replay(apply_followup_consent_repair(fixture()), hub, text, "affirm"))
    assert result["slots"].get("outcome") != "redispatch"
    assert result["slots"].get(f"haier_followup_{hub}_route") != "consent_command"


@pytest.mark.parametrize("hub", HUBS)
@pytest.mark.parametrize(("text", "signal"), [
    ("stop calling", "affirm"), ("हाँ बढ़ा दीजिए, stop calling", "affirm"),
    ("service nahi chahiye", "affirm"), ("no", "refusal"),
    ("हाँ, बढ़ा दीजिए लेकिन service nahi chahiye", "affirm"),
    ("already paid", "already_paid"), ("हाँ बढ़ा दीजिए, already paid", "already_paid"),
    ("warranty dispute", "affirm"), ("हाँ बढ़ा दीजिए, warranty dispute", "affirm"),
    ("next week", "affirm"), ("हाँ बढ़ा दीजिए next week", "affirm"),
    ("हाँ", "affirm"), ("हाँ technician भेज दीजिए", "affirm"),
])
def test_existing_protected_and_reviewed_consent_routes_are_preserved(hub, text, signal):
    original = fixture()
    before = asyncio.run(replay(original, hub, text, signal))
    after = asyncio.run(replay(apply_followup_consent_repair(original), hub, text, signal))
    assert after["trace"] == before["trace"]
    assert after["awaiting"] == before["awaiting"]
    assert after["slots"] == before["slots"]


def test_unclear_reply_has_distinct_clarification_and_neutral_unverified_closing():
    definition = apply_followup_consent_repair(fixture())
    first = asyncio.run(replay(definition, HUBS[0], "हम्म", None))
    assert first["awaiting"] == HUBS[1]
    original_question = next(node for node in definition["nodes"] if node["id"] == HUBS[0])["config"]["textByLanguage"]["hi"]
    assert first["reply"] == "क्या technician की visit वाली request आगे forward कर दूँ?"
    assert first["reply"] != original_question
    final = asyncio.run(replay(definition, HUBS[1], "हम्म", None))
    assert final["slots"]["outcome"] == "unverified_no_action"
    assert final["trace"][-1] == "n_v2_end_unverified"
    assert "customer care" not in final["reply"]
    assert "जानकारी पूरी तरह confirm नहीं हो पाई" in final["reply"]
    assert final["slots"].get("service_required") is None


def test_clarification_preserves_day_preference_without_appointment_claim():
    definition = apply_followup_consent_repair(fixture())
    result = asyncio.run(replay(definition, HUBS[0], "हम्म", None, {"preferred_service_day": "Wednesday"}))
    assert result["awaiting"] == HUBS[1]
    assert "Wednesday की आपकी preference नोट कर ली है" in result["reply"]
    assert result["reply"].endswith("क्या technician की visit वाली request आगे forward कर दूँ?")
    assert result["slots"].get("outcome") is None


@pytest.mark.parametrize("hub", HUBS)
def test_previous_command_capture_cannot_turn_an_unclear_new_turn_into_consent(hub):
    definition = apply_followup_consent_repair(fixture())
    result = asyncio.run(replay(definition, hub, "हम्म", None,
                               {f"haier_followup_{hub}_route": "consent_command"}))
    assert result["slots"].get("outcome") != "redispatch"
    assert result["slots"].get(f"haier_followup_{hub}_route") != "consent_command"


@pytest.mark.parametrize("hub", HUBS)
@pytest.mark.parametrize("signal", [None, "affirm"])
def test_elided_permission_does_not_reuse_a_stale_command_for_hmm(hub, signal):
    result = asyncio.run(replay(apply_followup_consent_repair(version_three_fixture()), hub, "हम्म।", signal,
                               {f"haier_followup_{hub}_route": "consent_command"}))
    assert result["slots"].get("outcome") != "redispatch"
    assert result["slots"].get(f"haier_followup_{hub}_route") != "consent_command"


def test_transform_is_pure_idempotent_and_only_changes_reviewed_node_configs():
    original = fixture()
    backup = deepcopy(original)
    repaired = apply_followup_consent_repair(original)
    assert original == backup
    assert repaired["edges"] == original["edges"]
    assert len(repaired["nodes"]) == len(original["nodes"])
    changed = {before["id"] for before, after in zip(original["nodes"], repaired["nodes"]) if before != after}
    assert changed == {*HUBS, "n_identity_gate", "n_v2_end_unverified"}
    assert apply_followup_consent_repair(repaired) == repaired
    assert apply_followup_consent_repair(json.loads(json.dumps(repaired, sort_keys=True))) == repaired
    assert next(node for node in repaired["nodes"] if node["id"] == "n_identity_gate")["config"][MARKER] == VERSION


@pytest.mark.parametrize("hub", HUBS)
@pytest.mark.parametrize("text", [
    "नहीं, service नहीं चाहिए।", "service नहीं चाहिए॥", "सर्विस नहीं चाहिए।",
    "जी, service नहीं चाहिए।", "हाँ, service नहीं चाहिए।", "service nahi chahiye।",
    "service नहीं चाहिए!", "service नहीं चाहिए", "service नहीं चाहिए।\n",
    "हाँ, बढ़ा दीजिए, service नहीं चाहिए।",
])
def test_complete_service_refusal_with_hindi_punctuation_cancels_at_permission(hub, text):
    repaired = apply_followup_consent_repair(fixture())
    for definition in (repaired, json.loads(json.dumps(repaired, sort_keys=True))):
        result = asyncio.run(replay(definition, hub, text, "affirm"))
        assert result["slots"]["outcome"] == "cancel_no_service"
        assert result["slots"][f"haier_audit_{hub}_route"] == "no_service"
        assert result["slots"].get(f"haier_followup_{hub}_route") != "consent_command"


@pytest.mark.parametrize("hub", HUBS)
@pytest.mark.parametrize("text", [
    "service नहीं चाहिए क्या?", "क्या service नहीं चाहिए?", "service नहीं चाहिए क्यों?",
    "service नहीं चाहिएऔर", "service नहीं चाहिएगा।",
])
def test_danda_fix_does_not_turn_questions_or_partial_words_into_refusal(hub, text):
    result = asyncio.run(replay(apply_followup_consent_repair(fixture()), hub, text, None))
    assert result["slots"].get("outcome") != "cancel_no_service"
    assert result["slots"].get(f"haier_audit_{hub}_route") != "no_service"


def test_version_one_upgrade_preserves_refusal_fix_and_adds_forward_permission_wording():
    original = version_two_fixture()
    nodes = {node["id"]: node for node in original["nodes"]}
    nodes["n_identity_gate"]["config"][MARKER] = 1
    for hub in HUBS:
        for capture in nodes[hub]["config"]["alsoCapture"]:
            entity = capture.get("entity", {}).get("synonymPatterns", {})
            if "no_service" in entity:
                entity["no_service"] = [p.replace(r"[\w\u0900-\u0963\u0966-\u097f]", r"[\w\u0900-\u097f]")
                                        for p in entity["no_service"]]
    backup = deepcopy(original)
    repaired = apply_followup_consent_repair(original)
    assert original == backup
    assert repaired["edges"] == original["edges"]
    changed = {before["id"] for before, after in zip(original["nodes"], repaired["nodes"]) if before != after}
    assert changed == {*HUBS, "n_identity_gate"}
    for before, after in zip(original["nodes"], repaired["nodes"]):
        assert len(before.get("config", {}).get("alsoCapture", [])) == len(after.get("config", {}).get("alsoCapture", []))
    assert apply_followup_consent_repair(repaired) == repaired


def version_three_fixture():
    """Restore v3's matcher so the upgrade test reproduces the call failure."""
    original = apply_followup_consent_repair(fixture())
    nodes = {node["id"]: node for node in original["nodes"]}
    nodes["n_identity_gate"]["config"][MARKER] = 3
    for hub in HUBS:
        for capture in nodes[hub]["config"]["alsoCapture"]:
            patterns = capture.get("entity", {}).get("synonymPatterns", {})
            if "consent_command" in patterns:
                patterns["consent_command"] = [pattern for pattern in patterns["consent_command"]
                                                if "kiijiye" not in pattern]
    return original


def test_actual_call_cv_c485f6c330a8_command_is_accepted_instead_of_repeated_then_unverified():
    original = version_three_fixture()
    text = "हाँ कर दीजिए।"
    first = asyncio.run(replay(original, HUBS[0], text, "affirm"))
    assert first["awaiting"] == HUBS[1]
    assert first["reply"] == "क्या technician की visit वाली request आगे forward कर दूँ?"
    repeated = asyncio.run(replay(original, HUBS[1], text, "affirm", first["slots"]))
    assert repeated["slots"]["outcome"] == "unverified_no_action"

    repaired = apply_followup_consent_repair(original)
    accepted = asyncio.run(replay(repaired, HUBS[0], text, "affirm"))
    assert accepted["slots"]["outcome"] == "redispatch"
    assert HUBS[1] not in accepted["trace"]
    assert "m_n_v2_end_unverified" not in accepted["trace"]


def test_version_three_upgrade_only_adds_scoped_captures_and_marker_preserving_all_speech_and_routes():
    original = version_three_fixture()
    backup = deepcopy(original)
    repaired = apply_followup_consent_repair(original)
    assert original == backup
    assert repaired["edges"] == original["edges"]
    assert len(repaired["nodes"]) == len(original["nodes"])
    changed = {before["id"] for before, after in zip(original["nodes"], repaired["nodes"]) if before != after}
    assert changed == {*HUBS, "n_identity_gate"}
    for before, after in zip(original["nodes"], repaired["nodes"]):
        restored = deepcopy(after)
        if before["id"] in HUBS:
            old_captures = before["config"]["alsoCapture"]
            new_captures = after["config"]["alsoCapture"]
            assert len(old_captures) == len(new_captures)
            changed_captures = [(old, new) for old, new in zip(old_captures, new_captures) if old != new]
            assert len(changed_captures) == 1
            old_patterns = changed_captures[0][0]["entity"]["synonymPatterns"]["consent_command"]
            new_patterns = changed_captures[0][1]["entity"]["synonymPatterns"]["consent_command"]
            assert len(new_patterns) == len(old_patterns) + 1
            assert new_patterns[:-1] == old_patterns
            restored["config"]["alsoCapture"] = deepcopy(old_captures)
        if before["id"] == "n_identity_gate":
            assert after["config"][MARKER] == VERSION == 4
            restored["config"][MARKER] = 3
        assert restored == before
    assert apply_followup_consent_repair(repaired) == repaired
    assert apply_followup_consent_repair(json.loads(json.dumps(repaired, sort_keys=True))) == repaired


def version_two_fixture():
    """Recreate the prior speech/capture contract independently of v3 text."""
    original = version_three_fixture()
    nodes = {node["id"]: node for node in original["nodes"]}
    nodes["n_identity_gate"]["config"][MARKER] = 2
    for hub in HUBS:
        config = nodes[hub]["config"]
        for capture in config["alsoCapture"]:
            patterns = capture.get("entity", {}).get("synonymPatterns", {})
            if "consent_command" in patterns:
                patterns["consent_command"] = [pattern for pattern in patterns["consent_command"]
                                                if "(?:forward|forword|" not in pattern]
        # The prior revision has the same localizable field contracts.
        for field in ("prompt", "textByLanguage", "readback"):
            config[field] = json.loads(json.dumps(config[field], ensure_ascii=False).replace(
                "आगे forward कर दूँ", "आगे बढ़ा दूँ").replace(
                    "May I forward the technician visit request?", "May I go ahead with the technician visit request?"))
        config["resumePrompt"] = config["prompt"]
        config["resumePromptByLanguage"] = deepcopy(config["textByLanguage"])
    return original


def test_version_two_upgrade_updates_every_authored_speech_variant_only_at_permission_hubs():
    original = version_two_fixture()
    backup = deepcopy(original)
    repaired = apply_followup_consent_repair(original)
    assert original == backup
    assert repaired["edges"] == original["edges"]
    changed = {before["id"] for before, after in zip(original["nodes"], repaired["nodes"]) if before != after}
    assert changed == {*HUBS, "n_identity_gate"}
    for node in repaired["nodes"]:
        if node["id"] not in HUBS:
            continue
        config = node["config"]
        assert "आगे बढ़ा दूँ" not in json.dumps(config, ensure_ascii=False)
        assert "May I go ahead" not in json.dumps(config, ensure_ascii=False)
        expected = ("ठीक है, क्या technician की visit के लिए मैं आपकी request आगे forward कर दूँ?"
                    if node["id"] == HUBS[0] else "क्या technician की visit वाली request आगे forward कर दूँ?")
        assert config["textByLanguage"]["hi"] == expected
        assert config["resumePromptByLanguage"]["hi"] == expected
        for group in config["readback"]["hi"]["groups"]:
            assert "आगे forward कर दूँ?" in group["template"]
    assert apply_followup_consent_repair(repaired) == repaired


@pytest.mark.parametrize("day", [None, "Wednesday"])
def test_version_two_upgrade_speaks_forward_permission_with_optional_date_preference(day):
    definition = apply_followup_consent_repair(version_two_fixture())
    slots = {"preferred_service_day": day} if day else {}
    primary = asyncio.run(replay(definition, "n_service_need", "हाँ", "affirm", slots))
    assert primary["awaiting"] == HUBS[0]
    assert primary["reply"].endswith("क्या technician की visit के लिए मैं आपकी request आगे forward कर दूँ?")
    clarify = asyncio.run(replay(definition, HUBS[0], "हम्म", None, slots))
    assert clarify["awaiting"] == HUBS[1]
    assert clarify["reply"].endswith("क्या technician की visit वाली request आगे forward कर दूँ?")
    assert ("Wednesday" in primary["reply"]) == bool(day)
    assert ("Wednesday" in clarify["reply"]) == bool(day)


def test_version_two_upgrade_keeps_prior_hindi_refusal_boundary_repair():
    original = version_two_fixture()
    repaired = apply_followup_consent_repair(original)
    for before, after in zip(original["nodes"], repaired["nodes"]):
        if before["id"] not in HUBS:
            continue
        before_patterns = [capture["entity"]["synonymPatterns"]["no_service"]
                           for capture in before["config"]["alsoCapture"]
                           if "no_service" in capture.get("entity", {}).get("synonymPatterns", {})]
        after_patterns = [capture["entity"]["synonymPatterns"]["no_service"]
                          for capture in after["config"]["alsoCapture"]
                          if "no_service" in capture.get("entity", {}).get("synonymPatterns", {})]
        assert before_patterns == after_patterns


@pytest.mark.parametrize("mutation", ["wrong_bot", "missing_prior_repair", "missing_consent_route", "changed_outcome"])
def test_transform_rejects_unreviewed_contracts(mutation):
    definition = fixture()
    nodes = {node["id"]: node for node in definition["nodes"]}
    if mutation == "wrong_bot":
        definition["bot_id"] = "another_bot"
    elif mutation == "missing_prior_repair":
        nodes["n_identity_gate"]["config"].pop("haierFollowupRepairVersion")
    elif mutation == "missing_consent_route":
        nodes[HUBS[0]]["config"]["correctionCaptureVariable"] = "unreviewed"
    else:
        nodes["m_n_v2_end_unverified"]["config"]["setSlots"]["outcome"] = "redispatch"
    with pytest.raises(ValueError):
        apply_followup_consent_repair(definition)
