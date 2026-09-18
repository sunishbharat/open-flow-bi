import itertools
from importlib.metadata import version as _pkg_version
from typing import Annotated

import requests
import typer
from rich.console import Console
from rich.table import Table

from openflowbi.config import Settings
from openflowbi.jira import deployment
from openflowbi.jira.deployment import DeploymentProfile
from openflowbi.logging import configure_logging
from openflowbi.pipeline import run as pipeline_run
from openflowbi.pipeline.source import jira_source

app = typer.Typer(help="flowbi — Jira flow-metrics extractor")
extract_app = typer.Typer(help="Extract Jira data")
app.add_typer(extract_app, name="extract")
console = Console()

configure_logging()

# Rule 6 (CLAUDE.md): every command that walks Jira data honours --limit — a
# debug run must never walk a whole project by accident. Defined once, reused
# by every extract subcommand so it can't be forgotten on a new one.
LimitOption = Annotated[
    int,
    typer.Option("--limit", help="Max rows to process - never walk a project by accident"),
]


def _resolve_profile() -> DeploymentProfile:
    settings = Settings()  # type: ignore[call-arg]  # required fields resolved from env at runtime
    return deployment.detect(
        settings.jira_base_url,
        email=settings.jira_email,
        api_token=settings.jira_api_token,
        pat=settings.jira_pat,
    )


@app.command()
def version() -> None:
    """Print the installed flowbi version."""
    typer.echo(_pkg_version("open-flow-bi"))


@app.command()
def doctor() -> None:
    """Detect the Jira deployment and report account timezone + rate-limit budget."""
    profile = _resolve_profile()

    table = Table(title="flowbi doctor")
    table.add_column("Check")
    table.add_column("Value")
    table.add_row("Deployment", "Cloud" if profile.is_cloud else "Server/DC")
    table.add_row("Version", profile.version)
    table.add_row("Base URL", profile.base_url)

    try:
        tz = deployment.account_timezone(profile.base_url, profile.auth)
        table.add_row("Account timezone", tz)
    except requests.exceptions.RequestException:
        table.add_row("Account timezone", "unavailable (check credentials)")

    if profile.is_cloud:
        table.add_row("Rate-limit budget", "reported per-request via response headers")
    else:
        table.add_row("Rate-limit budget", "n/a - Server/DC has no budget headers")

    console.print(table)


SinkOption = Annotated[str, typer.Option(help="table (terminal preview) - filesystem lands in M3")]


@extract_app.command("fields")
def extract_fields(sink: SinkOption = "table", limit: LimitOption = 20) -> None:
    """Preview Jira field definitions. Does not touch a dlt destination (see M2 notes)."""
    if sink != "table":
        raise typer.BadParameter("Only --sink table is supported until M3 adds a filesystem sink")

    profile = _resolve_profile()
    source = jira_source(profile)
    rows = list(itertools.islice(source.fields, limit))

    table = Table(title="Jira fields")
    table.add_column("id")
    table.add_column("name")
    table.add_column("schema_type")
    table.add_column("custom")
    for row in rows:
        table.add_row(row["field_id"], row["name"], str(row["schema_type"]), str(row["custom"]))
    console.print(table)


@extract_app.command("issues")
def extract_issues(limit: LimitOption = 20) -> None:
    """Extract issues to the filesystem destination as Parquet under out/.

    Incrementally: only issues updated since the last successful run's
    watermark are fetched (dlt pipeline state) - a --limit-truncated run only
    ever advances that watermark to the oldest-updated issue it actually
    fetched, so a later unlimited run still picks up whatever it skipped.
    """
    settings = Settings()  # type: ignore[call-arg]  # required fields resolved from env at runtime
    profile = _resolve_profile()
    info = pipeline_run.run(
        profile,
        project=settings.jira_project,
        limit=limit,
        incremental_start=settings.jira_incremental_start,
    )
    console.print(info)


@extract_app.command("changelog")
def extract_changelog(limit: LimitOption = 20) -> None:
    """Extract issue changelogs (3-tier: expand -> bulkfetch -> per-issue) as Parquet under out/."""
    settings = Settings()  # type: ignore[call-arg]  # required fields resolved from env at runtime
    profile = _resolve_profile()
    info = pipeline_run.run(
        profile, project=settings.jira_project, limit=limit, resources=("issue_changelog",)
    )
    console.print(info)


if __name__ == "__main__":
    app()
