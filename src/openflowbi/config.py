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
    # "cloud" | "server": declare the deployment instead of probing /serverInfo
    # unauthenticated first — needed behind mTLS, where that probe is reset.
    jira_deployment: str | None = None
    # mTLS client certificate + private key, each a base64-encoded PEM (how a key pair
    # survives `cf set-env`). Both or neither; see openflowbi.cloud.tls.
    jira_client_cert_b64: str | None = None
    jira_client_key_b64: str | None = None
    jira_project: str | None = None
    # Phase 2 (docs/phase2-postgres-design.md §7): stable slug, part of every
    # Postgres primary key. Optional — deployment.derive_instance_id() falls
    # back to a slug of the base URL's host when unset.
    jira_instance_id: str | None = None
    jira_incremental_start: str = DEFAULT_INCREMENTAL_START
    # Phase 2 (docs/phase2-postgres-design.md §4.4): the single user-facing
    # surface for the Postgres DSN, handed to dlt explicitly rather than
    # letting dlt's own secrets.toml resolution be a second source of truth.
    postgres_dsn: str | None = None

    @field_validator(
        "jira_deployment", "jira_client_cert_b64", "jira_client_key_b64", mode="before"
    )
    @classmethod
    def _blank_means_unset(cls, value: str | None) -> str | None:
        # Same present-but-empty trap as jira_incremental_start below: `FLOWBI_X=` in .env
        # arrives as "" and must mean "not configured", not an invalid value.
        return value or None

    @field_validator("jira_incremental_start", mode="before")
    @classmethod
    def _blank_means_default(cls, value: str | None) -> str:
        # .env.example documents this field as "optional" - pydantic-settings
        # treats a present-but-empty FLOWBI_JIRA_INCREMENTAL_START= as an
        # explicit "" rather than unset, which then crashes pendulum.parse("")
        # downstream. An empty value must mean the same as omitting it.
        return value or DEFAULT_INCREMENTAL_START
