"""Stage 08 — MDND conversation flow v3 + structured call summary (LOCAL).

Applies ONLY to the dedicated MDND bot (state key BOT_MDND):

  1. Workflow "Zepto MDND concern journey" rebuilt from
     06_single_bots.build_mdnd_workflow() (v3: reached+called asked together,
     wide handover-recipient vocabulary, guard-name follow-up gated on a guard
     handover, new CX-support-call question, confirm/correct loop that
     re-asks only cleared fields).
  2. A new published system-prompt version (MDND_SYSTEM) describing the four
     enquiries and the confirmation behaviour.
  3. goalPolicy.summaryFields — the structured post-call summary
     (call_customer / reach_customer_location / hand_over_product /
     hand_over_to / call_cx) derived from the final workflow slots.
  4. Runtime-context test payload without `other_deduction` (MDND-only line).

Greeting, intents, runtime context, KB and channel are NOT touched.
Idempotent: re-running PUTs the same workflow/policy and adds a prompt
version only when the text changed.

Run: env/bin/python zepto/setup/08_mdnd_flow_v3.py
"""

import importlib.util
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("stage06", HERE / "06_single_bots.py")
stage06 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stage06)


def main() -> None:
    c = stage06.client()
    state = stage06.load_state()
    bot_id = state["BOT_MDND"]
    spec = next(s for s in stage06.CONCERNS if s["state_key"] == "BOT_MDND")
    print(f"===== {spec['bot_name']} ({bot_id}) =====")

    # 1. workflow
    nodes, edges = stage06.build_mdnd_workflow()
    wf = stage06.check(c.put(f"/bots/{bot_id}/workflow", json={
        "name": spec["workflow_name"], "nodes": nodes, "edges": edges,
        "status": "approved",
    }), f"workflow v3 ({len(nodes)} nodes, {len(edges)} edges)")
    print(f"     id={wf['id']} version={wf.get('version')} status={wf.get('status')}")
    if wf.get("issues"):
        print("     issues:", json.dumps(wf["issues"], ensure_ascii=False)[:800])
    state["BOT_MDND_WF"] = wf["id"]

    # 2. system prompt (new version only when the wording changed)
    prompts = stage06.check(c.get(f"/bots/{bot_id}/prompts"), "list prompts")
    system = next(p for p in prompts if p["type"] == "system")
    active_no = system.get("publishedVersion") or system.get("activeVersion")
    active = next((v for v in system.get("versions") or []
                   if v.get("version") == active_no), None)
    active_text = (active or {}).get("fullPrompt") or ""
    if active_text.strip() != stage06.MDND_SYSTEM.strip():
        stage06.check(c.post(f"/prompts/{system['id']}/versions", json={
            "promptMode": "full", "fullPrompt": stage06.MDND_SYSTEM,
            "note": "MDND flow v3: combined reached/called ask, recipient "
                    "vocabulary, CX-support enquiry, confirm/correct loop",
        }), "system prompt version")
        stage06.check(c.patch(f"/prompts/{system['id']}", json={"state": "approved"}),
                      "approve system")
        stage06.check(c.patch(f"/prompts/{system['id']}", json={"state": "published"}),
                      "publish system")
    else:
        print("ok   system prompt already current")

    # 3. structured summary fields
    current = stage06.check(c.get(f"/bots/{bot_id}/voice-settings"),
                            "read voice settings")
    goal_policy = dict(current.get("goalPolicy") or {})
    goal_policy["summaryFields"] = stage06.MDND_SUMMARY_FIELDS
    stage06.check(c.put(f"/bots/{bot_id}/voice-settings",
                        json={"goalPolicy": goal_policy}),
                  f"goalPolicy.summaryFields ({len(stage06.MDND_SUMMARY_FIELDS)})")

    # 4. runtime context (Testing-Studio payload): MDND-only ticket facts —
    #    the `other_deduction` value is deliberately absent on this line now.
    stage06.check(c.put(f"/bots/{bot_id}/runtime-context", json={
        "name": "Partner & support facts",
        "sourceMode": "manual",
        "fields": [],
        "allowAdditional": True,
        "testPayload": {
            "partner_name": "Ravi Kumar",
            "partner_id": "ZP-88231",
            "partner_city": "Mumbai",
            "callback_window": "within 24 to 48 hours",
            "support_action": ("Zepto Support records the concern details "
                               "and the concern team reviews the deduction "
                               "and connects with the partner"),
            **spec["context_extra"],
        },
        "missingValuePolicy": ("Never guess a deduction amount, date, policy "
                               "rule, ticket number or callback time. If a "
                               "value is not in the context or a system "
                               "result from this call, say the support team "
                               "will confirm it after reviewing the "
                               "concern."),
        "domainPolicy": "generic",
    }), "runtime context (MDND-only ticket facts)")

    stage06.save_state(state)
    print("\ndone:", json.dumps({"bot": bot_id, "workflow": wf["id"]}))


if __name__ == "__main__":
    sys.exit(main())
