"""Central configuration. All knobs live here; nothing else reads os.environ."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """Runtime settings, populated from environment / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="AGENTCREW_",
        extra="ignore",
    )

    # ---- Model -----------------------------------------------------------
    provider: Literal["anthropic", "openai", "gemini", "fake"] = "anthropic"
    model: str = ""
    """Empty means 'use this provider's default'. Never hardcode a model here:
    a provider-specific default would be silently carried across when the
    provider changes (an OpenAI run using a Claude model name). Resolution
    goes through the PROVIDERS registry in llm.py - see resolved_model()."""
    temperature: float = 0.0
    max_output_tokens: int = 2048
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    gemini_api_key: str | None = None
    openai_base_url: str | None = None

    # ---- Database --------------------------------------------------------
    database_path: Path = REPO_ROOT / "data" / "northstar.db"
    sql_timeout_seconds: float = 15.0
    max_result_rows: int = 500
    """Hard cap injected as LIMIT when the model omits one."""

    # ---- Agent budgets (loop control) ------------------------------------
    max_attempts_per_step: int = 3
    max_steps: int = 4
    max_llm_calls: int = 30
    max_sql_executions: int = 24
    wall_clock_seconds: float = 180.0

    # ---- Schema selection ------------------------------------------------
    max_tables_in_context: int = 8
    sample_values_per_column: int = 3
    max_distinct_for_sampling: int = 40
    """Only show sample values for low-cardinality text columns."""

    # ---- Safety ----------------------------------------------------------
    allow_write_mode: bool = False
    """When False the agent is structurally incapable of writing. Default off."""

    # ---- Observability ---------------------------------------------------
    trace_dir: Path = REPO_ROOT / "data" / "traces"
    langfuse_enabled: bool = False
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    def resolved_model(self) -> str:
        """Provider-appropriate default when the user did not override the model.

        Delegates to the PROVIDERS registry so there is exactly one place that
        knows each provider's default. Imported lazily to keep config free of
        module-level dependencies on the LLM layer.
        """
        if self.model:
            return self.model
        from agentcrew.llm import default_model

        return default_model(self.provider)


_settings: Settings | None = None


def get_settings(**overrides: object) -> Settings:
    """Process-wide settings singleton. Pass overrides in tests."""
    global _settings
    if overrides:
        return Settings(**overrides)  # type: ignore[arg-type]
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Test helper."""
    global _settings
    _settings = None
