"""SpeechNaturalnessPlanner — config resolution, contextual/probabilistic
filler planning, gender agreement, critical-content safety, per-sentence
delivery, backchannels and self-correction. Pure unit level (seeded RNG)."""

import random

import pytest

from shared.orchestration.naturalness import (
    HUMAN_SPEECH_DEFAULTS,
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

    def test_fillers_are_occasional_not_constant(self):
        p = planner(seed=13)
        used = sum(
            1 for i in range(1, 101)
            if p.plan_turn(language="hi-IN", identity=MALE,
                           route_kind="llm", turn_index=i).has_preface
        )
        # Default thinking probability 0.25, dampened after a decorated turn.
        assert 3 <= used <= 40

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
