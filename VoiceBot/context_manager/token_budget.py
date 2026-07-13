"""
LLM messages from Redis turn dicts (source of truth) or in-memory fallback.
"""

import logging

from orchestrator.call_state import CallState

logger = logging.getLogger(__name__)


def estimate_tokens(text: str) -> int:
    return int(len(str(text).split()) * 1.3)


def build_llm_messages_from_redis(
    system_prompt: str,
    redis_turns: list[dict],
    current_text: str,
    knowledge_content: str | None,
    running_summary: str | None,
    context_window_tokens: int,
) -> list[dict]:
    budget = context_window_tokens

    budget -= estimate_tokens(system_prompt)

    if knowledge_content:
        current_content = (
            f"Context: {knowledge_content}\n\nUser: {current_text}"
        )
    else:
        current_content = current_text
    budget -= estimate_tokens(current_content)

    if budget <= 0:
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": current_content},
        ]

    history: list[dict] = []

    total_turn_tokens = sum(
        estimate_tokens(t.get("content", ""))
        for t in redis_turns
    )

    if total_turn_tokens <= budget:
        # Full history fits — skip running summary (avoid duplicate context)
        for t in redis_turns:
            history.append({
                "role": t["role"],
                "content": t.get("content", ""),
            })
        budget -= total_turn_tokens
        logger.info(
            "[TokenBudget] ALL %s Redis turns included | %s tokens left (est.)",
            len(redis_turns),
            budget,
        )
    else:
        if running_summary:
            summary_text = f"EARLIER IN THIS CALL: {running_summary}"
            s_tokens = estimate_tokens(summary_text)
            if budget >= s_tokens:
                history.append({
                    "role": "system",
                    "content": summary_text,
                })
                budget -= s_tokens
        turns_to_add: list[dict] = []
        for t in reversed(redis_turns):
            t_tokens = estimate_tokens(t.get("content", ""))
            if budget >= t_tokens:
                turns_to_add.insert(0, {
                    "role": t["role"],
                    "content": t.get("content", ""),
                })
                budget -= t_tokens
            else:
                break
        trimmed = len(redis_turns) - len(turns_to_add)
        logger.warning(
            "[TokenBudget] Trimmed %s old turns | kept %s recent",
            trimmed,
            len(turns_to_add),
        )
        history.extend(turns_to_add)

    messages = [
        {"role": "system", "content": system_prompt},
        *history,
        {"role": "user", "content": current_content},
    ]

    logger.info(
        "[TokenBudget] Built %s messages | Redis turns=%s | budget=%s (est.)",
        len(messages),
        len(redis_turns),
        budget,
    )
    return messages


def build_llm_messages(
    call_state: CallState,
    current_text: str,
    knowledge_content: str | None,
    context_window_tokens: int,
) -> list[dict]:
    """
    Unit-test / offline helper: same as Redis path using call_state.turns.
    """
    redis_turns = [
        {"role": t.role, "content": t.content}
        for t in call_state.turns
    ]
    return build_llm_messages_from_redis(
        system_prompt=call_state.system_prompt,
        redis_turns=redis_turns,
        current_text=current_text,
        knowledge_content=knowledge_content,
        running_summary=call_state.running_summary,
        context_window_tokens=context_window_tokens,
    )
