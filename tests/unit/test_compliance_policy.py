"""Deterministic compliance enforcement — pure-logic tests with a fixed
clock and explicit timezones. No database or Redis (stubbed where needed)."""

from datetime import datetime, timezone

import pytest

from shared.compliance import (
    CompliancePolicySnapshot,
    WordingTemplate,
    check_and_count_contact,
    check_calling_window,
    resolve_wording,
    substitute_wordings,
)
from shared.guardrails import (
    MANDATORY_FLOOR,
    EffectiveGuardrails,
    GuardrailEngine,
)


def _policy(**overrides) -> CompliancePolicySnapshot:
    base = dict(
        policy_id="cp_test", code="test_policy", version=3,
        name="Test policy", regulator="RBI", jurisdiction="IN",
        timezone="Asia/Kolkata",
    )
    base.update(overrides)
    return CompliancePolicySnapshot(**base)


IST_WINDOW = ({"days": [0, 1, 2, 3, 4, 5, 6], "start": "08:00", "end": "19:00"},)


def utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


class TestCallingWindows:
    def test_inside_the_window_is_allowed(self):
        policy = _policy(calling_windows=IST_WINDOW)
        # 2026-08-10 03:30 UTC = 09:00 IST (Monday)
        decision = check_calling_window(policy, utc(2026, 8, 10, 3, 30))
        assert decision.allowed

    def test_outside_the_window_is_blocked(self):
        policy = _policy(calling_windows=IST_WINDOW)
        # 15:00 UTC = 20:30 IST — after the RBI 19:00 boundary
        decision = check_calling_window(policy, utc(2026, 8, 10, 15, 0))
        assert not decision.allowed
        assert decision.policy_code == "test_policy" and decision.policy_version == 3
        assert "IST" in (decision.local_time or "")

    def test_window_edges_start_inclusive_end_exclusive(self):
        policy = _policy(calling_windows=IST_WINDOW)
        # 02:30 UTC = 08:00 IST exactly → allowed; 13:30 UTC = 19:00 IST → blocked
        assert check_calling_window(policy, utc(2026, 8, 10, 2, 30)).allowed
        assert not check_calling_window(policy, utc(2026, 8, 10, 13, 30)).allowed

    def test_daylight_saving_boundary_is_handled_by_zoneinfo(self):
        policy = _policy(timezone="America/New_York", calling_windows=(
            {"days": [0, 1, 2, 3, 4, 5, 6], "start": "08:00", "end": "19:00"},
        ))
        # Winter (EST, UTC-5): 12:30 UTC = 07:30 local → blocked.
        assert not check_calling_window(policy, utc(2026, 1, 15, 12, 30)).allowed
        # Summer (EDT, UTC-4): the SAME UTC time is 08:30 local → allowed.
        assert check_calling_window(policy, utc(2026, 7, 15, 12, 30)).allowed

    def test_day_restriction(self):
        weekdays_only = _policy(calling_windows=(
            {"days": [0, 1, 2, 3, 4], "start": "08:00", "end": "19:00"},
        ))
        # 2026-08-09 is a Sunday (weekday 6) — 09:00 IST but wrong day.
        assert not check_calling_window(weekdays_only, utc(2026, 8, 9, 3, 30)).allowed
        assert check_calling_window(weekdays_only, utc(2026, 8, 10, 3, 30)).allowed

    def test_overnight_window_belongs_to_its_start_day(self):
        policy = _policy(timezone="UTC", calling_windows=(
            {"days": [4], "start": "21:00", "end": "02:00"},  # Friday night
        ))
        assert check_calling_window(policy, utc(2026, 8, 7, 22, 0)).allowed   # Fri 22:00
        assert check_calling_window(policy, utc(2026, 8, 8, 1, 0)).allowed    # Sat 01:00
        assert not check_calling_window(policy, utc(2026, 8, 8, 3, 0)).allowed
        assert not check_calling_window(policy, utc(2026, 8, 6, 22, 0)).allowed  # Thu

    def test_no_windows_never_restricts(self):
        assert check_calling_window(_policy(), utc(2026, 8, 10, 22, 0)).allowed

    def test_invalid_timezone_fails_closed(self):
        policy = _policy(timezone="Not/AZone", calling_windows=IST_WINDOW)
        decision = check_calling_window(policy, utc(2026, 8, 10, 3, 30))
        assert not decision.allowed and "invalid" in decision.reason

    def test_malformed_window_never_grants(self):
        policy = _policy(calling_windows=({"start": "8am", "end": "7pm"},))
        assert not check_calling_window(policy, utc(2026, 8, 10, 3, 30)).allowed


class _RedisStub:
    def __init__(self):
        self.counts: dict[str, int] = {}
        self.keys_seen: list[str] = []

    async def incr(self, key):
        self.keys_seen.append(key)
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key, ttl):
        return True


class TestContactLimits:
    async def test_counts_atomically_and_blocks_over_limit(self, monkeypatch):
        stub = _RedisStub()
        monkeypatch.setattr("shared.db.redis.get_redis", lambda: stub)
        policy = _policy(contact_limits={"per_day": 2})
        now = utc(2026, 8, 10, 5, 0)
        for expected in (True, True, False):
            allowed, _ = await check_and_count_contact(
                policy, tenant_id="tn_x", caller="+91 98765 43210", now=now)
            assert allowed is expected
        # Raw phone numbers never appear in store keys.
        assert all("98765" not in k for k in stub.keys_seen)

    async def test_day_boundary_uses_the_policy_timezone(self, monkeypatch):
        stub = _RedisStub()
        monkeypatch.setattr("shared.db.redis.get_redis", lambda: stub)
        policy = _policy(contact_limits={"per_day": 1})
        # 19:00 UTC Aug 10 = 00:30 IST Aug 11 — a NEW local day vs 05:00 UTC.
        await check_and_count_contact(policy, tenant_id="tn_x",
                                      caller="+911234567890", now=utc(2026, 8, 10, 5, 0))
        allowed, _ = await check_and_count_contact(
            policy, tenant_id="tn_x", caller="+911234567890",
            now=utc(2026, 8, 10, 19, 0))
        assert allowed  # separate local-day bucket

    async def test_no_limit_or_no_caller_never_restricts(self, monkeypatch):
        monkeypatch.setattr("shared.db.redis.get_redis", lambda: _RedisStub())
        assert (await check_and_count_contact(
            _policy(), tenant_id="tn_x", caller="+911234567890"))[0]
        assert (await check_and_count_contact(
            _policy(contact_limits={"per_day": 1}), tenant_id="tn_x", caller=None))[0]


WORDINGS = (
    WordingTemplate(code="recovery_notice", language="en", version=1,
                    text="OLD ENGLISH TEXT."),
    WordingTemplate(code="recovery_notice", language="en", version=2,
                    text="This is a reminder that your loan account is overdue."),
    WordingTemplate(code="recovery_notice", language="hi", version=1,
                    text="यह सूचना है कि आपका लोन खाता बकाया है।"),
)


class TestWordings:
    def test_highest_version_for_the_language_wins(self):
        policy = _policy(wordings=WORDINGS)
        template, source = resolve_wording([policy], "recovery_notice", "en-IN")
        assert template.version == 2 and "reminder" in template.text
        assert source.code == "test_policy"
        template, _ = resolve_wording([policy], "recovery_notice", "hi-IN")
        assert template.language == "hi"

    def test_substitution_is_verbatim_and_reports_the_version(self):
        policy = _policy(wordings=WORDINGS)
        used = []
        out = substitute_wordings(
            "Before we continue: {{wording:recovery_notice}} Please pay soon.",
            [policy], "en", on_use=lambda w, p: used.append((w.code, w.version, p.version)),
        )
        assert "This is a reminder that your loan account is overdue." in out
        assert "{{" not in out
        assert used == [("recovery_notice", 2, 3)]

    def test_unresolved_reference_is_dropped_never_spoken_raw(self):
        out = substitute_wordings("Hello {{wording:missing_code}} there.", [_policy()], "en")
        assert "{{" not in out and "missing_code" not in out

    def test_unknown_language_falls_back_to_english(self):
        template, _ = resolve_wording([_policy(wordings=WORDINGS)], "recovery_notice", "ta-IN")
        assert template.language == "en" and template.version == 2


WAIVER_POLICY = _policy(waiver_rules={
    "require_authorization": True,
    "patterns": [
        r"\b(?:i|we)\s+(?:can|will)\s+(?:waive|write\s*off|discount)\b",
        r"\b(?:penalty|late\s*fee)\b[^.?!\n]{0,30}\bwaived?\b",
    ],
})

CONDUCT_POLICY = _policy(code="conduct", version=2, prohibited_conduct=[
    {"code": "threat_or_intimidation", "action": "block",
     "patterns": [r"\byou will (?:be arrested|go to jail)\b"]},
    {"code": "competitor_mention", "action": "flag",
     "patterns": [r"\bother lenders\b"]},
])


def _engine(policies) -> GuardrailEngine:
    return GuardrailEngine(EffectiveGuardrails(rules=MANDATORY_FLOOR),
                           compliance=policies)


class TestWaiverEnforcement:
    def test_unauthorized_waiver_is_blocked_pre_tts_and_escalates(self):
        engine = _engine([WAIVER_POLICY])
        engine.begin_turn()
        assert engine.has_output_block_rules  # sentence-hold streaming active
        result = engine.check_output_stream("Good news, we can waive the late fee")
        assert result.blocked and result.reply_key == "guardrail_waiver"
        hit = engine.hits[-1]
        assert hit.rule.code == "waiver_unauthorized"
        assert hit.policy_code == "test_policy" and hit.policy_version == 3
        assert hit.outcome == "escalated"
        # The block also gates every tool call this turn.
        assert engine.check_tool_call(intent="apply_waiver").allowed is False

    def test_authorized_waiver_proceeds_with_recorded_evidence(self):
        engine = _engine([WAIVER_POLICY])
        engine.begin_turn()
        engine.record_waiver_authorization(reference="APR-1201", expires_at=None)
        result = engine.check_output_stream("Good news, we can waive the late fee")
        assert not result.blocked
        assert any(h.rule.code == "waiver_promise_authorized" for h in engine.hits)

    def test_expired_authorization_blocks_again(self):
        engine = _engine([WAIVER_POLICY])
        engine.begin_turn()
        engine.record_waiver_authorization(reference="APR-9", expires_at=1000.0)
        assert engine.waiver_authorized(now=999.0)
        assert not engine.waiver_authorized(now=1001.0)

    def test_prompt_text_alone_never_authorizes(self):
        engine = _engine([WAIVER_POLICY])
        engine.begin_turn()
        # No tool result was recorded — whatever the model claims, block.
        result = engine.check_output_text(
            "As authorized, I will waive the penalty for you"
        )
        assert result.blocked


class TestConductEnforcement:
    def test_threat_is_blocked_before_tts(self):
        engine = _engine([CONDUCT_POLICY])
        engine.begin_turn()
        result = engine.check_output_stream("Pay now or you will be arrested tomorrow")
        assert result.blocked and result.reply_key == "guardrail_blocked"
        hit = engine.hits[-1]
        assert hit.rule.code == "threat_or_intimidation"
        assert (hit.policy_code, hit.policy_version, hit.outcome) == ("conduct", 2, "blocked")

    def test_flag_action_records_without_blocking(self):
        engine = _engine([CONDUCT_POLICY])
        engine.begin_turn()
        result = engine.check_output_text("Unlike other lenders we offer flexibility")
        assert not result.blocked
        assert any(h.rule.code == "competitor_mention" and h.outcome == "flagged"
                   for h in engine.hits)

    def test_legitimate_collections_content_is_untouched(self):
        engine = _engine([CONDUCT_POLICY, WAIVER_POLICY])
        engine.begin_turn()
        text = ("Your outstanding amount is 4,520 rupees. If the account stays "
                "overdue, a recovery notice may be issued as per policy.")
        result = engine.check_output_text(text)
        assert not result.blocked and result.text == text

    def test_broken_policy_pattern_is_skipped_not_fatal(self):
        broken = _policy(prohibited_conduct=[
            {"code": "bad", "action": "block", "patterns": ["([unclosed"]},
        ])
        engine = _engine([broken])
        engine.begin_turn()
        assert not engine.check_output_text("hello there").blocked


@pytest.mark.parametrize("locale,needle", [("en", "waiver"), ("hi-IN", "छूट")])
def test_waiver_escalation_reply_is_localized(locale, needle):
    from shared.guardrails import guardrail_reply

    assert needle in guardrail_reply("guardrail_waiver", locale)
