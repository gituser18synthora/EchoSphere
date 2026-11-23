"""Pure, local workflow repair for the Haier 23 November call audit.

The shared interpreter already supports structured capture, correction edges
and condition nodes.  These authored rules keep negation attached to its
subject and require actual consent at the re-dispatch proposal.  No I/O or
shared runtime changes are performed here.
"""
from __future__ import annotations

from copy import deepcopy
import re

from haier.setup.haier_service_need_repair import (
    BOT_ID, WORKFLOW_ID, apply_service_need_repair,
)


PREFIX = "n_haier_audit_"
_B = r"(?<![\w\u0900-\u097f])"
_E = r"(?![\w\u0900-\u097f])"
_NEG = r"(?:nahi+n?|nahin|nhi|नहीं|नही|not|never|didn't|did not)"
_TECH = r"(?:haier(?:\s+(?:ke|ka|ki|के|का|की))?\s+)?(?:technician|टेक्नीशियन|टेक्निशियन|तकनीशियन|टेक्नीशियन)"
_CONTACT = r"(?:call|phone|कॉल|काल|फोन|फ़ोन)"
_VISIT = r"(?:visit|विजिट|विज़िट|आ(?:या|ए|ये)|aa(?:ya|ye)|came|visited)"
_INSTALL = r"(?:installation|install|service|इंस्टॉलेशन|इंस्टालेशन|इंस्टलेशन|इंस्टॉल|सर्विस|सेवा)"
_DONE = r"(?:ho\s*(?:gaya|gayi|chuka|chuki)|हो\s*(?:गया|गई|गयी|चुका|चुकी)|complete(?:d)?|done|installed|finished)"
_PENDING = r"(?:pending|पेंडिंग|पेन्डिंग|baki|baaki|बाकी|अधूरा|अधूरी)"
_SEP = r"[\s,.!।:;—-]+"
_YES = r"(?:yes|yeah|yup|yep|haan|han|ha|हाँ|हां|हा|जी\s*हाँ|जी\s*हां|हाँजी|हांजी)"
_NO = r"(?:no+|nope|nahi+n?|nahin|nhi|नहीं|नही|नो)"
_POLITE = r"(?:ji|jee|sir|maam|mam|madam|जी|सर|मैम|please|प्लीज)"
_BARE_NO = rf"^\W*(?:(?:{_POLITE}){_SEP})?(?:{_NO})(?:{_SEP}(?:{_NO}|{_POLITE}))*\W*$"
_BARE_YES = rf"^\W*(?:(?:{_POLITE}){_SEP})?(?:{_YES})(?:{_SEP}(?:{_YES}|{_POLITE}))*\W*$"
_ATTENTION = (
    rf"^\W*(?:(?:hello|hallo|hlo|हेलो|हलो|हैलो|हॅलो|hmm+|hm+|हम्म+|हम्+|हमम|हूँ|हुं){_SEP}?)+\W*$",
    rf"^\W*(?:(?:{_YES}|{_POLITE}){_SEP})*(?:boliye|bolo|bataiye|batao|बोलिए|बोलिये|बताइए|बताइये|कहिए|कहिये)\W*$",
    rf"^\W*(?:{_YES}|{_NO})(?:\s+{_POLITE})?\s*\?\W*$",
    r"^\W*(?:warranty|वारंटी|guarantee|गारंटी)\W*$",
)
_INSTALL_DONE = rf"{_B}{_INSTALL}(?:\s+\S+){{0,3}}?\s+(?:{_DONE}|kar\s+(?:liya|li|diya)|कर\s+(?:लिया|ली|दिया)){_E}"
_DECLINED_SERVICE = (
    rf"{_B}(?:requirement|रिक्वायरमेंट|zaroorat|zarurat|ज़रूरत|जरूरत|आवश्यकता)(?:\s+(?:ki|की|to|toh|तो|abhi|अभी)){{0,3}}\s+{_NEG}{_E}",
    rf"{_B}(?:service|सर्विस|सेवा)(?:\s+(?:ki|की|abhi|अभी)){{0,2}}\s+{_NEG}\s+(?:chahiye|चाहिए|needed|required){_E}",
    r"\b(?:don't|do not|dont|no longer)\s+(?:need|want|require)\s+(?:any\s+|the\s+)?service\b",
    r"\bno\s+service\s+(?:needed|required)\b",
    r"\b(?:cancel|close)\s+(?:kar(?:a|ai)?iye|karaiye|karwa(?:i)?iye|karo|kar\s+(?:do|dijiye))\b",
    r"(?:कैंसल|कैंसिल|कैंसेल|क्लोज|बंद)\s+(?:कर\s+(?:दो|दीजिए)|कराइए|कराइये|करवाइए)",
)
_DECLINE_QUESTION = (
    r"(?:\?|\b(?:why|how|when|kya|kyun|kyon|kaise|kab|are\s+you|do\s+you|does|is\s+it)\b|क्यों|क्या|कैसे|कब)"
)
_DECLINED_SERVICE = tuple(rf"^(?![\s\S]*{_DECLINE_QUESTION})[\s\S]*(?:{pattern})"
                          for pattern in _DECLINED_SERVICE)
_INSTALL_PENDING = (
    rf"{_B}{_INSTALL}(?:\s+(?:abhi|tak|hi|अभी|तक|ही|is|has|been)){{0,3}}\s+{_NEG}\s+(?:hui|hua|huwa|हुई|हुआ|हो|done|completed?){_E}",
    rf"{_B}{_INSTALL}(?:\s+(?:abhi|अभी|is|still)){{0,2}}\s+{_PENDING}{_E}",
    rf"{_B}(?:abhi|अभी|still)\s+{_PENDING}{_E}",
)
_VISITED = (
    rf"{_B}{_TECH}(?:\s+(?:to|toh|तो|ji|जी|bhi|भी|already|abhi|अभी|pehle|पहले)){{0,3}}\s+(?:aa\s*(?:gaya|gaye|chuka|chuke)|आ\s*(?:गया|गए|चुका|चुके)|aaya\s*(?:tha|hai)?|आया\s*(?:था|है)?|aaye\s*(?:the|hain)?|आए\s*(?:थे|हैं)?|came|visited){_E}",
    rf"{_B}{_TECH}(?:\s+\S+){{0,2}}?\s+(?:demo\s+(?:de|dikha)|डेमो\s+(?:दे|दिखा)){_E}",
)
_CALLED = (
    rf"{_B}{_TECH}(?:\s+(?:ne|ka|ki|ke|ने|का|की|के))?\s+{_CONTACT}\s+(?:kiya|किया|aaya|आया|tha|था){_E}",
    rf"{_B}{_TECH}\s+(?:called|phoned){_E}",
)
_NEGATED_AFTER = rf"(?!\s+(?:(?:tha|thi|the|hai|है|था|थी|थे)\s+)?{_NEG}{_E})"
_VISITED = tuple(pattern + _NEGATED_AFTER for pattern in _VISITED)
_CALLED = tuple(pattern + _NEGATED_AFTER for pattern in _CALLED)
_NO_CALL = (
    rf"{_B}{_TECH}\s+(?:(?:ki|ke)\s+taraf\s+se|(?:की|के)\s+तरफ\s+से)\s+{_NEG}{_E}",
    rf"{_B}{_TECH}(?:\s+(?:ka|ki|ke|का|के|की|ne|ने))?\s+{_CONTACT}(?:\s+(?:abhi|tak|अभी|तक)){{0,2}}\s+{_NEG}{_E}",
    rf"{_B}(?:no\s+(?:call|phone)|never\s+called)\s+(?:from\s+)?(?:a\s+)?{_TECH}{_E}",
    rf"{_B}{_TECH}(?:\s+(?:ka|ki|ke|ne|का|की|के|ने))?\s+{_CONTACT}\s+(?:aaya|kiya|आया|किया)(?:\s+(?:tha|hai|था|है))?\s+{_NEG}{_E}",
)
_NO_VISIT = (
    rf"{_B}{_TECH}(?:\s+(?:abhi|tak|अभी|तक)){{0,2}}\s+{_NEG}\s+(?:aaya|aaye|आया|आए|come|visit){_E}",
    rf"{_B}{_TECH}\s+(?:never|did\s+not|didn't)\s+(?:come|visit){_E}",
    rf"{_B}{_TECH}\s+(?:aaya|aaye|आया|आए|aa\s+gaya|आ\s+गया)(?:\s+(?:tha|hai|था|है))?\s+{_NEG}{_E}",
)


def _clean(pattern: str) -> str:
    return pattern.replace("(?i)", "")


def _literal_union(labels: list[str]) -> str:
    """Factor shared alias prefixes to keep the authored JSON compact."""
    trie = {}
    for label in labels:
        for token in re.split(r"[/,|]", label or ""):
            if not token.strip():
                continue
            branch = trie
            for char in token.strip():
                branch = branch.setdefault(char, {})
            branch[""] = None

    def emit(branch):
        children = [re.escape(char) + emit(child)
                    for char, child in sorted(branch.items()) if char]
        suffix = children[0] if len(children) == 1 else "(?:" + "|".join(children) + ")"
        if not children:
            return ""
        return "(?:" + suffix + ")?" if "" in branch else suffix

    return _B + emit(trie) + _E if trie else ""


def _exclusive(routes: list[tuple[str, list[str]]]) -> dict:
    """Make priority explicit in regexes, independent of MySQL JSON key order."""
    result, previous = {}, []
    for canonical, patterns in routes:
        body = "|".join(f"(?:{_clean(p)})" for p in patterns)
        guard = rf"(?![\s\S]*(?:{'|'.join(previous)}))" if previous else ""
        pattern = rf"^{guard}[\s\S]*(?:{body})"
        re.compile(pattern, re.I)
        result[canonical] = [pattern]
        previous.extend(f"(?:{_clean(p)})" for p in patterns)
    return result


def _put(items: list[dict], item: dict) -> None:
    old = next((x for x in items if x["id"] == item["id"]), None)
    if old is not None:
        if old != item:
            raise ValueError(f"Existing audit repair has changed: {item['id']}")
    else:
        items.append(item)


def _patterns(captures: list[dict]) -> list[str]:
    return [_clean(p) for capture in captures
            for alternatives in (capture.get("entity", {}).get("synonymPatterns") or {}).values()
            for p in ([alternatives] if isinstance(alternatives, str) else alternatives)]


def _install_routes(result: dict, hub: str, routes: list[tuple[str, str, list[str]]],
                    *, fallback: str, expected_original: str | None = None) -> None:
    """Layer scoped decisions over the existing correction contract safely.

    A previous route slot never directs a later turn: only this turn's regex
    capture triggers correction. Existing captures receive an explicit
    original route on the same turn, preserving their original handler.
    """
    nodes = {node["id"]: node for node in result["nodes"]}
    node = nodes.get(hub)
    if not node or node.get("kind") != "intent":
        raise ValueError(f"Reviewed intent node is missing: {hub}")
    config = node["config"]
    variable = f"haier_audit_{hub}_route"
    scoped_variable = config.get("correctionCaptureVariable")
    if scoped_variable not in (None, variable):
        raise ValueError(f"Unreviewed correction capture scope at {hub}")
    config["correctionCaptureVariable"] = variable
    existing_specs = [capture for capture in config.get("alsoCapture", [])
                      if capture.get("variable") != variable]
    edges = [edge for edge in result["edges"] if edge.get("from") == hub]
    corrections = [edge for edge in edges if "correction" in re.split(r"[/,|]", edge.get("label", ""))]
    if len(corrections) > 1:
        raise ValueError(f"Multiple correction contracts at {hub}")
    original = config.get("haierAuditOriginalCorrection")
    if corrections and not original and corrections[0]["id"] != f"e_haier_audit_{hub}_correction":
        original = corrections[0]["to"]
    if original and original != expected_original:
        raise ValueError(f"Unreviewed correction contract at {hub}: {original}")
    if expected_original and original != expected_original:
        raise ValueError(f"Missing reviewed correction contract at {hub}")
    authored = [(name, patterns) for name, _, patterns in routes]
    if original:
        patterns = _patterns(existing_specs)
        if not patterns:
            raise ValueError(f"Existing correction has no structured capture at {hub}")
        authored.append(("original", patterns))
        routes = [*routes, ("original", original, patterns)]
        config["haierAuditOriginalCorrection"] = original
    # alsoCapture is an ordered JSON array. Low-priority matches run first;
    # later, more specific matches overwrite the same turn-local route.
    # Each entity has one canonical, so object-key ordering cannot affect it.
    captures = []
    for name, patterns in reversed(authored):
        for pattern in patterns:
            re.compile(_clean(pattern), re.I)
        captures.append({"variable": variable, "overwrite": True,
                         "entity": {"dataType": "text", "matchCanonicalValues": False,
                                    "synonymPatterns": {name: patterns}}})
    old_captures = [capture for capture in config.get("alsoCapture", [])
                    if capture.get("variable") == variable]
    if old_captures and old_captures != captures:
        raise ValueError(f"Existing audit capture contract has changed: {hub}")
    config["alsoCapture"] = [*existing_specs, *captures]
    first = f"{PREFIX}{hub}_0"
    if corrections:
        corrections[0]["to"] = first
    else:
        _put(result["edges"], {"id": f"e_haier_audit_{hub}_correction", "from": hub,
                               "to": first, "label": "correction"})
    for index, (name, target, _) in enumerate(routes):
        if target not in nodes:
            raise ValueError(f"Reviewed route destination is missing: {target}")
        node_id = f"{PREFIX}{hub}_{index}"
        _put(result["nodes"], {"id": node_id, "kind": "condition",
                               "label": f"Audit repair — {hub}: {name}",
                               "config": {"variable": variable, "operator": "equals", "value": name}})
        _put(result["edges"], {"id": f"e_{node_id}_yes", "from": node_id, "to": target, "label": "true"})
        next_id = f"{PREFIX}{hub}_{index + 1}" if index + 1 < len(routes) else fallback
        _put(result["edges"], {"id": f"e_{node_id}_no", "from": node_id, "to": next_id, "label": "false"})


def _guard_specials(patterns: list[str], outgoing: list[dict], ignore: set[str]) -> list[str]:
    preserved = _literal_union([edge.get("label", "") for edge in outgoing
                               if edge["id"].startswith(("e_v2_special_", "e_warranty_"))
                               and edge["to"] not in ignore])
    if not preserved:
        return patterns
    guard = rf"^(?![\s\S]*(?:{preserved}))"
    body = "|".join(f"(?:{_clean(p)})" for p in patterns)
    return [guard + rf"[\s\S]*(?:{body})"]


def _contact_repairs(result: dict) -> None:
    reviewed_yes = {"bill": "n_reason_bill_q", "refused_pay": "n_reason_refused_pay_q",
                    "refused_visit": "n_reason_refused_q", "not_delivered": "n_reason_delivered_q",
                    "ecom_damaged": "n_reason_ecom_q", "unit_ok": "n_reason_unit_ok_q",
                    "not_haier": "n_reason_not_haier_q"}
    for stem, mode in [("bill", "visit"), ("refused_pay", "visit"),
                       ("refused_visit", "call"), ("not_delivered", "call"),
                       ("ecom_damaged", "call"), ("unit_ok", "both"), ("not_haier", "both")]:
        for suffix in ("", "_clarify"):
            hub = f"n_intro_{stem}{suffix}"
            outgoing = [e for e in result["edges"] if e.get("from") == hub]
            yes = next((e for e in outgoing if e["id"] == f"e_{hub}_yes"), None)
            no = next((e for e in outgoing if e["id"] == f"e_{hub}_no"), None)
            if not yes or not no:
                raise ValueError(f"Reviewed contact edges are missing: {hub}")
            reviewed_no = "n_apology_call" if mode == "call" else (
                "n_apology_visit" if mode == "visit" else "n_apologize_no_visit")
            if yes.get("to") != reviewed_yes[stem] or no.get("to") != reviewed_no:
                raise ValueError(f"Reviewed contact destinations have changed: {hub}")
            clarify = f"n_intro_{stem}_clarify" if not suffix else "m_n_v2_end_unverified"
            routes = [("attention", clarify, list(_ATTENTION))]
            original = f"{hub}_contact_confirmed" if stem == "unit_ok" else None
            ignore = {"n_v2_completed_check", "n_apology_visit", "n_apology_call"}
            protect = lambda patterns: _guard_specials(patterns, outgoing, ignore)
            routes.append(("no_service", "m_n_end_no_service", protect(list(_DECLINED_SERVICE))))
            routes.append(("completed", "n_v2_completed_check", protect([_INSTALL_DONE])))
            visit_target = "n_v2_no_response_status" if mode == "call" else yes["to"]
            routes.append(("visited", visit_target, protect(list(_VISITED))))
            no_call_target = no["to"] if mode == "call" else original or clarify
            routes.append(("no_call", no_call_target, protect(list(_NO_CALL))))
            no_visit_target = no["to"] if mode == "visit" else original or clarify
            routes.append(("no_visit", no_visit_target, protect(list(_NO_VISIT))))
            call_target = clarify if mode == "visit" else original or yes["to"]
            routes.append(("called", call_target, protect(list(_CALLED))))
            _install_routes(result, hub, routes, fallback=clarify, expected_original=original)
            # Humming is an acknowledgement, never technician-contact proof.
            yes["label"] = "/".join(t for t in yes["label"].split("/")
                                      if not re.fullmatch(r"hmm*|hm+|हम्म+", t.strip(), re.I))


def _status_repairs(result: dict) -> None:
    for base, completed in [("n_v2_no_response_status", False), ("n_v2_completed_check", True)]:
        for suffix in ("", "_clarify"):
            hub = base + suffix
            node = next(n for n in result["nodes"] if n["id"] == hub)
            outgoing = [e for e in result["edges"] if e.get("from") == hub]
            captures = node["config"].get("alsoCapture", [])
            status = next((c for c in captures if c.get("variable") == "installation_status"), None)
            if not status:
                raise ValueError(f"Reviewed installation-status capture is missing: {hub}")
            patterns = node["config"].setdefault(
                "haierAuditOriginalStatusPatterns", deepcopy(status["entity"]["synonymPatterns"]))
            done, pending = list(patterns["done"]), list(patterns["pending"])
            done = [_clean(p) for p in done]
            pending = [_clean(p) for p in pending]
            for p in _INSTALL_PENDING:
                if p not in pending:
                    pending.append(p)
            if completed and _BARE_NO not in done:
                done.append(_BARE_NO)
            ignore = {"n_v2_completed_check", "n_v2_pending_case_check"}
            protect = lambda values: _guard_specials(values, outgoing, ignore)
            status["entity"]["synonymPatterns"] = _exclusive([
                ("pending", [*pending, _BARE_YES] if completed else pending), ("done", done)])
            clarify = base + "_clarify" if not suffix else "m_n_v2_end_unverified"
            done_target = "n_v2_guard_completed" if completed else "n_v2_completed_check"
            routes = [("attention", clarify, list(_ATTENTION)),
                      ("pending", "n_service_need", protect(pending)),
                      ("done", done_target, protect(done))]
            if completed:
                # One polarity: yes = work remains, no = nothing remains.
                en = "Is any installation or service work still pending for this product?"
                hi = "क्या इस product की installation या service का कोई काम अभी बाकी है?"
                node["config"]["prompt"] = en
                node["config"].setdefault("textByLanguage", {}).update(en=en, hi=hi)
                for e in outgoing:
                    if e["id"].startswith("e_completed_yes"):
                        e["to"] = "n_v2_pending_case_check"
                    elif e["id"].startswith("e_completed_no"):
                        e["to"] = "n_v2_guard_completed"
                routes.append(("pending_yes", "n_v2_pending_case_check", [_BARE_YES]))
            _install_routes(result, hub, routes, fallback=clarify)


def _service_repairs(result: dict) -> None:
    for hub in ("n_service_need", "n_service_need_clarify"):
        outgoing = [e for e in result["edges"] if e.get("from") == hub]
        # A temporal word alone is not a request for service.
        for e in outgoing:
            if e["id"].startswith("e_service_soon"):
                e["label"] = "/".join(t for t in e["label"].split("/")
                                         if t.strip().lower() not in {"abhi", "अभी"})
        clarify = "n_service_need_clarify" if hub == "n_service_need" else "m_n_v2_end_unverified"
        terminal_no = rf"(?:^|[.!।;])\s*(?:{_POLITE}\s+)?{_NO}(?:\s+{_POLITE})?[\s.!।]*$"
        decline = _guard_specials([terminal_no, _BARE_NO], outgoing, set())
        _install_routes(result, hub, [("attention", clarify, list(_ATTENTION)),
                                      ("declined", "m_n_end_no_service", decline)],
                        fallback=clarify, expected_original="m_n_end_no_service")


def _refusal_timing_repairs(result: dict) -> None:
    timing = [
        r"\b(?:renovation|construction|shifting)\b[^.!?।]*(?:hone\s+ke\s+baad|complete\s+hone\s+ke\s+baad|after|baad\s+mein)",
        r"(?:रिनोवेशन|रेनोवेशन|रिन्यूएशन|renovation|कंस्ट्रक्शन|शिफ्टिंग)[^.!?।]*(?:होने\s+के\s+बाद|पूरा\s+होने\s+पर)",
        r"(?:होने\s+के\s+बाद|hone\s+ke\s+baad)[^.!?।]*(?:इंस्टॉल|इंस्टाल|installation|install|service|सर्विस)",
    ]
    for hub in ("n_reason_refused_q", "n_reason_refused_q_clarify"):
        outgoing = [e for e in result["edges"] if e.get("from") == hub]
        clarify = "n_reason_refused_q_clarify" if hub == "n_reason_refused_q" else "m_n_v2_end_unverified"
        preserve = lambda ps: _guard_specials(ps, outgoing, {"n_v2_future_when"})
        _install_routes(result, hub, [("no_service", "m_n_end_no_service", preserve(list(_DECLINED_SERVICE))),
                                      ("after_work", "n_v2_future_when", preserve(timing))], fallback=clarify)


def _redispatch_repairs(result: dict) -> None:
    nodes = {n["id"]: n for n in result["nodes"]}
    hub = nodes.get("n_redispatch")
    if not hub or hub.get("kind") != "intent":
        raise ValueError("Reviewed re-dispatch proposal is missing")
    clarify = PREFIX + "redispatch_confirm"
    prompt = "May I forward this request for re-dispatch so that a Haier technician can visit?"
    hi = "क्या मैं यह request re-dispatch के लिए आगे भेजूँ, ताकि Haier के technician visit कर सकें?"
    config = deepcopy(hub["config"])
    config.pop("alsoCapture", None)
    config.update(prompt=prompt, elseIsAnswer=False,
                  textByLanguage={"en": prompt, "hi": hi})
    new = {"id": clarify, "kind": "intent", "label": "Re-dispatch — explicit confirmation",
           "config": deepcopy(config)}
    # On a repeated transform this node already has our capture contract.
    if clarify not in nodes:
        result["nodes"].append(new)
        original_edges = [e for e in result["edges"] if e.get("from") == "n_redispatch"]
        for e in original_edges:
            clone = {**deepcopy(e), "id": "e_haier_audit_confirm_" + e["id"], "from": clarify}
            if clone["label"] == "else":
                clone["to"] = "m_n_v2_end_unverified"
            result["edges"].append(clone)
    hub["config"].update(prompt=prompt, elseIsAnswer=False,
                         textByLanguage={"en": prompt, "hi": hi})
    okay = r"(?:ok|okay|theek\s+hai|thik\s+hai|ठीक\s+है|ओके|sure|alright)"
    consent = [
        _BARE_YES,
        rf"^\W*(?:{_POLITE}\s+)?{okay}(?:{_SEP}(?:{_POLITE}|thank\s+you(?:\s+so\s+much)?|thanks|धन्यवाद|शुक्रिया))*\W*$",
        rf"^\W*(?:please\s+)?(?:send\s+(?:a\s+|the\s+)?technician|forward\s+(?:the\s+|my\s+)?request|(?:re-?dispatch|रीडिस्पैच)\s+(?:kar\s+(?:do|dijiye)|कर\s+(?:दो|दीजिए))|(?:kar|bhej|कर|भेज)\s+(?:do|dijiye|दो|दीजिए))(?:\s+{_POLITE})?\W*$",
    ]
    consent = [rf"^(?![\s\S]*\?)[\s\S]*(?:{p})" for p in consent]
    for source, unknown in [("n_redispatch", clarify), (clarify, "m_n_v2_end_unverified")]:
        outgoing = [e for e in result["edges"] if e.get("from") == source]
        for e in outgoing:
            if e.get("label") == "else":
                e["to"] = unknown
        # Authored timing and opt-out facts beat a generic model affirmation.
        # An explicit date wins over a vague "not now" in the same answer.
        special_edges = [e for e in outgoing if "e_v2_special_" in e["id"]]
        priority = ["m_n_v2_end_repeat_calls", "m_n_end_gift", "m_n_end_resale",
                    "n_future_confirm", "n_v2_future_when"]
        targets = sorted({e["to"] for e in special_edges},
                         key=lambda target: (priority.index(target) if target in priority else len(priority), target))
        routes = [(f"special_{index}", target, [_literal_union([
            e.get("label", "") for e in special_edges if e["to"] == target])])
                  for index, target in enumerate(targets)]
        no_proposal = [
            rf"^(?![\s\S]*\?)[\s\S]*(?:{_BARE_NO})",
            rf"^\W*{_NO}(?:{_SEP}(?:thanks|thank\s+you|{_POLITE}))*\W*$",
            r"\b(?:don't|do not)\s+send\b|(?:मत\s+भेज|mat\s+bhej)",
        ]
        routes += [("no_service", "m_n_end_no_service", list(_DECLINED_SERVICE)),
                   ("consent", "m_n_end_redispatch_thanks", consent),
                   ("declined", "n_service_need_clarify", no_proposal),
                   ("unclear", unknown, [r"[\s\S]+$"])]
        _install_routes(result, source, routes, fallback=unknown)


def _identity_repair(result: dict) -> None:
    nodes = {n["id"]: n for n in result["nodes"]}
    gate = nodes.get("n_identity_gate")
    if not gate or gate.get("kind") != "ask" or gate["config"].get("variable") != "identity_answer":
        raise ValueError("Reviewed identity gate is missing")
    patterns = gate["config"]["entity"]["synonymPatterns"]["yes"]
    greeting_yes = (
        rf"^\W*(?:(?:hello|hallo|हेलो|हलो|हैलो|namaste|namaskar|नमस्ते|नमस्कार){_SEP})+"
        rf"(?:(?:{_POLITE}){_SEP})*(?:{_YES}|जी|ji|jee)(?:{_SEP}(?:{_YES}|{_POLITE}|boliye|बोलिए))*\W*$"
    )
    if greeting_yes not in patterns:
        patterns.append(greeting_yes)
    edge = next((e for e in result["edges"] if e["id"] == "e_gate_next"), None)
    safe = PREFIX + "identity_known"
    if not edge or edge.get("to") not in ("n_is_identity_denied", safe):
        raise ValueError("Reviewed identity fallback has changed")
    edge["to"] = safe
    _put(result["nodes"], {"id": safe, "kind": "condition", "label": "Identity must be established",
                           "config": {"variable": "identity_answer", "operator": "exists"}})
    _put(result["edges"], {"id": "e_haier_audit_identity_known_yes", "from": safe,
                           "to": "n_is_identity_denied", "label": "true"})
    _put(result["edges"], {"id": "e_haier_audit_identity_known_no", "from": safe,
                           "to": "m_n_v2_end_unverified", "label": "false"})


def apply_call_audit_repair(definition: dict) -> dict:
    """Return the reviewed workflow repaired; fail closed on changed contracts."""
    if definition.get("id") != WORKFLOW_ID or definition.get("bot_id", BOT_ID) != BOT_ID:
        raise ValueError("This repair is restricted to the reviewed Haier workflow")
    already = next((n for n in definition.get("nodes", []) if n["id"] == PREFIX + "identity_known"), None)
    result = deepcopy(definition) if already else apply_service_need_repair(definition)
    # Bare warranty names denote neither a dispute nor a request for a visit.
    neutral = {"warranty", "वारंटी", "guarantee", "गारंटी"}
    for edge in result["edges"]:
        if edge["id"].startswith("e_warranty_") and edge.get("to") == "m_warranty_claimed":
            edge["label"] = "/".join(t for t in edge["label"].split("/") if t.strip().lower() not in neutral)
    _contact_repairs(result)
    _status_repairs(result)
    _service_repairs(result)
    _refusal_timing_repairs(result)
    _redispatch_repairs(result)
    _identity_repair(result)
    return result
