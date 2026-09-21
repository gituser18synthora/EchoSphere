"""Every authored AU string checked against the bot's EFFECTIVE guardrails.

The Finance profile blocks assistant output that solicits an OTP/PIN/CVV.
The flow legitimately asks for an OTP, so its wording must clear the rule
(see ``otp_ask`` in au_bank/setup/03_workflow.py). Run after any node-text
change:  env/bin/python au_bank/tests/guardrail_audit.py
"""

import importlib.util, sys, pathlib
ROOT = pathlib.Path("/var/www/html/python/EchoSphere")
sys.path.insert(0, str(ROOT))
from shared.db.mysql import get_sessionmaker
from shared.guardrails import GuardrailEngine, load_effective_guardrails_sync
from shared.compliance import load_active_policies_sync

s = get_sessionmaker()()
eff = load_effective_guardrails_sync("tn_b8897f32d4aa", "bot_ac634648c152", session=s)
pol = load_active_policies_sync("tn_b8897f32d4aa", session=s)
print("effective guardrail codes:", sorted(r.code for r in eff.rules))

spec = importlib.util.spec_from_file_location("w", ROOT/"au_bank/setup/03_workflow.py")
w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)
spec2 = importlib.util.spec_from_file_location("p", ROOT/"au_bank/setup/02_prompts.py")
sys.modules['_common'] = type(sys)('_common'); sys.modules['_common'].BOT='x'
sys.modules['_common'].check=lambda *a: None; sys.modules['_common'].client=lambda: None
p = importlib.util.module_from_spec(spec2); spec2.loader.exec_module(p)

strings = []
for n in w.NODES:
    c = n.get("config") or {}
    for key in ("text", "question", "prompt", "unmatchedReply"):
        if c.get(key):
            strings.append((f"{n['id']}.{key}", c[key]))
    for key, sub in (("textByLanguage", c.get("textByLanguage")),
                     ("unmatchedReplyByLanguage", c.get("unmatchedReplyByLanguage"))):
        for lang, val in (sub or {}).items():
            strings.append((f"{n['id']}.{key}.{lang}", val))
for v in p.GREETING:
    strings.append((f"greeting.{v['language']}", v["content"]))

blocked = 0
for label, text in strings:
    g = GuardrailEngine(eff, compliance=pol); g.begin_turn()
    out = g.check_output_text(text)
    if out.blocked or g.hits:
        blocked += 1
        print(f"\n!! {label}\n   {text[:160]}\n   blocked={out.blocked} "
              f"rules={sorted({h.rule.code for h in g.hits})} reply_key={out.reply_key}")
print(f"\n{len(strings)} authored strings checked, {blocked} triggered a guardrail")
raise SystemExit(1 if blocked else 0)
