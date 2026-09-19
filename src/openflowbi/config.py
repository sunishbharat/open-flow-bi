from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Epoch: an unset incremental start means "extract everything" on first run,
# same as M3/M4's plain full pass.
DEFAULT_INCREMENTAL_START = "1970-01-01T00:00:00.000+0000"


class Settings(BaseSettings):
    """CLI-level settings, resolved from FLOWBI_* env vars and .env.

    Separate from dlt's own secrets.toml resolution used inside pipeline/run.py —
    this only covers what the CLI itself needs before a pipeline is constructed.
    """

    model_config = SettingsConfigDict(env_prefix="FLOWBI_", env_file=".env", extra="ignore")

    jira_base_url: str
    jira_email: str | None = None
    jira_api_token: str | None = None
    jira_pat: str | None = None
    jira_project: str | None = None
    jira_incremental_start: str = DEFAULT_INCREMENTAL_START
    # Phase 2 (docs/phase2-postgres-design.md §4.4): the single user-facing
    # surface for the Postgres DSN, handed to dlt explicitly rather than
    # letting dlt's own secrets.toml resolution be a second source of truth.
    postgres_dsn: str | None = None

    @field_validator("jira_incremental_start", mode="before")
    @classmethod
    def _blank_means_default(cls, value: str | None) -> str:
        # .env.example documents this field as "optional" - pydantic-settings
        # treats a present-but-empty FLOWBI_JIRA_INCREMENTAL_START= as an
        # explicit "" rather than unset, which then crashes pendulum.parse("")
        # downstream. An empty value must mean the same as omitting it.
        return value or DEFAULT_INCREMENTAL_START
