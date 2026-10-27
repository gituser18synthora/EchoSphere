"""Speculative reply generation (Phase 4, llm_settings.speculative_reply).

On a turn that will most likely be answered by the reply LLM, the brain starts
that generation at dispatch, in parallel with the decision, instead of after
it. Tokens are buffered here and never reach TTS on their own: the brain
releases them only when the decision confirmed what the speculation assumed
(the route, an in-scope turn) AND the reply request it then builds — system
prompt, messages, temperature, token cap — is identical to the speculative
one. Anything else cancels the speculation and the turn generates as before.

The request is made exactly as ``_stream_llm_tokens`` makes it, so a kept
speculation is the same request started earlier; :meth:`replay` hands the
buffered chunks and then the live remainder to that consumer, which applies
its first-token deadline, placeholder filter, character cap and guardrail
hold unchanged.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncIterator, Callable


def request_key(messages: list[dict], system: str, temperature: float, max_tokens: int) -> str:
    """One comparable string per reply request."""
    return json.dumps(
        [system, messages, temperature, max_tokens], ensure_ascii=False, sort_keys=True,
    )


class SpeculativeReply:
    """One buffered reply-LLM stream started before the decision returned."""

    def __init__(
        self,
        llm,
        *,
        messages: list[dict],
        system: str,
        temperature: float,
        max_tokens: int,
        assumed_route: str,
        create_task: Callable,
    ) -> None:
        self.key = request_key(messages, system, temperature, max_tokens)
        self.assumed_route = assumed_route
        self.started_at = time.monotonic()
        self.first_token_at: float | None = None
        self.chunks: list[str] = []
        self.done = False
        self.error: BaseException | None = None
        self._changed = asyncio.Event()
        self._task = create_task(self._run(llm, list(messages), system, temperature, max_tokens))

    async def _run(self, llm, messages, system, temperature, max_tokens) -> None:
        try:
            async for chunk in llm.stream(
                messages, system=system, temperature=temperature, max_tokens=max_tokens,
            ):
                if self.first_token_at is None:
                    self.first_token_at = time.monotonic()
                self.chunks.append(chunk)
                self._changed.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — re-raised to the consumer by replay()
            self.error = exc
        finally:
            self.done = True
            self._changed.set()

    def matches(self, messages: list[dict], system: str, temperature: float, max_tokens: int) -> bool:
        return self.key == request_key(messages, system, temperature, max_tokens)

    @property
    def generated_chars(self) -> int:
        return sum(len(chunk) for chunk in self.chunks)

    async def replay(self) -> AsyncIterator[str]:
        """The buffered chunks, then the rest of the stream as it arrives; a
        provider failure surfaces where the original stream would raise it."""
        index = 0
        try:
            while True:
                while index < len(self.chunks):
                    yield self.chunks[index]
                    index += 1
                if self.done:
                    if self.error is not None:
                        raise self.error
                    return
                self._changed.clear()
                if index < len(self.chunks) or self.done:
                    continue
                await self._changed.wait()
        finally:
            # A consumer that stops early (first-token deadline, guardrail
            # block, barge-in) must not leave the request running.
            self.cancel()

    def cancel(self) -> None:
        if not self._task.done():
            self._task.cancel()
