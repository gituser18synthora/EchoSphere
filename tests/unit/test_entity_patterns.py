"""Structured canonical matching for entities (``synonymPatterns``).

Spoken answers vary word order and slip object words between the recipient
and the verb; a per-canonical regex captures that structure once, tried in
authored order (so negative canonicals can be listed first), before the flat
surface lexicon fallback."""

from shared.orchestration.entity_extractor import extract_entity

RECIPIENT = {
    "name": "recipient",
    "dataType": "text",
    "synonymPatterns": {
        "not handed over": [r"kisi\s*ko\s*nahi\s*diya|किसी\s*को\s*नहीं\s*दिया"],
        "mother": [
            r"(?:mummy|maa|माँ|मम्मी)\s*(?:ko|को|ke\s*paas|के\s*पास)\s*"
            r"(?:(?!nahi|नहीं)\S+\s+){0,3}?(?:de\s*diya|diya|दे\s*दिया|दिया)",
        ],
        "guard / security": [r"(?:guard|गार्ड)\s*(?:ko|को)\s*(?:\S+\s+){0,3}?(?:de\s*diya|diya|दे\s*दिया|दिया)"],
    },
    # Literal fallback still applies when no pattern fires.
    "synonyms": {"left at door": ["darwaze par rakh diya"]},
}


def _value(text, entity=RECIPIENT):
    return extract_entity(text, entity).get("value")


class TestSynonymPatterns:
    def test_order_tolerant_handover_phrases(self):
        assert _value("maine maa ko de diya tha") == "mother"
        assert _value("उनके माँ को प्रोडक्ट दिया") == "mother"
        assert _value("mummy ke paas de diya") == "mother"
        assert _value("guard ko order de diya") == "guard / security"

    def test_instruction_and_negation_are_not_handovers(self):
        assert _value("customer ne bola mummy ko de do") is None
        assert _value("maa ko nahi diya") is None
        assert _value("माँ को नहीं दिया था") is None

    def test_authored_order_lets_negative_canonicals_win(self):
        assert _value("kisi ko nahi diya, maa ko bhi nahi diya") == "not handed over"

    def test_lexicon_fallback_when_no_pattern_matches(self):
        assert _value("darwaze par rakh diya") == "left at door"
        assert extract_entity("darwaze par rakh diya", RECIPIENT)["method"] == "lexicon"
        assert extract_entity("maa ko de diya", RECIPIENT)["method"] == "lexicon_pattern"

    def test_invalid_pattern_is_ignored_not_fatal(self):
        broken = {"name": "x", "dataType": "text",
                  "synonymPatterns": {"bad": [r"("], "ok": [r"theek"]}}
        assert _value("sab theek hai", broken) == "ok"

    def test_explicit_regex_pattern_still_wins_over_patterns(self):
        entity = {"name": "n", "dataType": "text", "regexPattern": r"\d{4}",
                  "synonymPatterns": {"mother": [r"maa"]}}
        assert _value("maa ne 1234 bola", entity) == "1234"


class TestOrderedRegexPatterns:
    ENTITY = {"name": "guard_name", "dataType": "text", "regexPatterns": [
        r"(?:guard|गार्ड)\s*(?:ka|का)?\s*(?:naam|नाम)\s*(?:tha|था|hai|है)?\s*(?!nahi|नहीं)([A-Za-z\u0900-\u097F]{2,24})",
        r"(?:uska|उसका)\s*(?:naam|नाम)\s*(?:tha|था|hai|है)?\s*([A-Za-z\u0900-\u097F]{2,24})",
        r"^\W*([A-Za-z\u0900-\u097F]{2,24})\W*$",
    ], "synonyms": {"not known": ["yaad nahi", "याद नहीं"]}}

    def test_each_pattern_captures_its_own_group(self):
        assert extract_entity("guard ka naam Ramesh tha", self.ENTITY)["value"] == "Ramesh"
        assert extract_entity("उसका नाम था राजू", self.ENTITY)["value"] == "राजू"
        assert extract_entity("Raju", self.ENTITY)["value"] == "Raju"
        assert extract_entity("guard ka naam Ramesh tha", self.ENTITY)["method"] == "regex"

    def test_lexicon_still_applies_when_no_pattern_matches(self):
        assert extract_entity("mujhe yaad nahi", self.ENTITY)["value"] == "not known"

    def test_whole_sentence_is_never_the_value(self):
        assert extract_entity("maine bataya na abhi", self.ENTITY)["value"] is None
