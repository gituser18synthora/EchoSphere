"""Foreign-script letters leaked by the LLM never reach the TTS voice."""

from shared.audio.text import strip_foreign_scripts


def test_malayalam_reply_keeps_malayalam_latin_devanagari_and_digits():
    text = "രണ്ട് ആയിരം രൂപ U P I വഴി, ई-डास account XX0923, 25 ദിവസം."
    assert strip_foreign_scripts(text, "ml-IN") == (text, {})


def test_malayalam_reply_drops_cyrillic_armenian_cjk_arabic():
    text = "അക്കൗണ്ട് XX0923 бойынша രണ്ട് հազար രൂപ; 具体മായ ഒരു തീയതി; دوهായിരം"
    cleaned, removed = strip_foreign_scripts(text, "ml-IN")
    assert "бойынша" not in cleaned and "հազար" not in cleaned
    assert "具体" not in cleaned and "دوه" not in cleaned
    assert "രണ്ട്" in cleaned and "XX0923" in cleaned and "തീയതി" in cleaned
    assert removed == {"CYRILLIC": 7, "ARMENIAN": 5, "CJK": 2, "ARABIC": 3}


def test_hindi_reply_keeps_devanagari_and_latin_drops_telugu():
    cleaned, removed = strip_foreign_scripts("आपका amount दो హजार रुपये है", "hi-IN")
    assert "హ" not in cleaned and "आपका amount" in cleaned
    assert removed == {"TELUGU": 1}


def test_english_reply_keeps_hindi_brand_names():
    text = "Thanks, Gaurav — I am Aditya from ई-डास."
    assert strip_foreign_scripts(text, "en-IN") == (text, {})


def test_unknown_language_and_empty_are_untouched():
    assert strip_foreign_scripts("бойынша", None) == ("бойынша", {})
    assert strip_foreign_scripts("бойынша", "xx-YY") == ("бойынша", {})
    assert strip_foreign_scripts("", "ml-IN") == ("", {})


def test_never_strips_down_to_nothing_speakable():
    # A reply made only of foreign letters is left alone rather than emptied.
    assert strip_foreign_scripts("бойынша", "ml-IN") == ("бойынша", {})
