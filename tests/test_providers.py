"""Provider registry and provider/model compatibility.

These exist because the project shipped with a real defect: `Settings.model`
defaulted to a Claude model string, so selecting `provider="openai"` silently
kept `claude-sonnet-4-5-20250929` as the model. Nothing caught it, because the
defaults were duplicated between `config.resolved_model()` and a
`DEFAULT_MODELS` dict in `llm.py`.

Both now read the single `PROVIDERS` registry. These tests keep it that way.
"""

from __future__ import annotations

import pytest

from agentcrew.config import Settings
from agentcrew.llm import (
    PROVIDERS,
    SELECTABLE_PROVIDERS,
    LLMError,
    build_client,
    default_model,
)


class TestRegistry:
    def test_selectable_providers_are_real_providers(self) -> None:
        for p in SELECTABLE_PROVIDERS:
            assert p in PROVIDERS

    def test_fake_is_not_offered_in_the_ui(self) -> None:
        """`fake` is a test double; a person must never pick it by accident."""
        assert "fake" not in SELECTABLE_PROVIDERS

    @pytest.mark.parametrize("provider", sorted(PROVIDERS))
    def test_every_spec_is_complete(self, provider: str) -> None:
        spec = PROVIDERS[provider]
        assert spec.key == provider
        assert spec.label
        assert spec.models, "a provider with no models cannot be selected"

    @pytest.mark.parametrize("provider", SELECTABLE_PROVIDERS)
    def test_selectable_providers_declare_a_credential(self, provider: str) -> None:
        spec = PROVIDERS[provider]
        assert spec.key_field, "needed to route the UI key to the right field"
        assert spec.key_hint, "the UI needs placeholder text"
        assert spec.install

    def test_no_retired_gemini_models_are_offered(self) -> None:
        """Shipped once: `gemini-2.5-pro` 404'd for a new user, and
        `gemini-2.0-flash` was already shut down (2026-06-01).

        Model IDs expire. Anything listed here returns 404 and must never be a
        selectable default.
        """
        retired = {
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
            "gemini-1.5-pro",
            "gemini-1.5-flash",
            "gemini-3.1-flash-lite-preview",
            "gemini-2.5-pro",  # no longer offered to new API users
        }
        offered = set(PROVIDERS["gemini"].models)
        assert not (offered & retired), (
            f"retired Gemini model(s) still offered: {sorted(offered & retired)}"
        )

    def test_gemini_default_does_not_require_billing(self) -> None:
        """A reviewer with a free key must be able to run the project.

        The flagship pro model has no free tier, so it must not be the default.
        """
        default = PROVIDERS["gemini"].models[0]
        assert "pro" not in default, (
            f"default {default!r} has no free tier; pick a Flash model"
        )
        assert "preview" not in default, (
            f"default {default!r} is a preview model; prefer a GA model"
        )
        """Google is migrating Gemini keys from `AIza...` to `AQ....`.

        A hardcoded prefix rejected legitimate new-format keys, so the check is
        disabled for this provider rather than swapped for the new prefix -
        both formats are currently valid.
        """
        assert PROVIDERS["gemini"].key_prefixes == ()

    @pytest.mark.parametrize(
        "provider,sample",
        [
            ("gemini", "AQ.abc123def456"),
            ("gemini", "AIzaSyAbc123def456"),
        ],
    )
    def test_gemini_accepts_both_key_formats(self, provider: str, sample: str) -> None:
        spec = PROVIDERS[provider]
        assert not spec.key_prefixes or sample.startswith(spec.key_prefixes)

    @pytest.mark.parametrize("provider", SELECTABLE_PROVIDERS)
    def test_key_field_exists_on_settings(self, provider: str) -> None:
        """A typo here would silently drop the user's key."""
        assert PROVIDERS[provider].key_field in Settings.model_fields


class TestProviderModelCompatibility:
    """The defect this module was written for."""

    def test_no_provider_default_leaks_a_hardcoded_model(self) -> None:
        assert Settings().model == "", (
            "Settings.model must default to empty. A concrete default is "
            "provider-specific and gets carried across when the provider "
            "changes."
        )

    @pytest.mark.parametrize("provider", SELECTABLE_PROVIDERS)
    def test_each_provider_resolves_to_its_own_model(self, provider: str) -> None:
        resolved = Settings(provider=provider).resolved_model()
        assert resolved in PROVIDERS[provider].models

    @pytest.mark.parametrize("provider", SELECTABLE_PROVIDERS)
    def test_no_cross_provider_model_bleed(self, provider: str) -> None:
        """The exact original bug: openai must never resolve to a Claude model."""
        resolved = Settings(provider=provider).resolved_model()
        others = {
            m
            for p, spec in PROVIDERS.items()
            if p != provider
            for m in spec.models
        }
        assert resolved not in others - set(PROVIDERS[provider].models)

    @pytest.mark.parametrize("provider", sorted(PROVIDERS))
    def test_config_and_registry_agree(self, provider: str) -> None:
        """Two code paths, one answer. Prevents the duplication returning."""
        assert Settings(provider=provider).resolved_model() == default_model(provider)

    def test_explicit_model_is_respected(self) -> None:
        s = Settings(provider="gemini", model="gemini-2.5-flash")
        assert s.resolved_model() == "gemini-2.5-flash"

    def test_unknown_provider_is_rejected_by_settings(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Settings(provider="bogus")

    def test_unknown_provider_is_rejected_by_default_model(self) -> None:
        with pytest.raises(LLMError):
            default_model("bogus")


class TestEnvironmentIsolation:
    """These assertions are only meaningful if tests are hermetic.

    Guards the autouse fixture in conftest. Without it, a developer with a
    populated `.env` sees `TestCredentialIsolation` fail for reasons that have
    nothing to do with the code - and pytest prints the Settings object,
    including live API keys, into the assertion diff.
    """

    def test_no_provider_credentials_leak_from_the_environment(self) -> None:
        s = Settings()
        for provider in SELECTABLE_PROVIDERS:
            value = getattr(s, PROVIDERS[provider].key_field)
            assert value is None, (
                f"{provider} key leaked from .env or the shell: test isolation "
                "in conftest is not working"
            )

    def test_no_model_override_leaks_from_the_environment(self) -> None:
        assert Settings().model == ""

    def test_provider_default_is_not_environment_dependent(self) -> None:
        assert Settings().provider == "anthropic"


class TestCredentialIsolation:
    @pytest.mark.parametrize("provider", SELECTABLE_PROVIDERS)
    def test_setting_one_key_leaves_the_others_unset(self, provider: str) -> None:
        spec = PROVIDERS[provider]
        s = Settings(provider=provider, **{spec.key_field: "test-key-value"})
        for other in SELECTABLE_PROVIDERS:
            if other == provider:
                continue
            assert getattr(s, PROVIDERS[other].key_field) is None

    @pytest.mark.parametrize("provider", SELECTABLE_PROVIDERS)
    def test_missing_key_fails_loudly(self, provider: str) -> None:
        """No provider may fall back to an empty or shared credential."""
        with pytest.raises(LLMError):
            build_client(Settings(provider=provider))

    def test_fake_provider_needs_no_key(self) -> None:
        assert build_client(Settings(provider="fake")).name == "fake"
