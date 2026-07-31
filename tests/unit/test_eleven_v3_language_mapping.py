"""Eleven v3 language mapping: platform catalog records → offered locales.

The supported_languages catalog is the source of truth; the helper only maps
catalog records whose canonical base code is officially supported by the
model. Nothing is guessed, nothing bypasses the catalog.
"""

from backend.seeds.provider_catalog_seed import (
    _ELEVEN_V3_LANGS,
    _LEGACY_ELEVEN_V3_BARE_CODES,
    ELEVEN_V3_ISO_CODES,
    eleven_v3_platform_locales,
)


class TestElevenV3PlatformLocales:
    def test_en_us_and_en_in_map_as_separate_records(self):
        locales = eleven_v3_platform_locales([
            ("en-US", "en"), ("en-IN", "en"), ("hi-IN", "hi"),
        ])
        assert locales == ["en-US", "en-IN", "hi-IN"]

    def test_unsupported_catalog_languages_are_excluded(self):
        # Odia, Sanskrit, Konkani … exist as catalog records but are not in
        # the official Eleven v3 list.
        locales = eleven_v3_platform_locales([
            ("or-IN", "or"), ("sa-IN", "sa"), ("kok-IN", "kok"),
            ("mni-IN", "mni"), ("hi-IN", "hi"),
        ])
        assert locales == ["hi-IN"]

    def test_iso_code_falls_back_to_locale_prefix(self):
        assert eleven_v3_platform_locales([("ta-IN", None)]) == ["ta-IN"]
        assert eleven_v3_platform_locales([("xx-XX", None)]) == []

    def test_duplicates_and_blanks_are_dropped(self):
        locales = eleven_v3_platform_locales([
            ("en-US", "en"), ("en-US", "en"), ("", "en"), ("  ", None),
        ])
        assert locales == ["en-US"]

    def test_seed_constant_is_catalog_derived(self):
        # The module constant comes from the languages seed — a catalog-shaped
        # locale list with the two English records and no legacy bare codes.
        assert "en-US" in _ELEVEN_V3_LANGS
        assert "en-IN" in _ELEVEN_V3_LANGS
        assert "or-IN" not in _ELEVEN_V3_LANGS
        assert all("-" in code for code in _ELEVEN_V3_LANGS)
        assert len(_ELEVEN_V3_LANGS) == len(set(_ELEVEN_V3_LANGS))

    def test_official_iso_matrix_matches_verified_docs(self):
        # Spot checks against the official list (2026-07-30): Odia absent,
        # every base code the platform catalog relies on present.
        assert "or" not in ELEVEN_V3_ISO_CODES
        assert {"en", "hi", "bn", "mr", "gu", "ta", "te", "kn", "ml", "pa",
                "as", "ur", "ne", "sd", "es", "fr", "de", "vi"} <= ELEVEN_V3_ISO_CODES
        # The legacy row shape stays available as the guarded-conversion key.
        assert "en" in _LEGACY_ELEVEN_V3_BARE_CODES
        assert all("-" not in code for code in _LEGACY_ELEVEN_V3_BARE_CODES)
