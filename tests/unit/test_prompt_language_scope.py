"""Prompt variants may only add active languages assigned to their tenant."""

import pytest

from backend.routers import prompts
from shared.errors import ApiError


def variant(code: str):
    return prompts.VariantPayload(language=code, content="hello")


def test_new_unassigned_prompt_language_is_rejected(monkeypatch):
    monkeypatch.setattr(
        prompts, "tenant_allowed_language_codes", lambda *_args, **_kwargs: {"en-IN", "hi-IN"}
    )

    with pytest.raises(ApiError, match="not assigned"):
        prompts._validate_variant_languages(
            object(), "tn-1", [variant("en-IN"), variant("mr-IN")]
        )


def test_historical_unassigned_variant_can_be_retained_or_removed(monkeypatch):
    monkeypatch.setattr(
        prompts, "tenant_allowed_language_codes", lambda *_args, **_kwargs: {"en-IN"}
    )

    prompts._validate_variant_languages(
        object(),
        "tn-1",
        [variant("en-IN"), variant("old-XX")],
        previous_languages={"en-IN", "old-XX"},
    )
    prompts._validate_variant_languages(
        object(),
        "tn-1",
        [variant("en-IN")],
        previous_languages={"en-IN", "old-XX"},
    )


def test_prompt_requires_one_unique_language(monkeypatch):
    monkeypatch.setattr(
        prompts, "tenant_allowed_language_codes", lambda *_args, **_kwargs: {"en-IN"}
    )

    with pytest.raises(ApiError, match="At least one"):
        prompts._validate_variant_languages(object(), "tn-1", [])
    with pytest.raises(ApiError, match="only one variant"):
        prompts._validate_variant_languages(
            object(), "tn-1", [variant("en-IN"), variant("en-IN")]
        )
