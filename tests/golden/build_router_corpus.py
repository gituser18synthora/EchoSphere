"""Dev tool: record the router's deterministic classifiers over a utterance
corpus (every intent sample of every fixture bot, every scenario turn, plus a
multilingual signal probe list) → ``router_signals.json``.

``python -m tests.golden.build_router_corpus`` regenerates the corpus AND
re-records it; the test ``test_golden_router_signals.py`` replays it.
"""
from __future__ import annotations

import json
import pathlib

from shared.orchestration.router import (
    classify_user_signal,
    detect_consent_refusal,
    detect_do_not_call,
    detect_emergency,
    detect_hangup,
    leading_affirmation,
    looks_like_question,
)

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "router_signals.json"

PROBES = [
    # affirm / refusal / fillers, three scripts
    "haan", "haan ji", "ji haan", "theek hai", "ok ji", "bilkul", "yes yes please", "हाँ", "जी हाँ", "ठीक है",
    "nahi", "nahi nahi", "no thanks", "नहीं", "bilkul nahi", "abhi to nahi",
    "അതെ", "ശരി", "ഇല്ല", "വേണ്ട", "ஆமாம்", "சரி", "இல்லை", "வேண்டாம்", "athe", "seri", "illa",
    # collections domain
    "paise nahi hain", "abhi paise nahi hai", "no money", "cannot pay", "naukri chali gayi", "hospital mein hoon",
    "പണം ഇല്ല", "പണമില്ല", "பணம் இல்லை", "ആശുപത്രി",
    "already paid", "payment kar diya", "paise bhar diye", "हो गया", "kal pay karunga", "UPI se kar dunga",
    "അടയ്ക്കാം", "கட்டுகிறேன்", "google pay se", "ready to pay",
    "galat number", "wrong number", "mera loan nahi hai", "അമ്മയാണ്", "மனைவி பேசுறேன்",
    # call control
    "call back later", "baad mein call karo", "kal call karna", "shaam ko call karo", "abhi busy hoon", "meeting mein hoon",
    "ek minute ruko", "hold on", "wait karo", "line par raho", "kaatna mat", "don't hang up", "abhi aata hoon",
    "agent se baat karao", "customer care", "manager chahiye",
    "band karo call", "rakh do phone", "bye", "call mat karna kabhi", "do not call me again",
    # complaint / clarify / question
    "sun nahi rahe ho", "baar baar wahi baat", "samajh nahi aaya", "matlab kya", "kya kaha",
    "kitna dena hai", "कितना बाकी है", "kab tak?", "kya aap refund karoge", "is it refunded", "do you support Tally",
    "MDND kya hota hai?", "kaise pay karu", "what is the fee",
    # statements that must NOT be questions (Hinglish is/do/are/will)
    "Are maine product deliver kar diya phir bhi mera MDND mark do hai", "is baar maine deliver kar diya",
    "customer ne bola bahar rakh do", "maine bola will call you later", "main Tally use karta hoon", "Tally",
    "मैं टैली यूज़ करता हूँ।", "Busy",
    # leading affirmation
    "haan main bol raha hoon", "yes I am speaking", "हाँ हाँ मैं बोल रहा हूँ", "haan ji boliye", "haan lekin nahi",
    # emergency
    "emergency hai", "accident ho gaya",
]


def corpus() -> list[str]:
    seen: list[str] = []
    for path in sorted((HERE / "fixtures" / "definitions").glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        for intent in doc.get("intents") or []:
            for sample in intent.get("samples") or []:
                if isinstance(sample, str) and sample.strip():
                    seen.append(sample.strip())
    for path in sorted((HERE / "cases").glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        for case in doc["cases"]:
            for turn in case["turns"]:
                seen.append(turn["text"])
    seen.extend(PROBES)
    unique: list[str] = []
    known: set[str] = set()
    for item in seen:
        if item not in known:
            known.add(item)
            unique.append(item)
    return unique


def classify(text: str) -> dict:
    return {
        "signal": classify_user_signal(text),
        "question": looks_like_question(text),
        "hangup": detect_hangup(text),
        "dnc": detect_do_not_call(text),
        "emergency": detect_emergency(text),
        "consent_refusal": detect_consent_refusal(text),
        "leading_affirm": leading_affirmation(text),
    }


def main() -> None:
    rows = [{"text": t, **classify(t)} for t in corpus()]
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=0), encoding="utf-8")
    from collections import Counter
    print(len(rows), "utterances;", Counter(r["signal"] for r in rows).most_common())


if __name__ == "__main__":
    main()
