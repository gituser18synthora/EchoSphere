"""Identity of a single STT final event, for idempotency and billing.

Both turn-taking and billing need to answer the same question: *is this final
the same event I already handled, or a new one?* Getting it wrong is expensive
in both directions — collapse distinct finals and the bot stops answering;
treat a replay as new and the caller is billed twice and answered twice.

The naive key is the provider's own request id. For Sarvam's realtime socket
that is **wrong**: ``request_id`` identifies the CONNECTION, not the utterance.
Observed on a live call (one connection, three consecutive finals)::

    data=SpeechToTextTranscriptionData(request_id='20260729_e72b14fb-…',
        transcript='ಆಯ್ತು', metrics=TranscriptionMetrics(audio_duration=…))
    data=SpeechToTextTranscriptionData(request_id='20260729_e72b14fb-…',
        transcript='ಖಂಡಿತ.', …)
    data=SpeechToTextTranscriptionData(request_id='20260729_e72b14fb-…',
        transcript='मुझे धर्मेश से बात करना है।',
        metrics=TranscriptionMetrics(audio_duration=2.912,
                                     processing_latency=0.17774629592895508))

Deduplicating on that id alone billed one utterance per connection and would
have silenced the bot after the first — a 17-turn call recorded
``stt_requests=1``.

Nor can the frame timestamp be used: pipecat stamps it with ``time_now_iso8601()``
at the moment the frame is built, so a genuinely replayed provider message gets
a *fresh* timestamp and would not be recognised as a replay at all.

What is both stable across a replay and distinct between real finals is the
provider's own measured payload: the connection id, the transcript, and the
metrics it reported for that segment — ``processing_latency`` in particular is a
high-precision measurement no two segments realistically share. Absent any
provider payload (mock/REST paths) the frame timestamp plus text is the best
available identity, which is correct there because those paths build one frame
per segment locally.
"""


def _payload(frame) -> dict | None:
    """The provider-shaped result dict carried on a TranscriptionFrame."""
    result = getattr(frame, "result", None)
    if not isinstance(result, dict):
        return None
    data = result.get("data")
    return data if isinstance(data, dict) else result


def final_event_key(frame, text: str) -> str | None:
    """A replay-stable, per-segment identity for one STT final.

    Returns None when nothing identifying is available, in which case callers
    must treat the event as new (never silently drop a real utterance).
    """
    data = _payload(frame)
    if data is not None:
        connection = str(data.get("request_id") or "")
        # Deepgram Flux: TurnInfo carries an explicit per-connection turn
        # counter — the strongest possible identity. A replayed EndOfTurn for
        # the same turn cannot differ in (request_id, turn_index, transcript).
        turn_index = data.get("turn_index")
        if connection and turn_index is not None:
            return f"turn:{connection}|{turn_index}|{text}"
        metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
        duration = metrics.get("audio_duration")
        latency = metrics.get("processing_latency")
        if connection and (duration is not None or latency is not None):
            # Connection + what the provider measured for THIS segment.
            return f"seg:{connection}|{text}|{duration}|{latency}"
        if connection and text:
            # No metrics on this shape: fall back to connection + text, which
            # still separates distinct utterances within a connection.
            timestamp = getattr(frame, "timestamp", None) or ""
            return f"seg:{connection}|{text}|{timestamp}"
    timestamp = getattr(frame, "timestamp", None)
    if timestamp:
        return f"ts:{timestamp}|{text}"
    return None


def segment_audio_seconds(frame) -> float | None:
    """Billable audio duration the provider reported for THIS final segment.

    Sarvam documents ``metrics.audio_duration`` as "duration of processed
    audio in seconds" and reports it per response, so a call's billable STT
    audio is the SUM over its finals — not any single one of them.
    """
    data = _payload(frame)
    if data is None:
        return None
    metrics = data.get("metrics")
    if not isinstance(metrics, dict):
        return None
    duration = metrics.get("audio_duration")
    try:
        if duration is None or isinstance(duration, bool):
            return None
        value = float(duration)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None
