"""Publish a validated Haier configuration repair through DEMO's existing API.

Dry-run by default. No server files, services, calls or historical records are
changed. Requires the exact reviewed snapshot and hash-bound replay evidence.
The destination and bot are fixed to prevent use against another environment.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path


BASE = "https://echosphere.edas.tech/api/v1"
BOT = "bot_450a4f7b4832"
TENANT = "tn_fb39b67f5361"
REQUIRED_REPLAYS = {
    "cv_fadadc7782d5", "cv_479df7320d2d",
    "same_facts_dissatisfied_confirmed_problem",
    "contact_yes_problem_pending", "contact_no_service_yes", "contact_no_service_no",
    "mixed_no_but_call_received", "visit_but_dissatisfied", "resolved_by_haier",
    "resolved_without_haier", "uncertain_contact", "uncertain_resolution",
    "completed_installation_by_haier", "completed_installation_by_other",
    "completed_installation_by_unknown", "installation_pending",
    "late_future_service", "repeated_negation_problem_pending",
}
SUMMARY_REPLAYS = {
    "cv_fadadc7782d5", "cv_479df7320d2d", "same_facts_dissatisfied_confirmed_problem",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def main(args):
    snapshot = json.loads(args.snapshot.read_text())
    raw_candidate = args.candidate.read_bytes()
    candidate = json.loads(raw_candidate)
    evidence = json.loads(args.evidence.read_text())
    candidate_hash = hashlib.sha256(raw_candidate).hexdigest()
    if evidence.get("candidate_sha256") != candidate_hash:
        raise SystemExit("Replay evidence does not match this candidate")
    results = evidence.get("results") or []
    if not results or any(row.get("failures") for row in results):
        raise SystemExit("Replay evidence contains missing or failed scenarios")
    names = {row["name"] for row in results}
    if not REQUIRED_REPLAYS <= names:
        raise SystemExit("The complete related regression suite is required, including both calls")
    for name in SUMMARY_REPLAYS:
        row = next(row for row in results if row["name"] == name)
        checked = row.get("summary_validation") or {}
        if checked.get("error") or not checked.get("analysis"):
            raise SystemExit(f"Actual post-call summary validation is required: {name}")
    if snapshot["bot"]["id"] != BOT or snapshot["bot"]["tenant_id"] != TENANT:
        raise SystemExit("Wrong source bot/tenant")
    previous = snapshot["workflows"][0]
    workflow = candidate["workflow"]
    if workflow["id"] != previous["id"] or workflow["version"] != previous["version"] + 1:
        raise SystemExit("Candidate must be the next revision of the reviewed workflow")
    system = next(p for p in snapshot["prompts"] if p["type"] == "system")
    prior_system = snapshot.get("resolved_config", {}).get("system_prompt")
    if prior_system is None:
        prior_version = next(v for v in snapshot["prompt_versions"]
                             if v["prompt_id"] == system["id"]
                             and v["version"] == system["published_version"])
        prior_system = prior_version.get("compiled_prompt") or prior_version.get("full_prompt") or ""
    prompt_changed = candidate["system_prompt"] != prior_system
    policy_changed = candidate["goal_policy"] != snapshot["voice_settings"]["goal_policy"]

    os.environ["ECHOSPHERE_API"] = BASE
    import _common

    if _common.BASE.rstrip("/") != BASE:
        raise SystemExit("Refusing unexpected API destination before authentication")

    with closing(_common.client(prefer_tenant_admin=False)) as c:
        if str(c.base_url).rstrip("/") != BASE:
            raise SystemExit("Refusing unexpected API destination")

        def request(method, path, body=None):
            response = c.request(method, path, json=body) if body is not None else c.request(method, path)
            if response.status_code >= 300:
                raise SystemExit(f"DEMO API {method} {path}: HTTP {response.status_code}: {response.text[:500]}")
            return response.json()["data"]

        bot = request("GET", f"/bots/{BOT}")
        current = request("GET", f"/bots/{BOT}/workflow")
        prompts = request("GET", f"/bots/{BOT}/prompts")
        voice = request("GET", f"/bots/{BOT}/voice-settings")
        releases = request("GET", f"/bots/{BOT}/releases")
        current_prompt = next(p for p in prompts if p["id"] == system["id"])
        if bot["tenantId"] != TENANT or bot["liveVersion"] != snapshot["bot"]["live_version"]:
            raise SystemExit("DEMO release changed since review")
        if current["id"] != previous["id"] or current["version"] != previous["version"]:
            raise SystemExit("DEMO workflow changed since review")
        for key in ("nodes", "edges"):
            if digest(current[key]) != digest(previous[key]):
                raise SystemExit(f"DEMO workflow {key} changed since review")
        if current_prompt["activeVersion"] != system["active_version"] or current_prompt["publishedVersion"] != system["published_version"]:
            raise SystemExit("DEMO prompt changed since review")
        if digest(voice["goalPolicy"]) != digest(snapshot["voice_settings"]["goal_policy"]):
            raise SystemExit("DEMO summary policy changed since review")
        if any(r["stage"] in ("draft", "review", "approved") or r["version"] == args.release for r in releases):
            raise SystemExit("An open release or duplicate target version needs review")
        record = {
            "destination": BASE, "bot_id": BOT, "candidate_sha256": candidate_hash,
            "previous_release": bot["liveVersion"], "new_release": args.release,
            "workflow_version": workflow["version"],
            "system_prompt_version": system["active_version"] + 1 if prompt_changed else system["published_version"],
            "greeting_prompt_version": next(p["published_version"] for p in snapshot["prompts"] if p["type"] == "greeting"),
            "summary_policy_label": "v4", "passing_replays": len(results),
            "manual_risks": evidence.get("manual_risks") or [],
            "changed_components": ["workflow"] + (["system_prompt"] if prompt_changed else []) + (["summary_policy"] if policy_changed else []),
        }
        print(json.dumps(record, indent=2))
        if not args.apply:
            print("Dry-run: no configuration writes sent")
            return

        # The API snapshots each workflow revision and records all changes in
        # its normal audit log. Nothing is copied to the remote filesystem.
        saved = request("PUT", f"/bots/{BOT}/workflow", {
            "name": workflow["name"], "nodes": workflow["nodes"],
            "edges": workflow["edges"], "status": "approved",
        })
        if saved["id"] != workflow["id"] or saved["version"] != workflow["version"]:
            raise SystemExit("Unexpected saved workflow revision; publication stopped")
        # JSON object keys may be reordered by storage. Authored priority must
        # live in arrays; verify the returned definition before publication.
        if any(saved[key] != workflow[key] for key in ("nodes", "edges")):
            raise SystemExit("Persisted workflow differs from the validated candidate")
        if prompt_changed:
            draft = request("POST", f"/prompts/{system['id']}/versions", {
                "promptMode": "full", "fullPrompt": candidate["system_prompt"],
                "submitForApproval": True,
                "note": "Haier contact/status separation and confirmed pending-service handling; local real-brain regression validated.",
            })
            if draft["activeVersion"] != record["system_prompt_version"]:
                raise SystemExit("Unexpected prompt version; publication stopped")
            request("PATCH", f"/prompts/{system['id']}", {"state": "approved"})
            request("PATCH", f"/prompts/{system['id']}", {"state": "published"})
        if policy_changed:
            request("PUT", f"/bots/{BOT}/voice-settings", {"goalPolicy": candidate["goal_policy"]})
        component_diff = [{"component": "workflow", "before": previous["version"], "after": workflow["version"]}]
        if prompt_changed:
            component_diff.append({"component": "system_prompt", "before": system["published_version"], "after": record["system_prompt_version"]})
        if policy_changed:
            component_diff.append({"component": "summary_policy", "before": "previous", "after": "v4"})
        release = request("POST", f"/bots/{BOT}/releases", {
            "version": args.release,
            "notes": (
                f"Haier workflow v{workflow['version']}, system prompt v{record['system_prompt_version']}, "
                "greeting unchanged v7, summary policy v4. Fix cv_fadadc7782d5 contact negation, "
                "cv_479df7320d2d pending-problem apology/direct proposal, terminal-flow summary. "
                "Contact capture priority uses ordered arrays and survives database JSON key reordering. "
                f"Validated {len(results)} local real-brain replays plus Haier unit regressions. "
                "Completed-installation, voice and turn-taking preserved; no code deployment. "
                "Controlled testing only: review analyst wording on ambiguous incomplete replies; "
                "unknown workflow status must remain unconfirmed."
            ),
            "diff": component_diff,
        })
        approved = request("PATCH", f"/releases/{release['id']}", {"stage": "approved", "note": "Both calls and related local real-decision regressions reviewed."})
        body = {"stage": "published", "note": "User-authorized DEMO configuration-only release."}
        blocked = [item for item in approved["checklist"] if not item["ok"]]
        allowed = {"c1", "c3"}
        if any(item["id"] not in allowed for item in blocked):
            raise SystemExit("Unexpected publish-gate failure; publication stopped")
        if blocked:
            # This fixed-script POC has no KB or stored Studio scenarios.
            # External actual-brain regression evidence is hash-bound above;
            # record that limitation explicitly instead of fabricating runs.
            body["overrideReason"] = (
                f"Haier fixed-workflow DEMO POC: {len(results)} local actual ConversationBrain/LLM "
                f"replays passed (candidate SHA256 {candidate_hash}); dedicated workflow/summary "
                "regressions passed. No indexed KB is used by these fixed branches and no Studio "
                "scenarios exist. c1/c3 remain transparently unfulfilled; controlled testing only."
            )
        published = request("PATCH", f"/releases/{release['id']}", body)
        record.update({"release_id": release["id"], "stage": published["stage"],
                       "publish_checklist": published["checklist"], "override_reason": body.get("overrideReason")})
        if args.output:
            args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
            args.output.chmod(0o600)
        print(json.dumps({"release_id": release["id"], "stage": published["stage"], "version": published["version"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--release", default="v0.1.21")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--apply", action="store_true")
    main(parser.parse_args())
