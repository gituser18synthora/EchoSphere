"""Caller speech-level baseline — the "is this the same person?" heuristic.

Every stage ahead of the conversation engine judges *whether* something is
speech, never *whose* speech it is: the caller audio gate is energy against
the line's noise floor, Silero is speech-vs-non-speech, the STT transcribes any
speech it is given, and the transcript gate checks script, language and
duration. A second person talking near the handset therefore arrives as a
perfectly valid caller turn (2026-09-18 investigation: another speaker at
−36…−48 dBFS opened the gate, scored 0.98+ on Silero and was transcribed
verbatim by Sarvam on both transports).

What *does* separate the two acoustically, without a speaker model, is level:
the primary caller holds the handset and lands at a stable level for the
call, while someone across the room arrives 10–20 dB quieter. This module
keeps a per-call baseline of the CALLER's own speech level and classifies
later segments relative to it. It is deliberately conservative:

- the baseline learns only from segments the brain vouches for (accepted
  final, not captured during bot audio, long/contentful enough — see
  :func:`qualifies_for_baseline`), so a rejected hallucination, a backchannel
  or a segment already judged background never trains it;
- it is not trusted until the CALL ITSELF has vouched for at least one
  sample (:meth:`CallerLevelBaseline.observe_trusted`: identity confirmed,
  identifier validated, workflow advanced on that turn) AND several
  candidates AGREE with each other (``min_segments`` within
  ``consistency_db`` of the candidate median). Unvouched candidates refine
  the level but can never establish trust on their own (2026-09-24
  evaluation: three same-level background sentences accepted early
  established the baseline and then held the real, quieter caller), and
  until trust exists every verdict is :data:`LABEL_UNKNOWN`, which callers
  must treat as "behave exactly as before" rather than as a guess;
- once trusted it is the MEDIAN of the consistent candidates among the last
  few, so one loud or quiet segment cannot move it far, and a segment judged
  background never trains it — repeated quiet speech is not evidence that
  it came from the caller (only independent speaker evidence could be; see
  :meth:`CallerLevelBaseline.rebase`);
- the only output is a relative delta and a label; policy (hold, suppress a
  barge-in) lives with the caller, and the whole mechanism is
  feature-controlled through tenant Turn Detection
  (``background_speech_guard``) so it can be enabled per tenant.

Level is the caller audio gate's own per-segment measurement
(``speech_snapshot()["speech_dbfs"]``: power-mean dBFS of the frames that
passed the gate), so "loud" means the same thing here as in the SNR the
transcript gate already uses.
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass

# Candidate memory: the last N qualifying caller segments. Long enough that a
# single outlier is out-voted, short enough that a caller who genuinely
# changes level (within the margin) is followed within a few turns.
BASELINE_HISTORY = 8
# Candidates must agree this closely (dB from the candidate median) to count
# toward trust. Genuine caller turns with the bot quiet sit within a few dB of
# each other (2026-09-18 evaluation: interquartile range −3.6…+2 dB over 77
# real turns); a background sentence accepted early lands 10–20 dB away.
DEFAULT_CONSISTENCY_DB = 6.0
# A segment must carry this much gated speech to train the baseline: shorter
# bursts ("haan", a cough) have unstable level measurements.
MIN_TRAIN_SEGMENT_MS = 600.0
# ...and at least this many language-carrying words (the brain passes the
# count after stripping numeric/technical payload).
MIN_TRAIN_WORDS = 2
# Live (still-open) gate segments shorter than this carry too little audio
# for a level verdict — onset frames bias the running mean low.
MIN_LIVE_SEGMENT_MS = 300.0

LABEL_UNKNOWN = "unknown"            # no trusted baseline yet: preserve current behaviour
LABEL_CALLER = "caller"
LABEL_BACKGROUND = "background_suspect"


@dataclass(frozen=True)
class LevelVerdict:
    """Relative-level judgement for one speech segment."""

    label: str
    level_dbfs: float | None
    baseline_dbfs: float | None
    delta_db: float | None
    margin_db: float
    baseline_segments: int

    @property
    def suspect(self) -> bool:
        return self.label == LABEL_BACKGROUND

    def as_event(self) -> dict:
        return {
            "label": self.label,
            "speech_dbfs": self.level_dbfs,
            "baseline_dbfs": self.baseline_dbfs,
            "delta_db": self.delta_db,
            "margin_db": self.margin_db,
            "baseline_segments": self.baseline_segments,
        }


def qualifies_for_baseline(
    *,
    accepted: bool,
    verdict_reason: str,
    during_bot_audio: bool,
    segment_ms: float | None,
    words: int,
    suspect: bool,
) -> bool:
    """Whether an accepted segment is reliable enough to train the baseline.

    Only plainly accepted finals count: a rescue (transliteration,
    re-transcription, digit payload, stripped announcement) is by definition
    a segment the pipeline was unsure about. Audio captured while the bot was
    speaking may be echo or a backchannel; a short or low-content segment has
    an unstable level; a segment already judged background must never pull the
    baseline toward the background.
    """
    if not accepted or verdict_reason != "ok" or during_bot_audio or suspect:
        return False
    if segment_ms is None or segment_ms < MIN_TRAIN_SEGMENT_MS:
        return False
    return words >= MIN_TRAIN_WORDS


class CallerLevelBaseline:
    """Per-call caller speech-level baseline and relative classifier.

    Args:
        margin_db: A segment this far (or further) BELOW the baseline is
            ``background_suspect``.
        bot_audio_allowance_db: Extra margin for speech captured WHILE THE BOT
            IS SPEAKING. Browser echo cancellers and handset half-duplex
            suppression attenuate the near end during far-end playback, so a
            genuine caller barge-in measures well below the same caller's
            quiet-bot level (2026-09-18 evaluation over 89 real turns: median
            −9.6 dB, first quartile −15.4 dB, vs ±3 dB with the bot quiet).
            Without this allowance the guard would hold exactly the
            interruptions it must protect.
        min_segments: Qualifying segments that must AGREE (see
            ``consistency_db``) before the baseline is trusted; until then
            every verdict is ``unknown``.
        consistency_db: How far a candidate may sit from the candidate median
            and still count as agreeing.
        enforce: Whether the owning pipeline acts on verdicts. False is
            "shadow mode": verdicts are computed and recorded for evidence,
            behaviour is unchanged. Exposed here so every consumer reads one
            flag.
    """

    def __init__(
        self, *, margin_db: float = 10.0, min_segments: int = 3, enforce: bool = False,
        bot_audio_allowance_db: float = 12.0, consistency_db: float = DEFAULT_CONSISTENCY_DB,
        history: int = BASELINE_HISTORY,
    ) -> None:
        self.margin_db = float(margin_db)
        self.bot_audio_allowance_db = max(0.0, float(bot_audio_allowance_db))
        self.min_segments = max(1, int(min_segments))
        self.consistency_db = max(0.5, float(consistency_db))
        self.enforce = bool(enforce)
        self._levels: deque[float] = deque(maxlen=max(1, int(history)))
        self._rebased = 0
        self._trusted = 0
        self._vouched = False

    # ── baseline ─────────────────────────────────────────────────────────
    def _consistent(self) -> list[float]:
        """Candidates that agree with the candidate median."""
        if not self._levels:
            return []
        center = statistics.median(self._levels)
        return [c for c in self._levels if abs(c - center) <= self.consistency_db]

    @property
    def established(self) -> bool:
        """Trusted: the call vouched for the caller's level at least once AND
        enough candidates agree with each other. Candidates alone — however
        many and however consistent — never establish trust: nothing about
        them says whose speech they were."""
        return self._vouched and len(self._consistent()) >= self.min_segments

    @property
    def vouched(self) -> bool:
        """Whether any sample came with independent caller evidence."""
        return self._vouched

    @property
    def candidate_dbfs(self) -> float | None:
        """Median of the agreeing candidates regardless of trust (evidence
        only — never used for a verdict)."""
        consistent = self._consistent()
        if len(consistent) < self.min_segments:
            return None
        return round(statistics.median(consistent), 1)

    @property
    def segments(self) -> int:
        """Qualifying candidates observed (agreeing or not)."""
        return len(self._levels)

    @property
    def consistent_segments(self) -> int:
        return len(self._consistent())

    @property
    def baseline_dbfs(self) -> float | None:
        """The trusted baseline, or None while not established."""
        if not self._vouched:
            return None
        return self.candidate_dbfs

    def observe(self, level_dbfs: float) -> float | None:
        """Fold one qualifying caller segment's level into the candidates.

        Returns the trusted baseline after the update (None while not trusted).
        The caller must only pass segments :func:`qualifies_for_baseline`
        vouches for — in particular never a background-suspect one.
        """
        try:
            value = float(level_dbfs)
        except (TypeError, ValueError):
            return self.baseline_dbfs
        if value != value:  # NaN
            return self.baseline_dbfs
        self._levels.append(value)
        return self.baseline_dbfs

    def rebase(self, level_dbfs: float) -> float | None:
        """Restart the baseline at a level INDEPENDENT evidence attributes to
        the primary caller (e.g. a future speaker-verification match).

        Not called by the level policy itself: repeated quiet speech is not
        evidence of who spoke it, so a suspect segment can never rebase.
        Seeds ``min_segments`` agreeing candidates so trust is immediate.
        """
        self._levels.clear()
        self._rebased += 1
        self._vouched = True
        result = None
        for _ in range(self.min_segments):
            result = self.observe(level_dbfs)
        return result

    @property
    def rebased(self) -> int:
        return self._rebased

    @property
    def trusted_segments(self) -> int:
        """Trusted caller samples applied so far (see :meth:`observe_trusted`)."""
        return self._trusted

    def observe_trusted(self, level_dbfs: float) -> tuple[bool, float | None]:
        """Apply a level the CALL ITSELF proved belongs to the primary caller.

        Independent evidence that a dispatched turn came from the caller —
        the collections policy confirmed identity on it, a dictated
        identifier validated, or the workflow advanced on-script — is what
        the candidate bootstrap lacks. Before trust exists such a sample
        seeds the baseline at once (``rebase``), so background that spoke
        first cannot own it. Afterwards it refreshes like any candidate —
        unless it disagrees MATERIALLY (more than ``consistency_db``) with
        the trusted baseline: the caller's level has then genuinely moved
        (handset to speaker, moved away from the phone), and the baseline
        follows at once rather than holding the caller's next turns as
        background until the candidate history catches up.
        Returns ``(seeded, baseline_after)``.
        """
        try:
            value = float(level_dbfs)
        except (TypeError, ValueError):
            return False, self.baseline_dbfs
        if value != value:  # NaN
            return False, self.baseline_dbfs
        self._trusted += 1
        baseline = self.baseline_dbfs
        if baseline is None or abs(value - baseline) > self.consistency_db:
            return True, self.rebase(value)
        return False, self.observe(value)

    # ── classification ───────────────────────────────────────────────────
    def effective_margin(self, during_bot_audio: bool = False) -> float:
        """The margin applied to a segment: wider while the bot is speaking."""
        if during_bot_audio:
            return self.margin_db + self.bot_audio_allowance_db
        return self.margin_db

    def classify(
        self, level_dbfs: float | None, *, during_bot_audio: bool = False
    ) -> LevelVerdict:
        """Relative-level verdict for a segment measured at ``level_dbfs``.

        ``during_bot_audio`` widens the margin by the bot-audio allowance (see
        the class docstring): the caller's own barge-ins measure quieter.
        """
        margin = self.effective_margin(during_bot_audio)
        baseline = self.baseline_dbfs
        try:
            level = None if level_dbfs is None else float(level_dbfs)
        except (TypeError, ValueError):
            level = None
        if level is None or level != level or baseline is None:
            return LevelVerdict(
                LABEL_UNKNOWN, level, baseline, None, margin, self.consistent_segments,
            )
        delta = round(level - baseline, 1)
        label = LABEL_BACKGROUND if delta <= -margin else LABEL_CALLER
        return LevelVerdict(label, level, baseline, delta, margin, self.consistent_segments)

    def classify_live(self, gate) -> LevelVerdict | None:
        """Verdict for the gate's LIVE segment, or None when there is not yet
        enough of it to judge (the barge-in arbiter must then decide as if no
        classifier existed). The barge-in strategy only consults this while
        the bot is speaking, so the bot-audio allowance always applies."""
        if gate is None:
            return None
        try:
            live_ms = float(gate.live_speech_ms)
            snapshot = gate.speech_snapshot()
        except Exception:  # noqa: BLE001 — evidence must never break a turn
            return None
        if live_ms < MIN_LIVE_SEGMENT_MS or not snapshot:
            return None
        return self.classify(snapshot.get("speech_dbfs"), during_bot_audio=True)
