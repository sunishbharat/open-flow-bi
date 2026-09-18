from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # Epoch default: an unset incremental start means "extract everything" on
    # first run, same as M3/M4's plain full pass.
    jira_incremental_start: str = "1970-01-01T00:00:00.000+0000"
