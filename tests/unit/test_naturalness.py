"""SpeechNaturalnessPlanner — config resolution, contextual/probabilistic
filler planning, gender agreement, critical-content safety, per-sentence
delivery, backchannels and self-correction. Pure unit level (seeded RNG)."""

import random

import pytest

from shared.orchestration.naturalness import (
    ladder_cue,
    HUMAN_SPEECH_DEFAULTS,
    _LEADING_ACK_RE,
    _POOLS,
    SpeechNaturalnessPlanner,
    base_language,
    contains_critical_content,
    normalize_spoken_variant,
    resolve_human_speech,
    resolve_human_speech_with_sources,
    validate_human_speech,
)
from shared.orchestration.voice_identity import VoiceIdentity

MALE = VoiceIdentity(name="Mithun", gender="male")
FEMALE = VoiceIdentity(name="Ritu", gender="female")
NEUTRAL = VoiceIdentity(name="", gender="neutral")


def planner(overrides=None, seed=7):
    return SpeechNaturalnessPlanner(overrides or {}, rng=random.Random(seed))


# ── config resolution ────────────────────────────────────────────────────


class TestConfigResolution:
    def test_defaults_apply_without_layers(self):
        assert resolve_human_speech() == HUMAN_SPEECH_DEFAULTS

    def test_later_layers_win_per_key(self):
        merged = resolve_human_speech(
            {"backchannel_probability": 0.1, "enabled": False},
            {"backchannel_probability": 0.9},
        )
        assert merged["backchannel_probability"] == 0.9
        assert merged["enabled"] is False  # untouched by the bot layer

    def test_junk_values_and_unknown_keys_are_dropped(self):
        merged = resolve_human_speech(
            {"enabled": "yes", "thinking_filler_probability": 9,
             "min_gap_between_backchannels_ms": -5, "bogus": True},
        )
        assert merged["enabled"] is True
        assert merged["thinking_filler_probability"] == 1.0  # clamped
        assert merged["min_gap_between_backchannels_ms"] == 2000  # clamped low
        assert "bogus" not in merged

    def test_validate_rejects_what_resolution_would_mangle(self):
        problems = validate_human_speech({
            "enabled": "yes",
            "thinking_filler_probability": 2,
            "min_gap_between_backchannels_ms": 100,
            "nope": 1,
        })
        assert len(problems) == 4

    def test_validate_accepts_a_sparse_override(self):
        assert validate_human_speech({"backchannels": False}) == []

    def test_effective_sources_follow_platform_tenant_bot_precedence(self):
        effective, sources = resolve_human_speech_with_sources(
            {"backchannel_probability": 0.2, "micro_pauses": False},
            {"backchannel_probability": 0.1},
        )
        assert effective["backchannel_probability"] == 0.1
        assert sources["backchannel_probability"] == "bot"
        assert effective["micro_pauses"] is False
        assert sources["micro_pauses"] == "tenant"
        assert sources["enabled"] == "platform"

    def test_legacy_boolean_cannot_override_a_probability_or_its_source(self):
        effective, sources = resolve_human_speech_with_sources(
            {"backchannel_probability": True}
        )
        assert effective["backchannel_probability"] == HUMAN_SPEECH_DEFAULTS[
            "backchannel_probability"
        ]
        assert sources["backchannel_probability"] == "platform"


# ── critical content ─────────────────────────────────────────────────────


class TestCriticalContent:
    @pytest.mark.parametrize("text", [
        "Aapka balance ₹5000 pending hai",
        "Amount Rs. 2,500 due hai",
        "OTP hai 4321",
        "Payment 25 tareekh ko karna hai",
        "Your account number ends in 8842",
        "Transaction reference TXN123 verify karte hain",
        "The due date is 12/08",
        "The due date is 12.08.2026",
        "Payment on August 15 confirm karein",
        "यह कॉल रिकॉर्ड की जा रही है",
        "आपका minimum payable पच्चीस हज़ार रुपये है।",
        "You need to pay two thousand rupees today",
        "Please call me on +91 98765 43210",
        "My address is 12 Lake Road, Sector 4",
        "I will pay by next Monday",
        "The repayment commitment is for Friday",
        "I promise to pay tomorrow",
        "Kya aap aaj payment kar sakte hain?",
        "Your identity verification is required",
        "By continuing you consent to these terms and conditions",
        "The due date is twenty fifth of August",
    ])
    def test_detects_critical(self, text):
        assert contains_critical_content(text)

    @pytest.mark.parametrize("text", [
        "Achha, theek hai, main dekh raha hoon",
        "",
    ])
    def test_ignores_ordinary_speech(self, text):
        assert not contains_critical_content(text)


# ── turn planning ────────────────────────────────────────────────────────


class TestPlanTurn:
    def test_disabled_planner_never_decorates(self):
        p = planner({"enabled": False})
        for i in range(1, 20):
            plan = p.plan_turn(language="hi-IN", identity=MALE,
                               signal="already_paid", route_kind="tool",
                               turn_index=i)
            assert not plan.has_preface

    def test_greeting_turn_is_never_decorated(self):
        p = planner({"tool_ack_probability": 1.0})
        plan = p.plan_turn(language="hi-IN", identity=MALE,
                           route_kind="tool", turn_index=0)
        assert not plan.has_preface

    def test_tool_route_gets_checking_ack(self):
        p = planner({"tool_ack_probability": 1.0})
        plan = p.plan_turn(language="hi-IN", identity=MALE,
                           signal="already_paid", route_kind="tool",
                           turn_index=2)
        assert plan.preface_kind == "checking"
        assert plan.preface

    def test_unsupported_language_gets_no_filler(self):
        p = planner({"tool_ack_probability": 1.0})
        plan = p.plan_turn(language="bn-IN", identity=MALE,
                           route_kind="tool", turn_index=2)
        assert not plan.has_preface

    def test_serious_signal_never_gets_playful_hesitation(self):
        p = planner({"acknowledgement_probability": 1.0,
                     "thinking_filler_probability": 1.0})
        for signal in ("complaint", "hardship", "refusal", "wrong_person"):
            plan = p.plan_turn(language="hi-IN", identity=MALE,
                               signal=signal, route_kind="llm", turn_index=3)
            assert plan.preface_kind in ("", "empathy")
            assert "hmm" not in plan.preface.lower() or plan.preface_kind == "empathy"

    def test_gender_agreement_male_vs_female(self):
        for gender, identity, needle, forbidden in (
            ("male", MALE, "karta", "karti"),
            ("female", FEMALE, "karti", "karta"),
        ):
            p = planner({"tool_ack_probability": 1.0}, seed=3)
            texts = set()
            for i in range(1, 30):
                plan = p.plan_turn(language="hi-IN", identity=identity,
                                   route_kind="tool", turn_index=i)
                if plan.preface:
                    texts.add(plan.preface)
            joined = " ".join(texts).lower()
            assert forbidden not in joined, (gender, texts)
            assert needle in joined or "dekh" in joined, (gender, texts)

    def test_neutral_gender_skips_gendered_variants(self):
        p = planner({"tool_ack_probability": 1.0}, seed=11)
        for i in range(1, 30):
            plan = p.plan_turn(language="hi-IN", identity=NEUTRAL,
                               route_kind="tool", turn_index=i)
            low = plan.preface.lower()
            for form in ("karta", "karti", "raha", "rahi", "sakta", "sakti",
                         "dekhta", "dekhti"):
                assert form not in low, plan.preface

    def test_variant_pool_never_repeats_immediately(self):
        p = planner({"tool_ack_probability": 1.0}, seed=5)
        last = None
        for i in range(1, 25):
            plan = p.plan_turn(language="hi-IN", identity=MALE,
                               route_kind="tool", turn_index=i)
            assert plan.preface != last
            last = plan.preface

    def test_cross_pool_normalized_repetition_is_prevented(self):
        p = planner(seed=1)
        spoken = [
            p._pick("hi", "thinking", MALE),
            p._pick("hi", "acknowledgement", MALE),
            p._pick("hi", "backchannel", MALE),
        ]
        normalized = [normalize_spoken_variant(item) for item in spoken]
        assert normalized[1] != normalized[0]
        assert normalized[2] not in normalized[:2]

    def test_spoken_variant_normalization_folds_case_space_and_ellipsis(self):
        assert normalize_spoken_variant("  ACHHA  ... ") == normalize_spoken_variant(
            "achha…"
        )

    def test_single_safe_variant_still_works(self):
        p = planner(seed=3)
        first = p._pick("gu", "critical_checking", NEUTRAL)
        second = p._pick("gu", "critical_checking", NEUTRAL)
        assert first and second == first

    @pytest.mark.parametrize(
        ("locale", "base"),
        [
            ("en-IN", "en"), ("hi-IN", "hi"), ("gu-IN", "gu"),
            ("ml-IN", "ml"), ("mr-IN", "mr"), ("pa-IN", "pa"),
            ("ta-IN", "ta"), ("te-IN", "te"), ("ur-IN", "ur"),
        ],
    )
    def test_every_enabled_language_selects_only_its_native_pool(self, locale, base):
        p = planner({"tool_ack_probability": 1.0}, seed=9)
        plan = p.plan_turn(
            language=locale, identity=MALE, route_kind="tool", turn_index=2
        )
        expected = {
            normalize_spoken_variant(p._adapted(item, MALE))
            for item in _POOLS[base]["checking"]
        }
        assert normalize_spoken_variant(plan.preface) in expected

    def test_unknown_locale_never_borrows_another_language_pool(self):
        p = planner({"tool_ack_probability": 1.0}, seed=9)
        plan = p.plan_turn(
            language="bn-IN", identity=MALE, route_kind="tool", turn_index=2
        )
        assert plan.preface == ""
        assert plan.telemetry["suppression_reason"] == "no_pool_language:bn"

    def test_structured_critical_turn_suppresses_preface_and_correction(self):
        p = planner({
            "tool_ack_probability": 1.0,
            "thinking_filler_probability": 1.0,
            "self_correction": True,
            "self_correction_probability": 1.0,
        })
        plan = p.plan_turn(
            language="hi-IN", identity=FEMALE, route_kind="direct",
            turn_index=3, critical=True, critical_reason="repayment_commitment",
        )
        assert not plan.preface
        assert plan.allow_self_correction is False
        assert plan.telemetry["critical_content"] is True
        assert plan.telemetry["suppression_reason"] == "critical:repayment_commitment"

    def test_generic_tool_lookup_allows_only_safe_verification_preface(self):
        p = planner({"tool_ack_probability": 1.0})
        plan = p.plan_turn(
            language="en-IN", identity=NEUTRAL, route_kind="tool",
            turn_index=2, critical=True, critical_reason="tool_result",
            allow_safe_tool_preface=True,
        )
        assert plan.preface in _POOLS["en"]["critical_checking"]
        assert plan.preface_kind == "checking"
        assert plan.allow_self_correction is False
        assert plan.telemetry["acknowledgement_used"] is True

    def test_probability_zero_means_never(self):
        p = planner({
            "thinking_filler_probability": 0.0,
            "acknowledgement_probability": 0.0,
            "tool_ack_probability": 0.0,
        })
        for i in range(1, 30):
            plan = p.plan_turn(language="hi-IN", identity=MALE,
                               signal="affirm", route_kind="llm", turn_index=i)
            assert not plan.has_preface

    def test_non_tool_routes_never_get_a_preface_glued_to_the_reply(self):
        p = planner({"acknowledgement_probability": 1.0,
                     "thinking_filler_probability": 1.0}, seed=13)
        for route in ("llm", "kb", "direct"):
            for i in range(1, 30):
                plan = p.plan_turn(language="hi-IN", identity=MALE,
                                   signal="affirm", route_kind=route, turn_index=i)
                assert not plan.has_preface
                assert plan.telemetry["suppression_reason"] == "dispatch_ack_path"

    def test_tool_preface_never_stacks_on_a_dispatch_acknowledgement(self):
        p = planner({"tool_ack_probability": 1.0}, seed=13)
        for i in range(1, 60):
            plan = p.plan_turn(language="hi-IN", identity=MALE, route_kind="tool",
                               turn_index=i, early_ack_spoken=True)
            assert plan.preface and not _LEADING_ACK_RE.match(plan.preface), plan.preface
            assert plan.telemetry["early_ack"] is True
        assert _LEADING_ACK_RE.match("जी... एक मिनट दीजिए")
        assert _LEADING_ACK_RE.match("Achha... ek minute, main check karta hoon.")
        assert not _LEADING_ACK_RE.match("Ek minute, main check karta hoon...")

    def test_telemetry_shape(self):
        p = planner({"tool_ack_probability": 1.0})
        plan = p.plan_turn(language="hi-IN", identity=FEMALE,
                           signal="already_paid", route_kind="tool",
                           turn_index=2)
        assert plan.telemetry["filler_used"] is True
        assert plan.telemetry["filler_type"] == "checking"
        assert plan.telemetry["language"] == "hi"
        assert plan.telemetry["gender_mode"] == "female"


# ── per-sentence delivery ────────────────────────────────────────────────


class TestPlanSegment:
    def test_critical_segment_gets_clear_pacing(self):
        p = planner()
        seg = p.plan_segment("Aapka balance ₹5000 hai.",
                             base_pause_ms=150, language="hi-IN")
        assert seg.critical is True
        assert seg.speed_scale is not None and seg.speed_scale <= 1.0
        assert seg.pause_after_ms == 270  # base + 120, clear boundary
        assert seg.speech_style == "serious"
        assert seg.emphasis == "moderate"

    def test_structured_turn_criticality_is_not_regex_dependent(self):
        p = planner()
        p.set_turn_criticality(True, "tool_result")
        seg = p.plan_segment(
            "The lookup completed successfully.",
            base_pause_ms=150,
            language="en-IN",
        )
        assert seg.critical is True
        assert seg.critical_reason == "tool_result"
        assert seg.speed_scale is not None and seg.speed_scale <= 1.0

    def test_question_is_slightly_slower(self):
        p = planner()
        seg = p.plan_segment("Kya aap aaj payment kar sakte hain?",
                             base_pause_ms=150, language="hi-IN")
        assert seg.speed_scale is not None and seg.speed_scale < 1.0

    def test_jitter_stays_subtle_and_bounded(self):
        p = planner(seed=23)
        for _ in range(200):
            seg = p.plan_segment("Main aapki madad ke liye yahan hoon theek hai",
                                 base_pause_ms=150, language="hi-IN")
            if seg.speed_scale is not None:
                assert 0.9 <= seg.speed_scale <= 1.1
            if seg.pause_after_ms is not None:
                assert 80 <= seg.pause_after_ms <= 700

    def test_disabled_flags_disable_dimensions(self):
        p = planner({"prosody_variation": False, "micro_pauses": False})
        seg = p.plan_segment("Kya aap payment kar sakte hain?",
                             base_pause_ms=150, language="hi-IN")
        assert seg.speed_scale is None
        assert seg.pause_after_ms is None

    def test_zero_base_pause_never_invents_gaps(self):
        p = planner()
        for _ in range(50):
            seg = p.plan_segment("Achha theek hai bilkul.",
                                 base_pause_ms=0, language="hi-IN")
            assert seg.pause_after_ms is None


# ── backchannels ─────────────────────────────────────────────────────────


class TestBackchannels:
    def test_disabled_returns_nothing(self):
        p = planner({"backchannels": False, "backchannel_probability": 1.0})
        assert p.plan_backchannel(language="hi-IN", identity=MALE, now=10.0) == ""

    def test_min_gap_between_backchannels(self):
        p = planner({"backchannel_probability": 1.0,
                     "min_gap_between_backchannels_ms": 8000})
        first = p.plan_backchannel(language="hi-IN", identity=MALE, now=100.0)
        assert first
        assert p.plan_backchannel(language="hi-IN", identity=MALE, now=104.0) == ""
        assert p.plan_backchannel(language="hi-IN", identity=MALE, now=109.0) != ""

    def test_max_per_call(self):
        p = planner({"backchannel_probability": 1.0,
                     "min_gap_between_backchannels_ms": 2000,
                     "max_backchannels_per_call": 2})
        played = [
            p.plan_backchannel(language="hi-IN", identity=MALE, now=t)
            for t in (10.0, 20.0, 30.0, 40.0)
        ]
        assert sum(1 for token in played if token) == 2

    def test_failed_roll_consumes_the_window(self):
        p = planner({"backchannel_probability": 0.0})
        assert p.plan_backchannel(language="hi-IN", identity=MALE, now=50.0) == ""
        # The failed roll must not be immediately re-rolled every monitor tick.
        assert p._last_backchannel_monotonic == 50.0

    def test_unknown_language_has_no_backchannels(self):
        p = planner({"backchannel_probability": 1.0})
        assert p.plan_backchannel(language="bn-IN", identity=MALE, now=5.0) == ""

    @pytest.mark.parametrize(
        "signal", [
            "complaint", "hardship", "refusal", "wrong_person",
            "agent_request", "distress", "frustration",
        ]
    )
    def test_serious_context_suppresses_backchannel(self, signal):
        p = planner({"backchannel_probability": 1.0})
        assert p.plan_backchannel(
            language="hi-IN", identity=MALE, caller_state=signal, now=10.0
        ) == ""
        assert p.last_backchannel_suppression_reason == f"serious_context:{signal}"

    def test_normal_context_still_allows_backchannel(self):
        p = planner({"backchannel_probability": 1.0})
        assert p.plan_backchannel(
            language="hi-IN", identity=MALE, caller_state="question", now=10.0
        )


# ── self-correction ──────────────────────────────────────────────────────


class TestSelfCorrection:
    def test_off_by_default(self):
        p = planner({"self_correction_probability": 1.0})
        text = "Aapka payment status abhi pending dikh raha hai bilkul"
        assert p.maybe_self_correct(text, language="hi-IN") == text

    def test_applies_when_enabled(self):
        p = planner({"self_correction": True, "self_correction_probability": 1.0})
        text = "Aapka payment status abhi pending dikh raha hai bilkul"
        corrected = p.maybe_self_correct(text, language="hi-IN")
        assert corrected != text
        assert "..." in corrected
        assert text.split()[1] in corrected

    def test_never_touches_critical_content(self):
        p = planner({"self_correction": True, "self_correction_probability": 1.0})
        text = "Aapka payment ₹5000 due hai on 25 tareekh ko"
        assert p.maybe_self_correct(text, language="hi-IN") == text


def test_base_language_mapping():
    assert base_language("hi-IN") == "hi"
    assert base_language("hinglish") == "hi"
    assert base_language("en-IN") == "en"
    assert base_language("") == ""


# ── latency fillers + first-reply boost ──────────────────────────────────


class TestLatencyFillerConfig:
    def test_defaults_and_bounds(self):
        assert HUMAN_SPEECH_DEFAULTS["latency_fillers"] is True
        assert HUMAN_SPEECH_DEFAULTS["latency_filler_delay_ms"] == 1500
        assert validate_human_speech({"latency_fillers": True, "latency_filler_delay_ms": 2000}) == []
        assert validate_human_speech({"latency_filler_delay_ms": 300}) == [
            "'latency_filler_delay_ms' must be between 500 and 5000",
        ]
        assert validate_human_speech({"latency_filler_delay_ms": 1500.5}) == [
            "'latency_filler_delay_ms' must be an integer",
        ]
        assert HUMAN_SPEECH_DEFAULTS["latency_filler_ladder"] is True
        assert HUMAN_SPEECH_DEFAULTS["latency_filler_hmm_ms"] == 3500
        assert HUMAN_SPEECH_DEFAULTS["latency_filler_spoken_ms"] == 5000
        assert validate_human_speech({"latency_filler_hmm_ms": 1000}) == [
            "'latency_filler_hmm_ms' must be between 2000 and 8000",
        ]
        assert validate_human_speech({"latency_filler_spoken_ms": 20000}) == [
            "'latency_filler_spoken_ms' must be between 3000 and 12000",
        ]
        assert validate_human_speech({"latency_filler_ladder": 1}) == [
            "'latency_filler_ladder' must be a boolean",
        ]
        assert validate_human_speech({"latency_fillers": "on"}) == [
            "'latency_fillers' must be a boolean",
        ]
        # Runtime merging clamps rather than failing a live call.
        assert resolve_human_speech({"latency_filler_delay_ms": 9000})["latency_filler_delay_ms"] == 5000
        assert resolve_human_speech({"latency_filler_delay_ms": 100})["latency_filler_delay_ms"] == 500

    def test_planner_exposes_the_switch_under_the_master_switch(self):
        assert planner().latency_fillers_enabled is True
        assert planner().latency_filler_delay_ms == 1500
        assert planner({"latency_fillers": False}).latency_fillers_enabled is False
        assert planner({"enabled": False}).latency_fillers_enabled is False
        assert planner({"latency_filler_delay_ms": 2200}).latency_filler_delay_ms == 2200
        assert planner().latency_filler_ladder_enabled is True
        assert planner({"latency_fillers": False}).latency_filler_ladder_enabled is False
        assert planner({"latency_filler_ladder": False}).latency_filler_ladder_enabled is False
        assert planner({"latency_filler_hmm_ms": 4000}).latency_filler_hmm_ms == 4000
        assert planner({"latency_filler_spoken_ms": 6000}).latency_filler_spoken_ms == 6000
        assert ladder_cue("hi-IN", "hmm") == "हम्म…" and ladder_cue("en-US", "wait") == "One second…"
        assert ladder_cue("fr-FR", "hmm") == "" and ladder_cue("hi-IN", "sigh") == ""

    def test_sources_follow_precedence_for_the_new_keys(self):
        effective, sources = resolve_human_speech_with_sources(
            {"latency_fillers": False}, {"latency_filler_delay_ms": 2500},
        )
        assert effective["latency_fillers"] is False
        assert effective["latency_filler_delay_ms"] == 2500
        assert sources["latency_fillers"] == "tenant"
        assert sources["latency_filler_delay_ms"] == "bot"


class TestEarlyAck:
    """Dispatch-time acknowledgement: what the caller hears ~1 s after they
    stop, chosen from what they said, never glued to the reply."""

    @staticmethod
    def _ack_rate(turn_index, seeds=300, **overrides):
        hits = 0
        for seed in range(seeds):
            p = planner({"acknowledgement_probability": 0.5, **overrides}, seed=seed)
            hits += bool(p.plan_early_ack(language="hi-IN", identity=MALE,
                                          context="answer", turn_index=turn_index))
        return hits / seeds

    def test_first_reply_after_the_greeting_gets_better_odds(self):
        first, later = self._ack_rate(1), self._ack_rate(5)
        assert 0.65 <= first <= 0.85      # 0.5 × 1.5
        assert 0.4 <= later <= 0.6        # 0.5 unchanged
        assert first > later

    def test_never_on_two_consecutive_turns(self):
        p = planner({"acknowledgement_probability": 1.0})
        spoken = [
            bool(p.plan_early_ack(language="hi-IN", identity=MALE,
                                  context="answer", turn_index=i))
            for i in range(1, 11)
        ]
        assert spoken == [True, False] * 5
        assert p.plan_early_ack(language="hi-IN", identity=MALE, context="answer", turn_index=11)
        assert p.plan_early_ack(language="hi-IN", identity=MALE, context="answer", turn_index=12) == ""
        assert p.last_early_ack_reason == "anti_repetition"

    def test_context_selects_the_pool(self):
        p = planner({"acknowledgement_probability": 1.0}, seed=2)
        expected = {
            "answer": "ack_answer", "question": "ack_question",
            "lookup": "ack_lookup", "neutral": "ack_neutral",
        }
        turn = 1
        for context, pool in expected.items():
            token = p.plan_early_ack(language="hi-IN", identity=MALE,
                                     context=context, turn_index=turn)
            pool_norm = {normalize_spoken_variant(e) for e in _POOLS["hi"][pool]}
            assert normalize_spoken_variant(token) in pool_norm, (context, token)
            turn += 2   # skip the anti-repetition turn

    def test_serious_or_critical_turns_get_only_neutral_listening_tokens(self):
        neutral = {normalize_spoken_variant(e) for e in _POOLS["hi"]["ack_neutral"]}
        spoken = 0
        for seed in range(30):
            p = planner({"acknowledgement_probability": 1.0}, seed=seed)
            turn = 1
            for kwargs in ({"serious": True}, {"critical": True}):
                for context in ("answer", "question", "lookup"):
                    token = p.plan_early_ack(language="hi-IN", identity=MALE,
                                             context=context, turn_index=turn, **kwargs)
                    turn += 2
                    if token:
                        spoken += 1
                        assert normalize_spoken_variant(token) in neutral, (kwargs, context, token)
        assert spoken > 20
        # …and at half the odds: "ठीक है" after a refusal would read as acceptance.
        assert 0.15 <= self._neutral_rate() <= 0.35

    @staticmethod
    def _neutral_rate(seeds=300):
        hits = 0
        for seed in range(seeds):
            p = planner({"acknowledgement_probability": 0.5}, seed=seed)
            hits += bool(p.plan_early_ack(language="hi-IN", identity=MALE,
                                          context="answer", turn_index=5, serious=True))
        return hits / seeds

    def test_female_voice_gets_agreeing_grammar(self):
        seen = set()
        for seed in range(40):
            p = planner({"acknowledgement_probability": 1.0}, seed=seed)
            seen.add(p.plan_early_ack(language="hi-IN", identity=FEMALE,
                                      context="lookup", turn_index=1))
        joined = " ".join(seen)
        assert "देख रही हूँ" in joined
        assert "देख रहा हूँ" not in joined

    def test_english_and_fallback_languages(self):
        p = planner({"acknowledgement_probability": 1.0}, seed=1)
        en = p.plan_early_ack(language="en-IN", identity=NEUTRAL, context="answer", turn_index=1)
        assert normalize_spoken_variant(en) in {
            normalize_spoken_variant(e) for e in _POOLS["en"]["ack_answer"]
        }
        # Gujarati has no dedicated ack_* pools: its short pools stand in.
        gu = p.plan_early_ack(language="gu-IN", identity=NEUTRAL, context="answer", turn_index=3)
        assert normalize_spoken_variant(gu) in {
            normalize_spoken_variant(e) for e in _POOLS["gu"]["acknowledgement"]
        }
        gu_q = p.plan_early_ack(language="gu-IN", identity=NEUTRAL, context="question", turn_index=5)
        assert normalize_spoken_variant(gu_q) in {
            normalize_spoken_variant(e) for e in _POOLS["gu"]["thinking"]
        }

    def test_withheld_cases_report_why(self):
        p = planner({"acknowledgement_probability": 1.0})
        assert p.plan_early_ack(language="bn-IN", identity=MALE, turn_index=1) == ""
        assert p.last_early_ack_reason == "no_pool_language:bn"
        assert p.plan_early_ack(language="hi-IN", identity=MALE, turn_index=0) == ""
        assert p.last_early_ack_reason == "greeting_turn"
        off = planner({"acknowledgements": False})
        assert off.plan_early_ack(language="hi-IN", identity=MALE, turn_index=1) == ""
        assert off.last_early_ack_reason == "disabled"
        master_off = planner({"enabled": False, "acknowledgement_probability": 1.0})
        assert master_off.plan_early_ack(language="hi-IN", identity=MALE, turn_index=1) == ""
        no_think = planner({"acknowledgement_probability": 1.0, "thinking_fillers": False})
        assert no_think.plan_early_ack(language="hi-IN", identity=MALE,
                                       context="question", turn_index=1) == ""
        assert no_think.last_early_ack_reason == "thinking_disabled"
        never = planner({"acknowledgement_probability": 0.0})
        assert never.plan_early_ack(language="hi-IN", identity=MALE, turn_index=1) == ""
        assert never.last_early_ack_reason == "roll"

    def test_one_token_never_stacked(self):
        p = planner({"acknowledgement_probability": 1.0}, seed=8)
        for turn in range(1, 40, 2):
            token = p.plan_early_ack(language="hi-IN", identity=MALE,
                                     context="answer", turn_index=turn)
            assert token.count("…") <= 1 and len(token.split()) <= 3, token


class TestSentenceBreathAndAckPacing:
    def test_short_acknowledgement_is_a_touch_quicker(self):
        p = planner()
        seg = p.plan_segment("ठीक है।", base_pause_ms=150, language="hi-IN")
        assert seg.speed_scale is not None and 1.02 <= seg.speed_scale <= 1.05

    def test_breath_only_before_long_or_critical_sentences_in_pause_mode(self):
        p = planner({"sentence_breath_probability": 1.0})
        long = "Aapke account mein pichle mahine ki kist abhi tak update nahi hui hai isliye"
        assert p.plan_segment(long, base_pause_ms=150, language="hi-IN").breath_before is True
        assert p.plan_segment("Ji, theek hai.", base_pause_ms=150,
                              language="hi-IN").breath_before is False
        assert p.plan_segment("Aapka balance ₹5000 hai.", base_pause_ms=150,
                              language="hi-IN").breath_before is True      # critical
        # Never before the first sentence, never a second one in the turn,
        # never outside pause mode.
        assert p.plan_segment(long, base_pause_ms=150, language="hi-IN",
                              first_in_turn=True).breath_before is False
        assert p.plan_segment(long, base_pause_ms=150, language="hi-IN",
                              breaths_so_far=1).breath_before is False
        assert p.plan_segment(long, base_pause_ms=0, language="hi-IN").breath_before is False

    def test_breaths_are_rare_and_switchable(self):
        p = planner(seed=5)
        long = "Aapke account mein pichle mahine ki kist abhi tak update nahi hui hai isliye"
        hits = sum(
            p.plan_segment(long, base_pause_ms=150, language="hi-IN").breath_before
            for _ in range(200)
        )
        assert 45 <= hits <= 100                       # default probability 0.35
        off = planner({"sentence_breaths": False, "sentence_breath_probability": 1.0})
        assert off.plan_segment(long, base_pause_ms=150, language="hi-IN").breath_before is False
        assert HUMAN_SPEECH_DEFAULTS["sentence_breaths"] is True
        assert HUMAN_SPEECH_DEFAULTS["sentence_breath_probability"] == 0.35
        assert validate_human_speech({"sentence_breath_probability": 1.5}) == [
            "'sentence_breath_probability' must be between 0 and 1",
        ]
