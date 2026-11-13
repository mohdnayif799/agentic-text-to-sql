"""Central configuration. All knobs live here; nothing else reads os.environ."""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
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
    database_url: SecretStr | None = None
    """Self-hosted mode only: a PostgreSQL URL (postgresql://user:pass@host/db)
    the app answers from instead of NorthStar. Never set on the public demo,
    and never accepted from the browser. Connect as a read-only role
    (docs/SELF_HOSTED.md); the app also forces read-only transactions.

    A SecretStr because the URL holds the database password: printing or
    logging a Settings object (pytest does it in an assertion diff, as D15
    records for an API key) shows ********** instead. Only
    database_url_value() returns the real text, and only the engine uses it."""
    database_path: Path = REPO_ROOT / "data" / "northstar.db"
    sql_timeout_seconds: float = 15.0
    max_result_rows: int = 500
    """Hard cap injected as LIMIT when the model omits one."""

    # ---- Architecture (eval protocol v2) ---------------------------------
    arch: Literal["baseline", "v1", "router", "full"] = "full"
    """Which control flow answers a question: baseline (A), v1 (B, the
    original agent), router (C) or full (D, the default: the architecture
    that did best on the frozen holdouts). See agentcrew/archs and
    eval/results/phase5_report.md."""
    today: date | None = None
    """Pinned "today". None means the real current date; evaluation runs pin
    it (eval/run_config.yaml) so prompts and the response cache are stable."""

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

    # ---- Cost and abuse guard (agentcrew/caps.py, app.py) -----------------
    daily_run_cap: int = 150
    """Runs per UTC day on the server's key, across every session. 0 = no cap.
    The per-session allowance in app.py is reset by a page reload; this is
    the ceiling on total spend. Resets on a cold start (no store)."""
    daily_llm_call_cap: int = 1000
    """Model calls per UTC day on the server's key. 0 = no cap. A run makes
    about 2 (direct) to 7 (escalated) calls, so this is the finer bound."""
    session_sql_run_cap: int = 30
    """SQL editor runs per browser session (agentcrew/sql_editor.py). 0 = no
    cap. No model call is made, so it applies whichever key the visitor uses."""
    daily_sql_run_cap: int = 1000
    """SQL editor runs per UTC day across every session. 0 = no cap. Its own
    counter, separate from daily_run_cap: editor runs make no model call and
    must not use up the demo's agent runs."""
    daily_upload_cap: int = 50
    """Uploaded datasets per UTC day across every session. 0 = no cap. Each
    upload also spends model calls on drafting descriptions, which the daily
    call cap counts when the server key pays."""

    # ---- Bring your own data (agentcrew/datasets) ----------------------------
    uploads_enabled: bool = True
    """Whether the app offers CSV, Excel and SQLite uploads."""
    upload_dir: Path = Path(tempfile.gettempdir()) / "agentcrew" / "uploads"
    """Where converted uploads live. On Cloud Run this is in-memory tmpfs, so
    every byte here counts against the instance's memory limit."""
    max_upload_mb: float = 10.0
    """Largest CSV or SQLite file accepted, in MB."""
    max_excel_upload_mb: float = 10.0
    """Largest Excel file accepted, in MB. Separate because an .xlsx file is
    zipped XML and takes far more memory to read than its size suggests."""
    max_converted_mb: float = 50.0
    """Largest SQLite file one upload may become, in MB. The conversion stops
    when the file reaches it (or the room left in the store, if less)."""
    upload_store_max_mb: float = 150.0
    """All converted datasets together, in MB. A new upload that would exceed
    it is refused until others expire."""
    dataset_ttl_seconds: int = 900
    """A dataset not used for this long is deleted, whether or not its
    browser tab is still open."""
    max_upload_rows: int = 500_000
    """Rows per table in an upload."""
    max_upload_tables: int = 30
    """Tables (or Excel sheets) per upload."""
    max_upload_columns: int = 200
    """Columns per table in an upload."""
    descriptions_path: Path | None = None
    """Self-hosted only: a YAML file the table and column descriptions of
    AGENTCREW_DATABASE_URL are loaded from and saved to, so they are drafted
    once rather than on every restart. Unset: descriptions live in memory."""

    idle_timeout_seconds: int = 600
    """A browser session with no activity for this long is ended, in the
    browser and on the server, so an idle tab cannot keep the Cloud Run
    instance alive. The deployed demo scales to zero only when no session
    holds a connection."""

    # ---- Observability ---------------------------------------------------
    trace_dir: Path = REPO_ROOT / "data" / "traces"
    langfuse_enabled: bool | None = None
    """None (the default): Langfuse is on exactly when both keys below are
    set. False switches it off while keeping the keys. True is the same as
    None, plus a warning when the keys are missing (tracer.langfuse_wanted)."""
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"
    trace_content: bool = False
    """Off by default and off on the public demo. When on, a Langfuse trace
    also records the question text and the final SQL, so a self-hosted
    operator can see real failures. Result rows and uploaded data are never
    recorded, with this on or off."""

    def database_url_value(self) -> str | None:
        """The real database URL, password included. For the engine only:
        never log, render or put it in an exception message."""
        return self.database_url.get_secret_value() if self.database_url else None

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
