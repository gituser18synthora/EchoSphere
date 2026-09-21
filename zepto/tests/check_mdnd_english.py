"""Read-only DB + real-model replay; all workflow checkpoints/tools are in memory.

Run from repo root with env/bin/python zepto/tests/check_mdnd_english.py.
The English patch is applied only to the in-memory definition, so this also
validates an environment before saving it. No phone calls or tickets created.
"""
import asyncio
import copy
import json
from pathlib import Path
import runpy
import sys
import time
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import text
from shared.bot_config import _load_config_sync
from shared.db.mysql import get_engine
from shared.providers.base import ProviderConfig
from shared.providers.factory import get_llm_provider
import shared.orchestration.workflow_engine as wfe

BOT = "bot_59a84478f155"
WF = "wf_7e4cf166c7bd"
CONTEXT = {"mdnd_deduction_amount": "500", "mdnd_deduction_date": "25 August", "mdnd_order_last4": "9456"}


async def replay(definition, turns, llm, language="en-IN"):
    engine = wfe.WorkflowEngine()
    engine._checkpointer = MemorySaver()
    results = []
    history = []
    with patch.object(wfe, "load_workflow_definition", return_value=definition):
        for utterance, signal in turns:
            start = time.monotonic()
            result = await engine.handle_turn_detailed(
                session_id="english-validation", tenant_id="tn_04250683f1b3", bot_id=BOT,
                workflow_name=WF, user_text=utterance, language=language, signal=signal,
                llm=llm, history=history, context_values=CONTEXT, mock_tool_results={})
            result["elapsed"] = round(time.monotonic() - start, 2)
            results.append(result)
            history.extend([{"role": "user", "content": utterance},
                            {"role": "assistant", "content": result["reply"]}])
    return results


async def main():
    with get_engine().connect() as db:
        row = dict(db.execute(text("SELECT id,name,version,nodes,edges FROM workflows WHERE id=:id"), {"id": WF}).mappings().one())
    for key in ("nodes", "edges"):
        if isinstance(row[key], str):
            row[key] = json.loads(row[key])
    original = copy.deepcopy(row)
    row["nodes"] = runpy.run_path("zepto/setup/13_mdnd_english_extraction.py")["patched_nodes"](row["nodes"])
    cfg = _load_config_sync(BOT, True)
    assert not cfg.workflow_pins, "A pinned release needs a reviewed release update"
    llm = get_llm_provider(ProviderConfig(provider=cfg.llm["provider"], model=cfg.llm["model"],
        api_key_reference=cfg.llm.get("api_key_reference", "")))
    opener = ("Yes, I am delivery partner.", "affirm")
    complaints = [
        "I have delivered the order to correct customer but still the amount has been deducted under MDND.",
        "I have given the correct product to the customer. But still the amount has been deducted.",
        "I delivered the order to correct customer but still the amount work has been deducted. Under NBND.",
    ]
    for complaint in complaints:
        rs = await replay(row, [opener, (complaint, "complaint"),
            ("Yes, I reached the customer's location and called the customer before delivery.", "affirm"),
            ("No, I did not receive any call from CX support.", "refusal")], llm)
        assert rs[1]["trace"][-1] == "n_ask_reached_called", rs[1]
        assert rs[0]["trace"][-1] == "n_ask_issue_desc", rs[0]
        assert rs[1]["slots"].get("m_handover_recipient") == "customer (direct)", rs[1]
        assert rs[1]["slots"]["reached_location"] == "unknown", rs[1]
        assert rs[2]["trace"][-1] == "n_ask_cx", rs[2]
        assert rs[3]["trace"][-1] == "n_hub_verify", rs[3]
        assert rs[3]["slots"]["cx_support_called"] == "no", rs[3]
        print(json.dumps({"case": complaint, "nodes": [r["trace"][-1] for r in rs],
                          "seconds": [r["elapsed"] for r in rs], "passed": True}), flush=True)
    for case, utterance, expected in [
        ("complaint_only", "My amount has been deducted under MDND.", {}),
        ("instruction_only", "The customer told me to give the order to the guard.", {}),
        ("still_with_partner", "The customer told me to give it to the guard, but I still have the order.", {"delivery_handoff": "other"}),
        ("clear_negatives", "I never called the customer and did not go to their location. I returned the order to the store. No call from CX support.",
         {"customer_called": "no", "reached_location": "no", "delivery_handoff": "other", "cx_support_called": "no"}),
    ]:
        r = (await replay(row, [opener, (utterance, "complaint")], llm))[-1]
        for field in ("customer_called", "reached_location", "delivery_handoff", "cx_support_called"):
            assert r["slots"].get(field, "unknown") == expected.get(field, "unknown"), (case, r)
        assert r["trace"][-1] != "n_ask_issue_desc", (case, r)
        print(json.dumps({"case": case, "passed": True, "seconds": r["elapsed"]}), flush=True)
    # Compare exact Hindi routing, slots and replies, before/after the patch.
    for turns in [
        [("Haan boliye", "affirm"), ("Maine product customer ko deliver kar diya phir bhi MDND mark hua hai", "complaint"),
         ("Haan customer ki location par gaya tha aur call kiya tha", "affirm"), ("Nahi CX support se call nahi aaya", "refusal")],
        [("हाँ बोलिए", "affirm"), ("मैंने customer को call किया और उनके घर जाकर उनकी माँ को order दिया था", "complaint")],
    ]:
        never_called = AsyncMock()
        before = await replay(original, turns, never_called, "hi-IN")
        after = await replay(row, turns, never_called, "hi-IN")
        for left, right in zip(before, after):
            for key in ("reply", "slots", "trace", "done", "offScript", "status"):
                assert left.get(key) == right.get(key), (key, left, right)
        never_called.generate.assert_not_awaited()
    print("PASS: real-model English cases; Hindi before/after replies, slots and routes identical; no Hindi extraction calls.")


if __name__ == "__main__":
    asyncio.run(main())
