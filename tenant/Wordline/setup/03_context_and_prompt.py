"""Step 03 — runtime context + system prompt follow the API-sourced data.

Runtime context ("Merchant terminal data" schema):
* ``outlet_name`` / ``bank_name`` / ``asset_type`` / ``terminal_id`` / ``mid``
  leave the *test payload*: those values now come from the merchant lookup
  during the call.  Keeping fixtures there would speak "Sharma General Store"
  on every Testing Studio run and every live call without dialer data;
* the fields stay *declared* (plus ``contact_number``, minus ``mid`` which the
  CSV does not carry) so the prompt labels them and lists them as unknown
  until the lookup fills them;
* ``customer_verified: false`` in the test payload (declared boolean) turns on
  the session-verification gate: the looked-up facts become visible to the
  LLM for follow-up questions only after the workflow maps
  ``customer_verified: true`` from a successful lookup (same pattern as the
  Honasa bot's ``identity_verified``);
* ``customer_name`` and ``docket_number`` fixtures are untouched.

System prompt: a new version whose "# CALLER DATA" section says the terminal
details are known only after the Terminal ID lookup.  Published.

2026-11-10 (CSV gained Address and Contact Person): ``address`` and
``contact_person`` are declared too (labelled for the LLM; listed as "not
available" until the lookup fills them), the missing-value policy names
them, and a further prompt version (``upgrade_prompt_visit_fields``) adds
them to "# CALLER DATA" with the rule to say the four speech-shaped values
(outlet_name, asset_type, address, contact_person) exactly as written, and
tells the model that with a found record the request / visit steps read the
address and contact person back instead of collecting them.  Both prompt
transforms are idempotent and exact-match: an edited section is refused, never
guessed.  On an environment whose merchant service predates the two fields the
new text is harmless — the values simply stay unknown and the flow asks.

    env/bin/python tenant/Wordline/setup/03_context_and_prompt.py            # dry run
    env/bin/python tenant/Wordline/setup/03_context_and_prompt.py --apply
"""

from __future__ import annotations

import copy
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tenant.Wordline.setup.common import BOT_ID, check, client, parser, show  # noqa: E402

API_SOURCED = ("terminal_id", "outlet_name", "bank_name", "asset_type", "contact_number")
DROP_FROM_PAYLOAD = (*API_SOURCED, "mid")
DECLARED = [
    {"key": "customer_name", "type": "string",
     "label": "Merchant's name (spoken in the Terminal ID question)"},
    {"key": "docket_number", "type": "string",
     "label": "Service docket number for a battery replacement visit"},
    {"key": "customer_verified", "type": "boolean",
     "label": "Merchant verified (set by the Terminal ID lookup during the call)"},
    {"key": "terminal_id", "type": "string", "label": "Terminal ID (from the lookup)"},
    {"key": "outlet_name", "type": "string", "label": "Outlet name (from the lookup)"},
    {"key": "bank_name", "type": "string", "label": "Acquiring bank (from the lookup)"},
    {"key": "asset_type", "type": "string", "label": "Terminal type (from the lookup)"},
    {"key": "contact_number", "type": "string",
     "label": "Registered contact number (from the lookup)"},
    {"key": "address", "type": "string", "label": "Registered address (from the lookup)"},
    {"key": "contact_person", "type": "string", "label": "Contact person (from the lookup)"},
]
MISSING_POLICY = ("Never guess a merchant detail. The Terminal ID, outlet, bank, terminal "
                  "type, contact number, registered address and contact person are known "
                  "only after the flow has looked up the Terminal ID the caller gave; before "
                  "that they are unknown. If a name, docket number or any merchant detail is "
                  "absent, do not invent it.")

CALLER_DATA_HEADING = "# CALLER DATA"
CALLER_DATA_NEW = """# CALLER DATA

The call data may carry customer_name and docket_number. The merchant's terminal details —
terminal_id, outlet_name, bank_name, asset_type, contact_number — are fetched from the merchant
records only after the caller gives the Terminal ID and the flow has looked it up; before that
they are unknown, and if the lookup fails they stay unknown for the whole call. Use a value
only when the caller asks about it ("मेरा terminal किस bank का है?"). Never read out the
whole record, never invent a missing value, and never say a number the call data does not hold."""
PROMPT_NOTE = "Merchant Helpdesk — terminal details from the merchant lookup API v4"

# 2026-11-10: address + contact person (exact-match replacements of the v4 text).
CALLER_DATA_VISIT = """# CALLER DATA

The call data may carry customer_name and docket_number. The merchant's terminal details —
terminal_id, outlet_name, bank_name, asset_type, contact_number, address (the registered
address) and contact_person — are fetched from the merchant records only after the caller gives
the Terminal ID and the flow has looked it up; before that they are unknown, and if the lookup
fails they stay unknown for the whole call. Use a value only when the caller asks about it
("मेरा terminal किस bank का है?", "आपके records में मेरा address क्या है?"). Never read out the
whole record, never invent a missing value, and never say a number the call data does not hold.
outlet_name, asset_type, address and contact_person are already written the way they must be
spoken: say them exactly as written, in English letters even inside a Hindi reply — do not change
their capitals, do not translate or transliterate them, never spell a name or a word letter by
letter, and keep the numbers in an address as written. If the caller corrects the address or the
contact person, accept the correction and do not repeat the old value."""
RAISE_DETAILS_OLD = """Paper roll request raised by us — the details we need: Merchant or Outlet Name, T-I-D number,
complete address, Work Order details including the weekly off, preferred working time for the team's
visit, contact number, contact person's name, and the quantity (1 to 9)."""
RAISE_DETAILS_NEW = RAISE_DETAILS_OLD + """ When the merchant's record
was found, the flow already holds the Outlet Name, T-I-D and contact number and reads back the
registered address and contact person; the caller only confirms or corrects those and gives the
Work Order details with the weekly off, the preferred working time and the quantity. The
engineer-visit details for a damaged battery work the same way when the record was found.
Without a found record the caller gives every detail."""
PROMPT_NOTE_VISIT = ("Merchant Helpdesk — address and contact person from the merchant lookup "
                     "(TTS-ready values)")


def upgrade_schema(current: dict) -> dict:
    body = {
        "name": current.get("name") or "Merchant terminal data",
        "sourceMode": current.get("sourceMode") or "manual",
        "apiConnectionId": current.get("apiConnectionId"),
        "responsePath": current.get("responsePath"),
        "fields": copy.deepcopy(DECLARED),
        "allowAdditional": bool(current.get("allowAdditional", True)),
        "testPayload": {k: v for k, v in (current.get("testPayload") or {}).items()
                        if k not in DROP_FROM_PAYLOAD},
        "missingValuePolicy": MISSING_POLICY,
        "domainPolicy": current.get("domainPolicy") or "generic",
        "identityConfirmationMode": current.get("identityConfirmationMode"),
    }
    body["testPayload"].setdefault("customer_verified", False)
    return body


def upgrade_prompt(text: str) -> str | None:
    """Replace the CALLER DATA section; None when it is already current."""
    # Whitespace-normalised: the v4 section wraps "merchant\nrecords", so a
    # literal check never matched and a re-run re-created the same text.
    if "fetched from the merchant records" in " ".join(text.split()):
        return None
    pattern = re.compile(r"# CALLER DATA\n.*?(?=\n\n(?:Battery, battery looks fine|# ))", re.S)
    if not pattern.search(text):
        raise SystemExit("prompt: '# CALLER DATA' section not found — refusing to guess")
    return pattern.sub(lambda _m: CALLER_DATA_NEW, text, count=1)


def upgrade_prompt_visit_fields(text: str) -> str | None:
    """Add address / contact person; None when already current.  Exact-match
    on the v4 texts — an edited section is refused, never guessed."""
    if "address (the registered\naddress) and contact_person" in text:
        return None
    if CALLER_DATA_NEW not in text:
        raise SystemExit("prompt: the v4 '# CALLER DATA' section was edited — refusing to guess")
    if RAISE_DETAILS_OLD not in text:
        raise SystemExit("prompt: the 'Paper roll request raised by us' paragraph was edited — "
                         "refusing to guess")
    return (text.replace(CALLER_DATA_NEW, CALLER_DATA_VISIT, 1)
                .replace(RAISE_DETAILS_OLD, RAISE_DETAILS_NEW, 1))


def main() -> None:
    p = parser(__doc__.split("\n")[0])
    p.add_argument("--skip-prompt", action="store_true",
                   help="leave the system prompt untouched (runtime context only)")
    p.add_argument("--show-diff", action="store_true", help="print the prompt diff (dry run review)")
    args = p.parse_args()
    c = client(args.base)

    current = check(c.get(f"/bots/{BOT_ID}/runtime-context"), "read runtime context") or {}
    body = upgrade_schema(current)
    show("runtime context (new)", {k: body[k] for k in ("fields", "testPayload", "missingValuePolicy")})
    dropped = [k for k in (current.get("testPayload") or {}) if k in DROP_FROM_PAYLOAD]
    print(f"     test payload keys dropped: {dropped}")

    prompts = check(c.get(f"/bots/{BOT_ID}/prompts"), "read prompts") or []
    system = next((p for p in prompts if p.get("type") == "system"), None)
    if system is None:
        raise SystemExit("no system prompt on the bot")
    active = next((v for v in system.get("versions") or []
                   if v.get("version") == system.get("activeVersion")), None)
    text = (active or {}).get("fullPrompt") or ""
    new_text, note = None, PROMPT_NOTE
    if not args.skip_prompt:
        v4 = upgrade_prompt(text)
        visit = upgrade_prompt_visit_fields(v4 or text)
        new_text = visit or v4
        note = PROMPT_NOTE_VISIT if visit else PROMPT_NOTE
    if args.skip_prompt:
        print(f"     prompt {system['id']} v{system.get('activeVersion')} left untouched (--skip-prompt)")
    elif new_text is None:
        print("     prompt already current (no new version)")
    else:
        print(f"     prompt {system['id']} v{system.get('activeVersion')} → new version "
              f"({note!r}; {len(text)} → {len(new_text)} chars)")
        if args.show_diff:
            import difflib
            for line in difflib.unified_diff(text.splitlines(), new_text.splitlines(),
                                             "published", "new", lineterm="", n=0):
                print("     " + line)

    if not args.apply:
        print("dry run — re-run with --apply")
        return
    check(c.put(f"/bots/{BOT_ID}/runtime-context", json=body), "save runtime context")
    if new_text is not None:
        check(c.post(f"/prompts/{system['id']}/versions",
                     json={"promptMode": "full", "fullPrompt": new_text, "note": note}),
              "new prompt version")
        check(c.patch(f"/prompts/{system['id']}", json={"state": "approved"}), "approve prompt")
        check(c.patch(f"/prompts/{system['id']}", json={"state": "published"}), "publish prompt")
    print("done")


if __name__ == "__main__":
    main()
