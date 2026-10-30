"""Caller speech-level baseline (voice_runtime.caller_level)."""

from voice_runtime.caller_level import (
    LABEL_BACKGROUND,
    LABEL_CALLER,
    LABEL_UNKNOWN,
    CallerLevelBaseline,
    qualifies_for_baseline,
)


class TestBaseline:
    def test_unvouched_candidates_never_establish_trust(self):
        # Three agreeing accepted segments are still only candidates: nothing
        # about them says whose speech they were. Verdicts stay unknown.
        b = CallerLevelBaseline(margin_db=10, min_segments=3, consistency_db=6.0)
        assert not b.established and b.baseline_dbfs is None
        assert b.classify(-60.0).label == LABEL_UNKNOWN
        for level in (-30.0, -31.0, -29.0):
            b.observe(level)
        assert not b.established and not b.vouched
        assert b.baseline_dbfs is None and b.candidate_dbfs == -30.0
        assert b.classify(-60.0).label == LABEL_UNKNOWN
        # One sample the call vouched for makes the baseline trusted at once.
        seeded, after = b.observe_trusted(-30.0)
        assert seeded and after == -30.0
        assert b.established and b.vouched and b.baseline_dbfs == -30.0
        assert b.classify(-60.0).suspect

    def test_disagreeing_candidates_do_not_pull_a_trusted_baseline(self):
        # Trusted caller at −25, then background at −42 accepted as a
        # candidate: excluded from the consistent set, baseline unchanged.
        b = CallerLevelBaseline(margin_db=12, min_segments=3, consistency_db=6.0)
        b.observe_trusted(-25.0)
        b.observe(-42.0)
        b.observe(-26.0)
        assert b.established and b.baseline_dbfs == -25.0
        assert b.consistent_segments == 4 and b.segments == 5
        assert b.classify(-42.0).suspect

    def test_background_first_cannot_own_the_baseline(self):
        # Three agreeing background sentences before the caller speaks used to
        # establish a (wrong) baseline that then held the real, quieter
        # caller. They now establish nothing; the caller's first vouched turn
        # seeds the baseline at the caller's level and the background becomes
        # the suspect.
        b = CallerLevelBaseline(margin_db=12, min_segments=3, consistency_db=6.0)
        for level in (-42.0, -41.0, -43.0):
            b.observe(level)
        assert not b.established and b.candidate_dbfs == -42.0
        assert b.classify(-55.0).label == LABEL_UNKNOWN  # a quieter caller is not held
        seeded, after = b.observe_trusted(-25.0)
        assert seeded and after == -25.0 and b.baseline_dbfs == -25.0
        assert b.classify(-42.0).suspect
        assert not b.classify(-25.0).suspect

    def test_trusted_sample_refreshes_within_consistency_and_rebases_beyond(self):
        b = CallerLevelBaseline(margin_db=10, min_segments=3, consistency_db=6.0)
        assert b.observe_trusted(-30.0) == (True, -30.0)
        assert b.rebased == 1
        # Within 6 dB of the baseline: refreshes like any candidate.
        seeded, after = b.observe_trusted(-33.0)
        assert not seeded and -31.0 <= after <= -30.0 and b.rebased == 1
        # The caller moved to speakerphone: a vouched turn 12 dB quieter
        # would otherwise be held as background until the history caught up.
        assert b.classify(-42.0).suspect
        seeded, after = b.observe_trusted(-42.0)
        assert seeded and after == -42.0 and b.rebased == 2
        assert b.classify(-42.0).label == LABEL_CALLER
        assert b.trusted_segments == 3

    def test_median_resists_one_outlier(self):
        b = CallerLevelBaseline(margin_db=10, min_segments=3)
        b.observe_trusted(-30.0)
        for level in (-31, -29, -30):
            b.observe(level)
        b.observe(-10.0)  # one shouted turn
        assert b.baseline_dbfs == -30.0
        b.observe(-55.0)  # one very quiet turn
        assert -31.0 <= b.baseline_dbfs <= -29.0

    def test_history_is_bounded_so_baseline_follows_a_real_change(self):
        b = CallerLevelBaseline(margin_db=10, min_segments=3, history=4)
        b.observe_trusted(-20.0)
        b.observe(-20.0)
        for _ in range(4):
            b.observe(-40.0)
        assert b.baseline_dbfs == -40.0

    def test_junk_levels_are_ignored(self):
        b = CallerLevelBaseline(margin_db=10, min_segments=1)
        b.observe(None)
        b.observe("loud")
        b.observe(float("nan"))
        assert not b.established
        assert b.classify(None).label == LABEL_UNKNOWN

    def test_rebase_restarts_at_the_confirmed_level(self):
        b = CallerLevelBaseline(margin_db=10, min_segments=3)
        for _ in range(5):
            b.observe(-20.0)
        b.rebase(-38.0)
        assert b.rebased == 1
        # Established at once at the confirmed level: the caller's very next
        # turn must not be held again.
        assert b.established and b.baseline_dbfs == -38.0
        assert b.classify(-38.0).label == LABEL_CALLER
        assert b.classify(-20.0).label == LABEL_CALLER  # louder is never suspect
        b.observe(-37.0)
        assert -38.0 <= b.baseline_dbfs <= -37.0


class TestClassification:
    def _established(self, margin=10.0, allowance=12.0):
        b = CallerLevelBaseline(
            margin_db=margin, min_segments=3, bot_audio_allowance_db=allowance
        )
        b.observe_trusted(-30.0)
        return b

    def test_caller_within_margin(self):
        b = self._established()
        for level in (-22.0, -30.0, -35.0, -39.9):
            verdict = b.classify(level)
            assert verdict.label == LABEL_CALLER, level
            assert not verdict.suspect

    def test_background_at_or_beyond_margin(self):
        b = self._established()
        verdict = b.classify(-40.0)
        assert verdict.suspect and verdict.label == LABEL_BACKGROUND
        assert verdict.delta_db == -10.0 and verdict.baseline_dbfs == -30.0
        assert b.classify(-52.0).suspect

    def test_bot_audio_allowance_widens_the_margin(self):
        b = self._established(margin=10.0, allowance=12.0)
        # 15 dB below the caller: background while the bot is quiet, but a
        # normal echo-cancelled barge-in while the bot is speaking.
        assert b.classify(-45.0).suspect
        assert not b.classify(-45.0, during_bot_audio=True).suspect
        assert b.classify(-52.0, during_bot_audio=True).suspect
        assert b.effective_margin(True) == 22.0 and b.effective_margin() == 10.0

    def test_event_payload_is_flat_numbers(self):
        b = self._established()
        event = b.classify(-45.0).as_event()
        assert event == {
            "label": LABEL_BACKGROUND, "speech_dbfs": -45.0, "baseline_dbfs": -30.0,
            "delta_db": -15.0, "margin_db": 10.0, "baseline_segments": 3,
        }


class _Gate:
    def __init__(self, live_ms, level):
        self.live_speech_ms = live_ms
        self._level = level

    def speech_snapshot(self):
        return {"speech_dbfs": self._level, "segment_ms": self.live_speech_ms, "live": True}


class TestLiveClassification:
    def test_too_little_live_audio_gives_no_verdict(self):
        b = CallerLevelBaseline(margin_db=10, min_segments=1)
        b.observe(-30.0)
        assert b.classify_live(_Gate(120.0, -60.0)) is None
        assert b.classify_live(None) is None

    def test_live_verdict_uses_the_bot_audio_allowance(self):
        b = CallerLevelBaseline(margin_db=10, min_segments=1, bot_audio_allowance_db=12)
        b.observe_trusted(-30.0)
        assert not b.classify_live(_Gate(500.0, -45.0)).suspect
        assert b.classify_live(_Gate(500.0, -55.0)).suspect


class TestQualification:
    def _q(self, **overrides):
        base = dict(
            accepted=True, verdict_reason="ok", during_bot_audio=False,
            segment_ms=900.0, words=3, suspect=False,
        )
        base.update(overrides)
        return qualifies_for_baseline(**base)

    def test_plain_accepted_multiword_segment_qualifies(self):
        assert self._q()

    def test_exclusions(self):
        assert not self._q(accepted=False)
        assert not self._q(verdict_reason="transliterated_short_reply")
        assert not self._q(verdict_reason="digit_payload")
        assert not self._q(during_bot_audio=True)
        assert not self._q(segment_ms=400.0)
        assert not self._q(segment_ms=None)
        assert not self._q(words=1)
        assert not self._q(suspect=True)
