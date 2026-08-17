"""Stage 5: make the PM bot report WHY a booking was denied / how it was saved.

Problem this fixes
------------------
Workflow api nodes send the flow's slots as the request body; a node cannot set
a constant. So when the property manager states the reason inside the denial
sentence ("no, we are overbooked"), the PM bot takes a direct edge, never runs
the "ask reason" node, and the verification report reached the backend with
deny_reason = null. The customer bot then replayed that as deny_reason "other"
and spoke the generic price-denial line for an overbooked property.

Fix: one report connection per outcome path, each carrying the reason and the
resolution as a fixed `bodyTemplate` (template keys win over slot args), and the
PM workflow points every path at its matching connection. The customer bot now
reproduces the exact property-side reason and resolution wording.

Run after 03_workflows.py:  env/bin/python oyo/setup/05_pm_report_context.py
"""

import json

import httpx

BASE = "http://127.0.0.1:9001/api/v1"
MOCK = "http://127.0.0.1:9021/api/v1"
TENANT = "tn_de5cc992b1e9"
STATE_FILE = __file__.rsplit("/", 1)[0] + "/oyo_config_state.json"
BOT2 = json.load(open(STATE_FILE))["BOT2"]

REPORTS_URL = f"{MOCK}/verification-reports"

# (name, outcome, deny_reason, resolution, description)
REPORT_CONNECTIONS = [
    ("OYO PM Report Honored", "honored", None, "confirmed",
     "PM confirmed the booking on first ask."),
    ("OYO PM Report Honored — Penalty Advisory", "honored", "overbooked",
     "penalty_warning_accepted",
     "PM claimed overbooking but inventory was available; honored after the penalty advisory."),
    ("OYO PM Report Honored — Alternate Room", "honored", "maintenance",
     "alternate_room",
     "Property under maintenance; PM arranged an alternate room."),
    ("OYO PM Report Honored — ARR Pitch", "honored", "price_low",
     "arr_pitch_accepted",
     "Booking rate met the 7-day ARR; PM honored it after the ARR comparison."),
    ("OYO PM Report Honored — Compensation", "honored", "price_low",
     "compensation_added",
     "Rate below ARR; PM honored it after the complimentary amount was added."),
    ("OYO PM Report Not Honored", "not_honored", "other", "not_honored",
     "PM declined for a reason the flow could not classify, or a backend check failed."),
    ("OYO PM Report Denied — Overbooked", "not_honored", "overbooked", "not_honored",
     "Genuine overbooking confirmed against backend occupancy."),
    ("OYO PM Report Denied — Maintenance", "not_honored", "maintenance", "not_honored",
     "Property under maintenance with no alternate room available."),
    ("OYO PM Report Denied — Price", "not_honored", "price_low", "not_honored",
     "PM refused on rate, including after the complimentary amount offer."),
]

# PM-workflow edges to repoint: (from_node, edge_label_contains, connection name)
NODE_CONNECTIONS = {
    "n_api_report_h": "OYO PM Report Honored",
    "n_api_report_nh": "OYO PM Report Not Honored",
    "n_api_report_h_penalty": "OYO PM Report Honored — Penalty Advisory",
    "n_api_report_h_altroom": "OYO PM Report Honored — Alternate Room",
    "n_api_report_h_arr": "OYO PM Report Honored — ARR Pitch",
    "n_api_report_h_comp": "OYO PM Report Honored — Compensation",
    "n_api_report_nh_ob": "OYO PM Report Denied — Overbooked",
    "n_api_report_nh_mnt": "OYO PM Report Denied — Maintenance",
    "n_api_report_nh_price": "OYO PM Report Denied — Price",
}

# Which existing edge target moves to which new reporting node.
REWIRE = [
    # (from node, to node currently, new to node)
    ("n_cond_avail", "n_api_report_nh", "n_api_report_nh_ob"),      # genuinely overbooked
    ("n_intent_penalty", "n_api_report_h", "n_api_report_h_penalty"),
    ("n_intent_penalty", "n_api_report_nh", "n_api_report_nh_ob"),
    ("n_intent_altroom", "n_api_report_h", "n_api_report_h_altroom"),
    ("n_intent_altroom", "n_api_report_nh", "n_api_report_nh_mnt"),
    ("n_intent_arr", "n_api_report_h", "n_api_report_h_arr"),
    ("n_intent_arr", "n_api_report_nh", "n_api_report_nh_price"),
    ("n_intent_comp", "n_api_report_nh", "n_api_report_nh_price"),
    ("n_api_comp", "n_api_report_h", "n_api_report_h_comp"),
    ("n_api_comp", "n_api_report_nh", "n_api_report_nh_price"),
]


def check(r, what):
    if r.status_code >= 300:
        raise SystemExit(f"FAIL {what}: {r.status_code} {r.text[:600]}")
    print(f"ok   {what}")
    return r.json().get("data")


c = httpx.Client(base_url=BASE, timeout=30)
c.headers["Authorization"] = "Bearer " + check(
    c.post("/auth/login", json={"email": "oyo.config@oyo.com",
                                "password": "Demo@2026!"}), "login")["token"]

# ── 1. create / update the reporting connections ─────────────────────────────
existing = {a["name"]: a["id"]
            for a in check(c.get("/api-connections", params={"tenantId": TENANT}),
                           "list connections")}

for name, outcome, deny_reason, resolution, description in REPORT_CONNECTIONS:
    body_template = {"resolution": resolution}
    if deny_reason:
        body_template["deny_reason"] = deny_reason
    payload = {
        "name": name,
        "description": description,
        "method": "POST",
        "url": REPORTS_URL,
        "queryParams": {"channel": "pm", "outcome": outcome},
        # Template keys override same-named slot args, so the reason recorded is
        # the one this path actually established — never a stale utterance.
        "bodyTemplate": body_template,
        "isStateChanging": True,
        "tenantId": TENANT,
    }
    if name in existing:
        check(c.patch(f"/api-connections/{existing[name]}", json=payload),
              f"update connection '{name}'")
    else:
        check(c.post("/api-connections", json=payload), f"create connection '{name}'")

# ── 2. add the new reporting nodes + rewire the PM workflow ──────────────────
wf = check(c.get(f"/bots/{BOT2}/workflow"), "read PM workflow")
nodes = {n["id"]: n for n in wf["nodes"]}
edges = wf["edges"]

end_ok, end_deny = "n_end_ok", "n_end_deny"
template_h, template_nh = nodes["n_api_report_h"], nodes["n_api_report_nh"]

added = 0
for node_id, connection in NODE_CONNECTIONS.items():
    if node_id in nodes:
        nodes[node_id]["config"] = {"connection": connection}
        continue
    honored = "_h" in node_id.replace("_nh", "")
    base = template_h if honored else template_nh
    row, col = divmod(added, 3)
    nodes[node_id] = {
        "id": node_id, "kind": "api",
        "label": connection.replace("OYO PM ", ""),
        "config": {"connection": connection},
        "x": 40 + col * 300, "y": 1000 + row * 130,
    }
    edges.append({"id": f"e_{node_id}__end", "from": node_id,
                  "to": end_ok if honored else end_deny})
    added += 1
    print(f"ok   node {node_id} → {connection}")

for src, old_to, new_to in REWIRE:
    hits = [e for e in edges if e["from"] == src and e["to"] == old_to]
    if not hits:
        print(f"     (skip: no edge {src} → {old_to})")
    for edge in hits:
        edge["to"] = new_to
        edge["id"] = f"e_{src}__{new_to}__{abs(hash(edge.get('label', ''))) % 10000}"
        print(f"ok   rewired {src} → {new_to} [{edge.get('label', '')[:30]}]")

data = check(c.put(f"/bots/{BOT2}/workflow", json={
    "name": wf["name"], "nodes": list(nodes.values()), "edges": edges,
    "status": "approved",
}), f"save PM workflow ({len(nodes)} nodes, {len(edges)} edges)")
if data.get("issues"):
    print("     issues:", json.dumps(data["issues"])[:600])
print("pm report context done")
