"""Automatic Sarvam key terms and the saaras:v4 platform default (Phase 3).

Replaying two live calls (cv_0f224432545d MDND, cv_2d1119054a92 Haier;
2026-10-23, two passes each) through the production Sarvam path:

- saaras:v4 alone was NOT better than v3: it traded v3's Bengali/Gurmukhi/
  Malayalam finals for Odia, Santali and Gujarati ones (4–5 non-Hindi-script
  finals per pass vs 6–9) and misheard "CX support" as "share support";
- saaras:v4 with the bot's own vocabulary as key terms produced NO
  non-Hindi-script finals and fixed the domain words ("guard ko handover",
  "CX support", "location par pahuncha", "call bhi");
- adding the yes/no lexicon to the key terms produced 8 false "हाँ" / "जी हाँ" /
  "Yes." finals per pass on hums, "to" and line noise (3 without it) — so the
  automatic list leaves it out unless a bot opts in.
"""

from __future__ import annotations

from types import SimpleNamespace

from shared.bot_config import ResolvedBotConfig, _derived_stt_keyterms
from shared.config import Settings
from shared.providers.stt.sarvam_keyterms import (
    MAX_KEYTERM_CHARS,
    MAX_KEYTERMS,
    derive_keyterms,
)
from voice_runtime import pipeline as pipeline_mod
from voice_runtime.pipeline import _sarvam_keyterms_for_model, build_stt_service

WORKFLOW = {
    "nodes": [
        {"id": "n_ask_reached", "kind": "ask", "config": {"entity": {"synonyms": {
            "yes (reached the location)": ["location par pahuncha", "लोकेशन पर पहुंचा", "reached"],
            "no (did not reach)": ["location par nahi gaya", "लोकेशन पर नहीं गया"],
        }}, "alsoCapture": [{"entity": {"synonyms": {"guard / security": ["guard ko diya", "गार्ड को दिया"]}}}]}},
        {"id": "n_hub", "kind": "intent", "config": {"options": [{"label": "Technician visited"}]}},
    ],
    "edges": [
        {"from": "n_hub", "to": "a", "label": "हाँ/हाँ जी/yes/technician aaya tha"},
        {"from": "n_hub", "to": "b", "label": "नहीं/no/technician nahi aaya"},
        {"from": "n_hub", "to": "c", "label": "else"},
    ],
}


class TestDerivation:
    def test_content_words_from_options_synonyms_and_slots(self):
        terms = derive_keyterms(workflows=[WORKFLOW], slot_names=["deduction_amount"],
                                intents=[{"samples": ["mdnd issue hai"]}], languages=["hi-IN", "en-IN"])
        for expected in ("location", "लोकेशन", "guard", "गार्ड", "technician", "deduction", "amount", "mdnd"):
            assert expected in terms, expected
        # yes/no lexicon is opt-in; glue words, routing labels and short Latin never qualify
        for absent in ("हाँ", "haan", "yes", "no", "नहीं", "else", "par", "पर", "को", "ko", "tha"):
            assert absent not in terms, absent

    def test_yes_no_lexicon_only_when_opted_in(self):
        terms = derive_keyterms(workflows=[WORKFLOW], languages=["hi-IN", "en-IN"], include_yes_no=True)
        assert terms[:2] == ["haan", "ji haan"]
        assert {"हाँ", "नहीं", "yes", "no"} <= set(terms)

    def test_ranked_by_how_many_phrases_use_a_word(self):
        terms = derive_keyterms(workflows=[WORKFLOW], languages=["hi-IN"])
        assert terms.index("technician") < terms.index("guard")    # 3 phrases vs 1

    def test_within_sarvam_limits_and_deterministic(self):
        letters = "abcdefghijklmnopqrstuvwxyz"
        words = [a + b + c for a in letters[:8] for b in letters[:8] for c in letters[:4]]   # 256 words
        many = {"nodes": [], "edges": [{"label": "/".join(f"{w}word {w}term" for w in words)}]}
        terms = derive_keyterms(workflows=[many], languages=["en-IN"])
        assert len(terms) == MAX_KEYTERMS and all(len(t) <= MAX_KEYTERM_CHARS for t in terms)
        assert terms == derive_keyterms(workflows=[many], languages=["en-IN"])

    def test_nothing_to_derive_is_an_empty_list(self):
        assert derive_keyterms(workflows=[], languages=["hi-IN"]) == []


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows):
        self.rows = rows
        self.queries = 0

    def execute(self, query):
        self.queries += 1
        return _Rows(self.rows)


BOT = SimpleNamespace(id="bot-x", tenant_id="tn-x")


class TestConfigResolution:
    def test_v4_without_operator_terms_gets_derived_terms(self):
        session = _Session([(WORKFLOW["nodes"], WORKFLOW["edges"])])
        terms = _derived_stt_keyterms(session, BOT, stt_model="saaras:v4", stt_settings={}, intents=[],
                                      goal_policy={"slots": [{"name": "order_id"}]}, languages=["hi-IN"])
        assert "technician" in terms and "order" in terms

    def test_operator_terms_switch_or_v3_mean_no_derivation(self):
        session = _Session([(WORKFLOW["nodes"], WORKFLOW["edges"])])
        for model, settings in (("saaras:v4", {"keyterms": ["Zepto"]}), ("saaras:v4", {"keyterms_auto": False}),
                                ("saaras:v3", {})):
            assert _derived_stt_keyterms(session, BOT, stt_model=model, stt_settings=settings, intents=[],
                                         goal_policy={}, languages=["hi-IN"]) == []
        assert session.queries == 0                      # no DB work when nothing will be derived

    def test_a_failure_never_blocks_the_call(self):
        class _Broken:
            def execute(self, query):
                raise RuntimeError("db down")
        assert _derived_stt_keyterms(_Broken(), BOT, stt_model="saaras:v4", stt_settings={}, intents=[],
                                     goal_policy={}, languages=["hi-IN"]) == []


class _Recorder:
    def __init__(self):
        self.events = []
        self.session_id = "s"

    def add_event(self, kind, **data):
        self.events.append((kind, data))


class TestRuntime:
    def test_operator_list_wins_over_derived(self):
        rec = _Recorder()
        assert _sarvam_keyterms_for_model("saaras:v4", ["Zepto"], recorder=rec, derived=["guard"]) == ["Zepto"]
        assert rec.events[-1][1]["source"] == "configured"

    def test_derived_list_is_used_and_recorded(self):
        rec = _Recorder()
        assert _sarvam_keyterms_for_model("saaras:v4", None, recorder=rec, derived=["guard", "CX"]) == ["guard", "CX"]
        assert rec.events[-1][1] == {"model": "saaras:v4", "sent": True, "count": 2, "terms": ["guard", "CX"],
                                     "source": "derived"}

    def test_never_sent_to_v3(self):
        rec = _Recorder()
        assert _sarvam_keyterms_for_model("saaras:v3", None, recorder=rec, derived=["guard"]) == []
        assert rec.events[-1][1]["sent"] is False

    def test_derived_terms_reach_the_streaming_handshake(self, monkeypatch):
        monkeypatch.setenv("TEST_SARVAM_API_KEY", "k")
        monkeypatch.setattr(pipeline_mod.get_settings(), "sarvam_realtime_stt", False)
        config = ResolvedBotConfig(
            tenant_id="t", bot_id="b", bot_name="Test", version="1", published=True,
            language="hi-IN", languages=["hi-IN", "en-IN"],
            stt={"provider": "sarvam", "model": "saaras:v4", "language": "", "settings": {},
                 "api_key_reference": "env:TEST_SARVAM_API_KEY", "derived_keyterms": ["guard", "CX support"]},
        )
        service = build_stt_service(config, sample_rate=8000, use_provider_vad=False)
        assert service.keyterms == ["guard", "CX support"]


class TestDefault:
    def test_platform_default_stt_model_is_saaras_v4(self, monkeypatch):
        # Deployments pin the platform default in .env (STT_MODEL); the code
        # default is what a fresh deployment and a bot without a model get.
        monkeypatch.delenv("STT_MODEL", raising=False)
        assert Settings(_env_file=None).stt_model == "saaras:v4"
