"""Opt-in semantic regression against the bot's actual configured analyst.

ECHOSPHERE_RUN_SUMMARY_EVAL=1 env/bin/python -m pytest tests/evals/test_jeeves_summary_intents.py -q

Uses synthetic fixtures only. Reads local bot configuration and calls its LLM;
does not create conversations, persist memories or change bot settings.
"""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from jeeves.summary_intents import BOT_ID, INTENT_SUMMARY_FIELDS, enhance_summary_fields
from shared.orchestration.goal_engine import compile_goal_policy
from shared.post_call.analyzer import analyze_call, build_analysis_llm
from shared.post_call.structured import merge_structured_fields

pytestmark = pytest.mark.skipif(os.getenv("ECHOSPHERE_RUN_SUMMARY_EVAL") != "1", reason="Opt-in real-provider semantic evaluation")


async def test_multilingual_customer_intent_semantics():
    from shared.bot_config import resolve_bot_config
    from shared.config import get_settings

    assert get_settings().mysql_host in {"127.0.0.1", "localhost"}, "Evaluation requires local bot configuration"
    config = await resolve_bot_config(BOT_ID, require_published=False)
    assert config.bot_id == BOT_ID
    policy = compile_goal_policy(
        {**config.goal_policy, "summaryFields": enhance_summary_fields(config.goal_policy.get("summaryFields", []))},
        bot_name=config.bot_name, system_prompt=config.system_prompt, intents=config.intents,
    )
    cases = json.loads((Path(__file__).parents[1] / "fixtures/jeeves_summary_intents.json").read_text())
    details = {}
    limit = asyncio.Semaphore(3)
    async def evaluate(case):
        async with limit:
            turns = [{"role": "bot", "text": "I am calling about your Airtel device uninstallation and pickup."}]
            turns += case.get("turns", [{"role": "user", "text": case.get("customer", "")}])
            reference = (datetime.fromisoformat(case["reference"]) if case.get("reference") else
                         datetime(2026, 10, 27, 10, tzinfo=ZoneInfo("Asia/Kolkata")))
            # The durable worker retries unavailable analyses too. Retry only
            # transport/parse failures, never semantic mismatches.
            for _ in range(2):
                analysis, _ = await analyze_call(
                    build_analysis_llm(config), policy=policy, turns=turns, events=[], final_state={},
                    reference=reference, timeout_seconds=90,
                )
                if analysis is not None:
                    break
            if analysis is None:
                return [f"{case['id']}: analysis unavailable"]
            fields, _ = merge_structured_fields(policy, {}, analysis.structured_fields,
                                               evidence=analysis.structured_field_evidence, turns=turns)
            details[case["id"]] = {"fields": fields, "raw": analysis.structured_fields,
                                    "evidence": analysis.structured_field_evidence}
            expected = {f["name"]: None for f in INTENT_SUMMARY_FIELDS}
            expected.update(case["expected"])
            errors = []
            for name, value in expected.items():
                actual = fields[name]
                # Address punctuation around a copied span carries no meaning.
                if name == "updated_address" and isinstance(actual, str):
                    actual = actual.strip(" .,।")
                allowed = case.get("allowed", {}).get(name, [value])
                if actual not in allowed:
                    errors.append(f"{case['id']}.{name}: expected {value!r}, got {actual!r}")
            return errors

    results = await asyncio.gather(*(evaluate(case) for case in cases))
    errors = [error for result in results for error in result]
    if report := os.getenv("ECHOSPHERE_SUMMARY_EVAL_REPORT"):
        Path(report).write_text(json.dumps({"errors": errors, "cases": details}, ensure_ascii=False, indent=2) + "\n")
    assert not errors, "\n".join(errors)
