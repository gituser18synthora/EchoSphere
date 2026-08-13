"""Asyncio helpers for the orchestration layer."""

import asyncio


def _reap(task: asyncio.Task) -> None:
    """Retrieve an abandoned task's outcome so it never warns at GC."""
    if task.cancelled():
        return
    task.exception()


async def to_thread_abandonable(fn, /, *args, **kwargs):
    """``asyncio.to_thread`` that cancellation can abandon immediately.

    A task awaiting a plain ``to_thread`` future cannot be cancelled until the
    thread finishes: executor futures refuse cancellation once running, so the
    CancelledError is deferred to thread completion. On the voice turn path
    that meant a barge-in's ``cancel_task`` stalled for the remainder of a
    tool's HTTP timeout (observed live as ``_handle_turn: timed out waiting
    for task to cancel``, up to +1s before the next reply could dispatch).

    Wrapping the thread in its own task behind ``asyncio.shield`` keeps the
    thread running to completion (its work is simply discarded) while the
    awaiting coroutine honours cancellation at once.
    """
    task = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(_reap)
        raise
