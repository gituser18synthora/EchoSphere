"""Enable the MDND greeting retry through the local authoring API.

Preview by default; --apply replaces only the pre-workflow bridging section
and sets start_enquiries.fallbackBehavior to clarify. The existing runtime
then repeats the greeting's pending question for an unrecognized opening.
Workflow, greeting, matching samples and other prompt sections are preserved.

Run: env/bin/python zepto/setup/11_mdnd_opening_retry.py [--apply]
"""

import argparse
from contextlib import closing
import difflib
import importlib.util
import json
from pathlib import Path
import re
import tempfile
from urllib.parse import urlparse


_spec = importlib.util.spec_from_file_location(
    "mdnd_stage06", Path(__file__).with_name("06_single_bots.py"))
stage06 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stage06)

_OPENING_SECTION = re.compile(
    r"^## (?:Before the workflow starts[^\n]*|Unclear speech and retries)\n"
    r".*?(?=^#{1,2} |\Z)", re.M | re.S,
)


def patch_system(text: str) -> str:
    """Replace one opening subsection without changing surrounding rules."""
    if len(_OPENING_SECTION.findall(text)) != 1:
        raise ValueError("Expected exactly one MDND opening/retry section")
    replacement = _OPENING_SECTION.search(stage06.MDND_SYSTEM).group(0)
    return _OPENING_SECTION.sub(lambda _match: replacement, text, count=1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if urlparse(stage06.BASE).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("This patch requires the local authoring API")
    bot_id = stage06.load_state()["BOT_MDND"]
    with closing(stage06.client()) as client:
        prompts = stage06.check(client.get(f"/bots/{bot_id}/prompts"), "read prompts")
        systems = [p for p in prompts if p["type"] == "system"]
        if len(systems) != 1:
            raise ValueError("Expected one MDND system prompt")
        system = systems[0]
        version_no = system.get("publishedVersion") or system["activeVersion"]
        if system["activeVersion"] != version_no:
            raise ValueError("An unpublished prompt version exists; preserve that draft")
        version = next(v for v in system["versions"] if v["version"] == version_no)
        if version.get("promptMode") != "full":
            raise ValueError("Expected a full-mode MDND system prompt")
        before = version.get("fullPrompt") or ""
        after = patch_system(before)
        intents = stage06.check(client.get(f"/bots/{bot_id}/intents"), "read intents")
        opening = next(i for i in intents if i["name"] == "start_enquiries")
        if opening["status"] != "active":
            raise ValueError("The MDND opening intent must be active")
        print("".join(difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile=f"MDND/system_v{version_no}", tofile="MDND/opening_retry",
        )))
        print(f"Opening fallback: {opening.get('fallbackBehavior')!r} -> 'clarify'")
        if not args.apply:
            print("Preview complete. Use --apply to update this local bot.")
            return
        if before == after and opening.get("fallbackBehavior") == "clarify":
            print("MDND opening retry is already configured.")
            return
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="mdnd-opening-before-",
            suffix=".json", delete=False,
        ) as backup:
            json.dump({"bot_id": bot_id, "prompt_id": system["id"],
                       "version": version, "opening": opening}, backup,
                      ensure_ascii=False, indent=2)
            print(f"Previous configuration saved: {backup.name}")
        if opening.get("fallbackBehavior") != "clarify":
            stage06.check(client.patch(f"/intents/{opening['id']}", json={
                "fallbackBehavior": "clarify",
            }), "enable opening retry")
        if before != after:
            stage06.check(client.post(f"/prompts/{system['id']}/versions", json={
                "promptMode": "full", "fullPrompt": after,
                "note": "MDND opening retry: remove ticket-wait bridge after unclear greeting replies",
            }), "create opening-retry prompt version")
            for state in ("approved", "published"):
                stage06.check(client.patch(f"/prompts/{system['id']}", json={
                    "state": state,
                }), f"set local prompt {state}")
        print(f"Local MDND opening retry applied to {bot_id}.")


if __name__ == "__main__":
    main()
