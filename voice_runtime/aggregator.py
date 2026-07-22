"""Sentence-aware LLM-token buffering for TTS.

Extends Pipecat's sentence aggregator with the realtime flush rules the voice
pipeline needs:

- sentence-ending punctuation flushes (inherited, NLTK-confirmed boundaries);
- sentences shorter than ``min_flush_chars`` are held and merged with the
  following sentence, avoiding one-word TTS requests;
- a buffer that grows past ``max_buffer_chars`` without a boundary is force
  flushed at the last whitespace so unbounded text cannot accumulate;
- end-of-response and pause-hint flushes are driven by the TTS service via
  ``flush()``.

Sentence order is preserved by construction — text is only ever released from
the front of the buffer.
"""

from collections.abc import AsyncIterator

from pipecat.utils.text.base_text_aggregator import Aggregation, AggregationType
from pipecat.utils.text.simple_text_aggregator import SimpleTextAggregator


class VoiceSentenceAggregator(SimpleTextAggregator):
    def __init__(self, *, min_flush_chars: int = 4, max_buffer_chars: int = 400, **kwargs):
        super().__init__(**kwargs)
        self._min_flush_chars = max(1, min_flush_chars)
        self._max_buffer_chars = max(40, max_buffer_chars)
        self._held = ""

    def _merge_held(self, text: str) -> str:
        combined = f"{self._held} {text}".strip() if self._held else text
        self._held = ""
        return combined

    async def aggregate(self, text: str) -> AsyncIterator[Aggregation]:
        if self._aggregation_type == AggregationType.TOKEN:
            async for aggregation in super().aggregate(text):
                yield aggregation
            return

        for char in text:
            self._text += char

            result = await self._check_sentence_with_lookahead(char)
            if result:
                combined = self._merge_held(result.text)
                if len(combined) >= self._min_flush_chars:
                    yield Aggregation(text=combined, type=result.type)
                else:
                    self._held = combined
                continue

            if len(self._text) >= self._max_buffer_chars:
                cut = self._text.rfind(" ")
                if cut <= 0:
                    cut = len(self._text)
                chunk = self._merge_held(self._text[:cut].strip(" "))
                self._text = self._text[cut:].lstrip(" ")
                self._needs_lookahead = False
                if chunk:
                    yield Aggregation(text=chunk, type=AggregationType.SENTENCE)

    async def flush(self) -> Aggregation | None:
        held = self._held  # capture first — super().flush() resets state via reset()
        remaining = await super().flush()
        parts = [part for part in (held, remaining.text if remaining else "") if part]
        self._held = ""
        if parts:
            return Aggregation(text=" ".join(parts).strip(), type=AggregationType.SENTENCE)
        return None

    async def handle_interruption(self):
        self._held = ""
        await super().handle_interruption()

    async def reset(self):
        self._held = ""
        await super().reset()
