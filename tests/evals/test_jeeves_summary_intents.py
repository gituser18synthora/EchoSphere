"""Opt-in semantic regression against the bot's actual configured analyst.

ECHOSPHERE_RUN_SUMMARY_EVAL=1 env/bin/python -m pytest tests/evals/test_jeeves_summary_intents.py -q

Uses synthetic fixtures only. Reads local bot configuration and calls its LLM
through the same focused evidence request the post-call worker sends
(shared.post_call.analyzer.extract_evidence_fields); does not create
conversations, persist memories or change bot settings.

Two fixture sets: tests/fixtures/jeeves_summary_intents.json (one or two
customer lines per case) and tests/fixtures/jeeves_summary_calls.json (whole
simulated Jeeves calls — the bot's real v21 flow with the customer's remark
made in passing, plus the final workflow slots — where the combined
narrative prompt used to miss in-flow statements and invent dates).
"""

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from jeeves.summary_intents import BOT_ID, INTENT_SUMMARY_FIELDS, enhance_summary_fields
from shared.orchestration.goal_engine import compile_goal_policy
from shared.post_call.analyzer import build_analysis_llm, extract_evidence_fields
from shared.post_call.structured import merge_structured_fields

pytestmark = pytest.mark.skipif(os.getenv("ECHOSPHERE_RUN_SUMMARY_EVAL") != "1", reason="Opt-in real-provider semantic evaluation")

FIXTURES = Path(__file__).parents[1] / "fixtures"
DEFAULT_REFERENCE = datetime(2026, 10, 27, 10, tzinfo=ZoneInfo("Asia/Kolkata"))


async def _policy_and_config():
    from shared.bot_config import resolve_bot_config
    from shared.config import get_settings

    assert get_settings().mysql_host in {"127.0.0.1", "localhost"}, "Evaluation requires local bot configuration"
    config = await resolve_bot_config(BOT_ID, require_published=False)
    assert config.bot_id == BOT_ID
    policy = compile_goal_policy(
        {**config.goal_policy, "summaryFields": enhance_summary_fields(config.goal_policy.get("summaryFields", []))},
        bot_name=config.bot_name, system_prompt=config.system_prompt, intents=config.intents,
    )
    return policy, config


async def _evaluate(case, *, policy, config, turns, slots, reference, details):
    # The durable worker retries unavailable requests too. Retry only
    # transport/parse failures, never semantic mismatches.
    for _ in range(2):
        proposed, evidence, _ = await extract_evidence_fields(
            build_analysis_llm(config), policy=policy, turns=turns, reference=reference, timeout_seconds=90,
        )
        if proposed is not None:
            break
    if proposed is None:
        return [f"{case['id']}: evidence request unavailable"]
    fields, _ = merge_structured_fields(policy, slots, proposed, evidence=evidence, turns=turns, reference=reference)
    details[case["id"]] = {"fields": fields, "raw": proposed, "evidence": evidence}
    expected = {f["name"]: None for f in INTENT_SUMMARY_FIELDS}
    expected.update(case["expected"])
    errors = []
    for name, value in expected.items():
        actual = fields[name]
        # Address punctuation around a copied span carries no meaning.
        if name == "updated_address" and isinstance(actual, str):
            actual = actual.strip(" .,।")
        allowed = case.get("allowed", {}).get(name, [value])
        if isinstance(value, str) and value.startswith("contains:"):
            ok = isinstance(actual, str) and value[9:].lower() in actual.lower()
        else:
            ok = actual in allowed
        if not ok:
            errors.append(f"{case['id']}.{name}: expected {allowed if len(allowed) > 1 else value!r}, got {actual!r}")
    return errors


async def _run(cases, *, build_turns, slots_of, reference_of, report_env):
    policy, config = await _policy_and_config()
    details = {}
    limit = asyncio.Semaphore(3)

    async def one(case):
        async with limit:
            return await _evaluate(case, policy=policy, config=config, turns=build_turns(case),
                                   slots=slots_of(case), reference=reference_of(case), details=details)

    results = await asyncio.gather(*(one(case) for case in cases))
    errors = [error for result in results for error in result]
    if report := os.getenv(report_env):
        Path(report).write_text(json.dumps({"errors": errors, "cases": details}, ensure_ascii=False, indent=2) + "\n")
    assert not errors, "\n".join(errors)


async def test_multilingual_customer_intent_semantics():
    cases = json.loads((FIXTURES / "jeeves_summary_intents.json").read_text())
    await _run(
        cases,
        build_turns=lambda case: (
            [{"role": "bot", "text": "I am calling about your Airtel device uninstallation and pickup."}]
            + case.get("turns", [{"role": "user", "text": case.get("customer", "")}])
        ),
        slots_of=lambda case: {},
        reference_of=lambda case: datetime.fromisoformat(case["reference"]) if case.get("reference") else DEFAULT_REFERENCE,
        report_env="ECHOSPHERE_SUMMARY_EVAL_REPORT",
    )


async def test_realistic_call_transcripts():
    """Whole simulated calls: the remark is made mid-flow and the workflow's
    final slots take precedence for the eight slot-derived fields. Relative
    dates in these transcripts are relative to the run day (they were spoken
    'today'), so the expected ISO date is computed at run time."""
    cases = json.loads((FIXTURES / "jeeves_summary_calls.json").read_text())
    today = datetime.now(ZoneInfo("Asia/Kolkata"))
    for case in cases:
        for name, value in list(case["expected"].items()):
            if value == "yesterday":
                case["expected"][name] = (today.date() - timedelta(days=1)).isoformat()
    await _run(
        cases,
        build_turns=lambda case: case["turns"],
        slots_of=lambda case: case.get("slots") or {},
        reference_of=lambda case: today,
        report_env="ECHOSPHERE_SUMMARY_CALLS_EVAL_REPORT",
    )
