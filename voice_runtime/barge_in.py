"""Word-confirmed barge-in — the user-turn start policy for noisy callers.

With the stock ``VADUserTurnStartStrategy``, ANY audio that survives the
caller audio gate and reads as speech to Silero starts a user turn, and a turn
started while the bot is speaking is an interruption: the reply's audio is
cancelled mid-word. That is the right behaviour for a genuine barge-in, but
background conversation is real speech too — an energy gate cannot separate a
colleague talking near the caller's microphone from the caller, so every blip
of ambient chatter chopped the bot off (observed in browser testing as
"choppy, breaking audio": 2–4 ``barge_in`` cancellations per short call, the
greeting cut twice within 3 seconds).

This strategy makes the *transcript* the arbiter while the bot is speaking,
with a sustained-speech fallback for providers that cannot produce one
mid-utterance:

- bot quiet → VAD starts the turn, exactly as before (latency unchanged);
- bot speaking → VAD activity is noted but does NOT start a turn; the turn
  (and the interruption it implies) fires when EITHER
  (a) the STT transcribes at least ``min_words`` words — ambient noise rarely
  survives STT as multiple confident words, while a real "एक मिनट रुकिए"
  does; OR
  (b) VAD speech has been SUSTAINED for ``vad_fallback_secs`` — the
  transcript arbiter assumes interim transcripts exist, but Sarvam's
  streaming STT only produces a final when its socket is flushed at VAD
  stop, so a caller who keeps talking would otherwise never be able to
  interrupt at all. Post-gate noise (already 200 ms sustained and above the
  echo margin) very rarely also sustains Silero speech for a full second.
- bot stops speaking while gated VAD speech is live → the turn opens
  immediately: the word gate's rationale (protecting audible speech from
  being chopped) no longer applies, and without this the caller's opening
  words waited on the transcript for nothing.

A one-word backchannel ("हाँ", "hmm") still never silences the bot: it
neither reaches ``min_words`` nor sustains VAD speech for the fallback
window.

Hybrid / provisional mode (``provisional_duck=True``): the arbiters above
decide too late to be gentle and too early to be sure. In provisional mode a
VAD start while the bot speaks marks a PROVISIONAL interruption and asks the
playback duck (voice_runtime.playback_duck) to pause the reply at once; the
interruption is COMMITTED when speech sustains for ``commit_secs`` or a final
carries ``min_words`` words, and otherwise, when the VAD stops first, the
reply RESUMES where it paused and no turn is opened (the transcript, if any,
is judged by the brain's own rules). 2026-09-18 benchmark over 472 episodes:
every genuine substantive barge-in still commits; blips and backchannels
become a short pause instead of a cancelled reply.

Background-speech guard (optional ``speech_classifier``): another person
talking near the handset is real, multi-word, sustained speech — both
arbiters above confirm it (2026-09-18: a second speaker at −40…−50 dBFS
interrupted the bot through the sustained-VAD fallback on every trial). When
the pipeline supplies a caller-level classifier (voice_runtime.caller_level),
each confirmation first asks whether the LIVE gated speech sits far below the
caller's own established level; if so the interruption is withheld (recorded
through ``on_suppressed``) and the bot keeps talking. Genuine caller-level
speech — and any speech before a baseline exists — confirms exactly as
before. In shadow mode (``enforce_background=False``) the verdict is only
recorded.
"""

import inspect
import logging
import time

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start.base_user_turn_start_strategy import (
    BaseUserTurnStartStrategy,
)

from voice_runtime.announcements import speech_word_count, speech_words

logger = logging.getLogger(__name__)

# A suppressed sustained-speech confirmation is re-evaluated at this cadence
# while the same VAD episode continues (the caller may start talking OVER the
# background, raising the live level), bounding the evidence events per second.
_SUPPRESSION_RECHECK_S = 0.5
# Interim-transcript confirmation (realtime STT partials while the bot speaks):
# the VAD must have shown the caller speaking for at least this long, and the
# interim's leading words must be unchanged since the previous interim of the
# same speech episode (stable over this many consecutive updates).
_INTERIM_VAD_MIN_S = 0.3
_INTERIM_STABLE_UPDATES = 2
_INTERIM_HISTORY = 8


async def _call_hook(hook, *args) -> None:
    """Invoke a sync or async evidence/control hook; never let it break a turn."""
    if hook is None:
        return
    try:
        result = hook(*args)
        if inspect.isawaitable(result):
            await result
    except Exception:  # noqa: BLE001 — hooks are side channels
        logger.debug("barge-in hook failed", exc_info=True)


class WordConfirmedBargeInStrategy(BaseUserTurnStartStrategy):
    """VAD-started turns while the bot is quiet; word- or duration-confirmed
    while it speaks.

    Args:
        min_words: Transcribed words required for a turn start (= interruption)
            while the bot is speaking. A FINAL transcript confirms on its own.
            An INTERIM (partial) transcript confirms only when its leading
            words are stable across two consecutive updates AND the VAD shows
            sustained caller speech right now (``_INTERIM_VAD_MIN_S``): on the
            Sarvam realtime socket the partial of an always-open utterance
            accumulates the bot's own PSTN echo word by word and, counted
            alone, cut every long reply 2–6 s in (live, 2026-10-22).
        vad_fallback_secs: Sustained gated VAD speech that confirms a barge-in
            without any transcript. 0 disables the fallback (transcript-only
            confirmation, the pre-2026-08-11 behaviour). Used when
            ``provisional_duck`` is off.
        provisional_duck: Hybrid mode — pause the reply at VAD start, commit
            after ``commit_secs`` of sustained speech or ``min_words`` words,
            otherwise resume (see module docstring).
        commit_secs: Sustained gated speech that commits a provisional
            interruption in hybrid mode.
        on_provisional: Control hook ``(active: bool)`` (sync or async) —
            the playback duck's ``set_provisional``.
        speech_classifier: Optional zero-argument callable returning a
            ``LevelVerdict`` (or None when the live segment is too short to
            judge) for the caller audio gate's live segment. A
            ``background_suspect`` verdict withholds the interruption.
        enforce_background: When False the classifier's verdict is recorded
            via ``on_suppressed`` but the interruption proceeds (shadow mode).
        on_confirmed: Evidence hook ``(reason, sustained_seconds)``.
        on_provisional_change: Evidence hook ``(state, sustained_seconds)``
            with state ``"provisional"`` or ``"resumed"``.
        on_suppressed: Evidence hook ``(arbiter, sustained_seconds, verdict,
            enforced)`` called when a confirmation was (or in shadow mode,
            would have been) withheld as background speech.
    """

    def __init__(
        self, *, min_words: int = 2, vad_fallback_secs: float = 1.0,
        provisional_duck: bool = False, commit_secs: float = 1.0,
        on_provisional=None, on_provisional_change=None,
        on_confirmed=None, speech_classifier=None, enforce_background: bool = True,
        on_suppressed=None, **kwargs
    ):
        super().__init__(**kwargs)
        self._min_words = max(1, int(min_words))
        self._vad_fallback_secs = max(0.0, float(vad_fallback_secs))
        self._provisional_duck = bool(provisional_duck)
        self._commit_secs = max(0.1, float(commit_secs))
        self._on_provisional = on_provisional
        self._on_provisional_change = on_provisional_change
        self._bot_speaking = False
        self._vad_speech_since: float | None = None
        self._provisional = False
        # Evidence hook: the gateway log used to be the only place the
        # confirmation reason existed, so a call's barge-ins could not be
        # classified from its own event stream (2026-09-17 live audit had to
        # join journalctl lines to Mongo by wall-clock). The pipeline wires
        # this to the session recorder.
        self._on_confirmed = on_confirmed
        self._speech_classifier = speech_classifier
        self._enforce_background = bool(enforce_background)
        self._on_suppressed = on_suppressed
        self._last_suppressed_at: float | None = None
        # Leading words of the interim transcripts seen in the current VAD
        # speech episode, newest last (see _interim_confirms).
        self._interim_words: list[tuple[str, ...]] = []

    @property
    def provisional(self) -> bool:
        """Whether a provisional (paused, uncommitted) interruption is live."""
        return self._provisional

    @property
    def _sustain_window(self) -> float:
        return self._commit_secs if self._provisional_duck else self._vad_fallback_secs

    async def handle_user_turn_started(self):
        """Per-turn reset (pipecat >= 1.6 hook; ``reset`` is deprecated).
        ``_bot_speaking`` deliberately survives: it mirrors frame-derived
        transport state, not per-turn state — a barge-in turn starts
        precisely while the bot is still speaking."""
        self._vad_speech_since = None
        self._last_suppressed_at = None
        self._provisional = False
        self._interim_words = []
        await super().handle_user_turn_started()

    def _interim_confirms(self, text: str) -> str | None:
        """Why an interim transcript confirms the interruption, or None.

        Interims are cumulative and arrive for whatever audio the gate let
        through, including the bot's own echo on a phone leg; a word count
        alone is therefore no evidence of a caller. Two things together are:
        the caller audio gate + VAD currently report sustained speech
        (``_INTERIM_VAD_MIN_S``), and the interim's first ``min_words`` words
        did not change since the previous interim of this speech episode.
        """
        words = tuple(speech_words(text or ""))
        if len(words) < self._min_words:
            self._interim_words.append(words)
            return None
        head = words[: self._min_words]
        previous = self._interim_words[-1][: self._min_words] if self._interim_words else None
        self._interim_words.append(words)
        del self._interim_words[:-_INTERIM_HISTORY]
        sustained = self._sustained()
        if sustained is None or sustained < _INTERIM_VAD_MIN_S:
            return None
        if previous != head:
            return None
        return (
            f"interim transcript ({len(words)} words >= {self._min_words}, "
            f"stable over {_INTERIM_STABLE_UPDATES} updates, VAD {sustained:.1f}s)"
        )

    def _sustained(self) -> float | None:
        if self._vad_speech_since is None:
            return None
        return time.monotonic() - self._vad_speech_since

    async def _begin_provisional(self) -> None:
        if self._provisional:
            return
        self._provisional = True
        logger.info("barge-in provisional: pausing reply")
        await _call_hook(self._on_provisional_change, "provisional", self._sustained())
        await _call_hook(self._on_provisional, True)

    async def _end_provisional(self, resumed: bool, sustained: float | None = None) -> None:
        """Leave the provisional state. ``resumed`` means the reply continues
        (no commit); on a commit the interruption frames clear the duck."""
        if not self._provisional:
            return
        self._provisional = False
        if resumed:
            logger.info("barge-in provisional ended without commit: resuming reply")
            await _call_hook(self._on_provisional_change, "resumed", sustained)
            await _call_hook(self._on_provisional, False)

    def _background_suspect(self, arbiter: str) -> bool:
        """Whether the live gated speech reads as background and, when
        enforcing, must NOT confirm this interruption."""
        if self._speech_classifier is None:
            return False
        try:
            verdict = self._speech_classifier()
        except Exception:  # noqa: BLE001 — evidence must never block a turn
            logger.debug("barge-in speech classifier failed", exc_info=True)
            return False
        if verdict is None or not getattr(verdict, "suspect", False):
            return False
        self._last_suppressed_at = time.monotonic()
        logger.info(
            "barge-in %s as background speech (%s, delta %s dB)",
            "suppressed" if self._enforce_background else "flagged",
            arbiter, getattr(verdict, "delta_db", None),
        )
        if self._on_suppressed is not None:
            try:
                self._on_suppressed(
                    arbiter, self._sustained(), verdict, self._enforce_background
                )
            except Exception:  # noqa: BLE001 — evidence must never block a turn
                logger.debug("barge-in suppression hook failed", exc_info=True)
        return self._enforce_background

    async def _confirm(self, why: str) -> ProcessFrameResult:
        sustained = self._sustained()
        self._vad_speech_since = None
        self._last_suppressed_at = None
        was_provisional = self._provisional
        await self._end_provisional(resumed=False)
        logger.info("barge-in confirmed (%s)", why)
        if self._on_confirmed is not None:
            try:
                self._on_confirmed(why, sustained)
            except Exception:  # noqa: BLE001 — evidence must never block a turn
                logger.debug("barge-in confirmation hook failed", exc_info=True)
        if was_provisional:
            logger.debug("barge-in committed after provisional pause")
        await self.trigger_user_turn_started()
        return ProcessFrameResult.STOP

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        """Start the user turn per the policy above.

        Args:
            frame: The frame to be analyzed.

        Returns:
            STOP when the user turn started, CONTINUE otherwise.
        """
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            self._interim_words = []
            if self._vad_speech_since is not None:
                # The caller began speaking over the bot's final syllables and
                # the gate held the turn; with the bot now quiet there is
                # nothing left to protect — open the turn immediately instead
                # of waiting for a transcript. (Whether the speech was the
                # caller's is the brain's transcript-time decision: a
                # background segment is held there, never answered.)
                return await self._confirm("bot stopped during gated speech")
            await self._end_provisional(resumed=True, sustained=None)
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            if not self._bot_speaking:
                await self.trigger_user_turn_started()
                return ProcessFrameResult.STOP
            if self._vad_speech_since is None:
                self._vad_speech_since = time.monotonic()
                self._last_suppressed_at = None
                self._interim_words = []     # stability is judged per speech episode
            if self._provisional_duck and not self._background_suspect_live_for_pause():
                await self._begin_provisional()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            # The speech was not sustained through the fallback window; a new
            # VAD start begins a fresh window. In hybrid mode the paused
            # reply resumes.
            sustained = self._sustained()
            self._vad_speech_since = None
            self._last_suppressed_at = None
            self._interim_words = []
            await self._end_provisional(resumed=True, sustained=sustained)
        elif isinstance(frame, InterimTranscriptionFrame):
            if self._bot_speaking:
                why = self._interim_confirms(frame.text or "")
                if why is not None:
                    if self._background_suspect("transcript"):
                        return ProcessFrameResult.CONTINUE
                    return await self._confirm(why)
        elif isinstance(frame, TranscriptionFrame):
            if self._bot_speaking:
                # A telephony recording notice is not the caller interrupting:
                # only the words left after stripping it count toward the gate.
                words = speech_word_count(frame.text or "")
                if words >= self._min_words:
                    if self._background_suspect("transcript"):
                        return ProcessFrameResult.CONTINUE
                    return await self._confirm(
                        f"transcript ({words} words >= {self._min_words})"
                    )

        window = self._sustain_window
        if (
            self._bot_speaking
            and window > 0
            and self._vad_speech_since is not None
            and time.monotonic() - self._vad_speech_since >= window
        ):
            # Checked on every frame (audio arrives every ~20 ms), so the
            # fallback fires within a frame of its deadline without a timer
            # task to manage.
            if self._last_suppressed_at is not None and (
                time.monotonic() - self._last_suppressed_at < _SUPPRESSION_RECHECK_S
            ):
                # Already judged background for this episode; re-evaluate at
                # a bounded cadence in case the caller starts talking over it.
                return ProcessFrameResult.CONTINUE
            if self._background_suspect("sustained_vad"):
                return ProcessFrameResult.CONTINUE
            return await self._confirm(
                f"sustained VAD speech >= {window:.1f}s"
            )

        return ProcessFrameResult.CONTINUE

    def _background_suspect_live_for_pause(self) -> bool:
        """Whether to withhold even the provisional pause: only when the
        guard is ENFORCED and the live segment already reads as background.
        (At VAD start the live segment is usually too short to judge, so
        this rarely fires; it exists so an enforcing tenant's background
        speech does not pause the bot either.)"""
        if not self._enforce_background or self._speech_classifier is None:
            return False
        try:
            verdict = self._speech_classifier()
        except Exception:  # noqa: BLE001
            return False
        return bool(verdict is not None and getattr(verdict, "suspect", False))
