"""STT auto-detect policy: explicit choice > derived multilingual default."""

from shared.providers.stt_language_policy import (
    AUTO_DETECT_KEY,
    derived_auto_detect_default,
    effective_languages,
    explicit_auto_detect,
    resolve_auto_detect_language,
    stt_language_mode,
)


class TestEffectiveLanguages:
    def test_bot_languages_win_over_tenant_defaults(self):
        assert effective_languages(["hi-IN"], ["en-IN", "hi-IN"]) == ["hi-IN"]

    def test_empty_bot_list_inherits_tenant_defaults(self):
        assert effective_languages([], ["en-IN", "hi-IN"]) == ["en-IN", "hi-IN"]
        assert effective_languages(None, ["ta-IN"]) == ["ta-IN"]

    def test_blanks_and_duplicates_are_dropped_order_kept(self):
        assert effective_languages(["hi-IN", "", " hi-IN ", "en-IN", None]) == ["hi-IN", "en-IN"]


class TestDerivedDefault:
    def test_single_language_pins(self):
        assert derived_auto_detect_default(["en-IN"]) is False
        assert derived_auto_detect_default([]) is False

    def test_multilingual_detects(self):
        assert derived_auto_detect_default(["en-IN", "hi-IN"]) is True
        assert derived_auto_detect_default(["hi-IN", "en-IN", "ta-IN", "ml-IN"]) is True


class TestExplicitValue:
    def test_only_real_booleans_count(self):
        assert explicit_auto_detect({AUTO_DETECT_KEY: True}) is True
        assert explicit_auto_detect({AUTO_DETECT_KEY: False}) is False
        assert explicit_auto_detect({AUTO_DETECT_KEY: None}) is None
        assert explicit_auto_detect({AUTO_DETECT_KEY: "true"}) is None
        assert explicit_auto_detect({}) is None
        assert explicit_auto_detect(None) is None


class TestResolve:
    def test_new_multilingual_bot_defaults_on(self):
        d = resolve_auto_detect_language({"mode": "transcribe"}, ["en-IN", "hi-IN"])
        assert d.enabled is True and d.source == "derived" and d.derived_default is True
        assert d.explicit is None
        assert d.as_dict() == {
            "value": None, "effective": True, "source": "derived",
            "derivedDefault": True, "languages": ["en-IN", "hi-IN"],
        }

    def test_single_language_bot_defaults_off(self):
        d = resolve_auto_detect_language({}, ["en-IN"])
        assert d.enabled is False and d.source == "derived"

    def test_explicit_off_is_respected_for_multilingual_bot(self):
        d = resolve_auto_detect_language({AUTO_DETECT_KEY: False}, ["en-IN", "hi-IN"])
        assert d.enabled is False and d.source == "explicit" and d.derived_default is True

    def test_explicit_on_is_respected_for_single_language_bot(self):
        d = resolve_auto_detect_language({AUTO_DETECT_KEY: True}, ["hi-IN"])
        assert d.enabled is True and d.source == "explicit" and d.derived_default is False

    def test_tenant_multilingual_defaults_apply_when_bot_has_no_languages(self):
        d = resolve_auto_detect_language({}, [], ["hi-IN", "en-IN"])
        assert d.enabled is True and d.languages == ("hi-IN", "en-IN")

    def test_bot_single_language_overrides_multilingual_tenant(self):
        d = resolve_auto_detect_language({}, ["hi-IN"], ["hi-IN", "en-IN"])
        assert d.enabled is False and d.languages == ("hi-IN",)


class TestLanguageMode:
    def test_explicit_stt_language_always_pins(self):
        d = resolve_auto_detect_language({AUTO_DETECT_KEY: True}, ["en-IN", "hi-IN"])
        assert stt_language_mode("hi-IN", d, "en-IN") == ("pinned", "hi-IN")

    def test_unknown_counts_as_blank(self):
        d = resolve_auto_detect_language({}, ["en-IN", "hi-IN"])
        assert stt_language_mode("unknown", d, "hi-IN") == ("auto", None)

    def test_auto_when_enabled(self):
        d = resolve_auto_detect_language({}, ["en-IN", "hi-IN"])
        assert stt_language_mode("", d, "hi-IN") == ("auto", None)

    def test_pinned_to_default_when_disabled(self):
        d = resolve_auto_detect_language({}, ["hi-IN"])
        assert stt_language_mode(None, d, "hi-IN") == ("pinned", "hi-IN")

    def test_disabled_without_default_language_falls_back_to_auto(self):
        d = resolve_auto_detect_language({AUTO_DETECT_KEY: False}, [])
        assert stt_language_mode("", d, "") == ("auto", None)


class TestPrecedenceTableWithMalayalam:
    """The invariant: when detection is effectively ON, the runtime must not
    pin merely because the bot has a default/primary language. Only an
    explicit fixed STT language pins."""

    LANGS = ["en-IN", "hi-IN", "ml-IN"]

    def test_no_stt_language_and_derived_true_is_auto(self):
        d = resolve_auto_detect_language({}, self.LANGS)
        assert d.enabled and d.source == "derived"
        assert stt_language_mode("", d, "ml-IN") == ("auto", None)

    def test_default_bot_language_alone_never_pins(self):
        # A default language (language_voice_map.default = ml-IN) is not an
        # STT pin: the recognizer still auto-detects.
        d = resolve_auto_detect_language({"mode": "transcribe"}, self.LANGS)
        assert stt_language_mode(None, d, "ml-IN") == ("auto", None)

    def test_explicit_stt_language_pins_regardless(self):
        d = resolve_auto_detect_language({AUTO_DETECT_KEY: True}, self.LANGS)
        assert stt_language_mode("ml-IN", d, "hi-IN") == ("pinned", "ml-IN")
        d = resolve_auto_detect_language({}, self.LANGS)
        assert stt_language_mode("ml-IN", d, "hi-IN") == ("pinned", "ml-IN")

    def test_explicit_auto_true_with_stt_language_still_pins(self):
        # Documented precedence: a genuinely selected fixed STT language is
        # the one thing that outranks auto-detect.
        d = resolve_auto_detect_language({AUTO_DETECT_KEY: True}, self.LANGS)
        assert d.enabled and d.source == "explicit"
        assert stt_language_mode("ml-IN", d, "ml-IN") == ("pinned", "ml-IN")

    def test_explicit_auto_false_pins_to_default(self):
        d = resolve_auto_detect_language({AUTO_DETECT_KEY: False}, self.LANGS)
        assert stt_language_mode("", d, "ml-IN") == ("pinned", "ml-IN")

    def test_malayalam_only_bot_pins_to_malayalam(self):
        d = resolve_auto_detect_language({}, ["ml-IN"])
        assert not d.enabled
        assert stt_language_mode("", d, "ml-IN") == ("pinned", "ml-IN")


class TestInheritanceTable:
    """Product semantics (shared/tenant_languages.py): the tenant's
    default_languages are the ENTITLEMENT a Super Admin assigns; a bot picks
    its own subset (the Overview tab only offers entitled languages, and
    languages already on a bot are retained even if later removed from the
    tenant). The bot's list is therefore the supported set; the tenant list
    only fills in for a bot that has none of its own."""

    def test_tenant_multilingual_bot_without_override_inherits(self):
        d = resolve_auto_detect_language({}, [], ["en-IN", "hi-IN", "ml-IN"])
        assert d.languages == ("en-IN", "hi-IN", "ml-IN") and d.enabled

    def test_tenant_multilingual_bot_stores_only_its_primary(self):
        # A bot explicitly restricted to one language pins, whatever the tenant offers.
        d = resolve_auto_detect_language({}, ["hi-IN"], ["en-IN", "hi-IN", "ml-IN"])
        assert d.languages == ("hi-IN",) and not d.enabled

    def test_tenant_multilingual_bot_explicitly_restricted_to_one(self):
        d = resolve_auto_detect_language({}, ["ml-IN"], ["en-IN", "hi-IN", "ml-IN"])
        assert d.languages == ("ml-IN",) and not d.enabled

    def test_tenant_single_language_bot_explicitly_multilingual(self):
        # Retained/legacy bot languages beyond the entitlement stay supported.
        d = resolve_auto_detect_language({}, ["hi-IN", "ml-IN"], ["hi-IN"])
        assert d.languages == ("hi-IN", "ml-IN") and d.enabled

    def test_both_multilingual_with_different_values_bot_wins(self):
        d = resolve_auto_detect_language({}, ["ta-IN", "ml-IN"], ["en-IN", "hi-IN"])
        assert d.languages == ("ta-IN", "ml-IN") and d.enabled

    def test_tenant_includes_malayalam_and_bot_inherits_it(self):
        assert "ml-IN" in effective_languages([], ["hi-IN", "ml-IN"])

    def test_bot_explicitly_includes_malayalam(self):
        assert effective_languages(["en-IN", "ml-IN"], ["hi-IN"]) == ["en-IN", "ml-IN"]
