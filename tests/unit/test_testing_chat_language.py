"""Language following for the Studio text-chat tester."""

from backend.routers.testing import detect_chat_language


SUPPORTED = ["en-IN", "hi-IN"]


def test_hindi_script_starts_and_keeps_a_hindi_conversation():
    assert detect_chat_language("हाँ बोलिए।", "en-IN", SUPPORTED) == "hi-IN"
    assert (
        detect_chat_language(
            "भाई कॉल तुमने किया है, बताओ क्यों किया है।", "hi-IN", SUPPORTED,
        )
        == "hi-IN"
    )


def test_clear_english_sentence_switches_a_hindi_conversation_to_english():
    assert (
        detect_chat_language(
            "Please tell me why you called me.", "hi-IN", SUPPORTED,
        )
        == "en-IN"
    )


def test_one_code_switched_word_does_not_flip_the_conversation():
    assert detect_chat_language("okay", "hi-IN", SUPPORTED) == "hi-IN"
    assert detect_chat_language("haan", "en-IN", SUPPORTED) == "en-IN"


def test_romanized_hindi_sentence_switches_to_hindi():
    assert (
        detect_chat_language(
            "aapne mujhe call kyu kiya hai", "en-IN", SUPPORTED,
        )
        == "hi-IN"
    )
