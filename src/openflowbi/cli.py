import itertools
from datetime import UTC, datetime
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Annotated

import pyarrow as pa
import pyarrow.parquet as pq
import requests
import structlog
import typer
from pandera.errors import SchemaError
from rich.console import Console
from rich.table import Table

from openflowbi.config import Settings
from openflowbi.jira import deployment, flatten
from openflowbi.jira import fields as fields_mod
from openflowbi.jira.deployment import DeploymentProfile
from openflowbi.logging import configure_logging
from openflowbi.ops import sync_run as sync_run_mod
from openflowbi.pipeline import run as pipeline_run
from openflowbi.quality import checks, sql_checks

logger = structlog.get_logger(__name__)

app = typer.Typer(help="flowbi — Jira flow-metrics extractor")
extract_app = typer.Typer(help="Extract Jira data")
quality_app = typer.Typer(help="Validate extracted Parquet against pandera contracts")
app.add_typer(extract_app, name="extract")
app.add_typer(quality_app, name="quality")
console = Console()

# Table name -> its pandera contract (quality/checks.py). Extend this
# alongside jira_source's resources, not with a new hand-rolled writer.
TABLE_VALIDATORS = {
    "issues": checks.validate_issues,
    "issue_changelog": checks.validate_changelog,
}

configure_logging()

# Rule 6 (CLAUDE.md): every command that walks Jira data honours --limit — a
# debug run must never walk a whole project by accident. Defined once, reused
# by every extract subcommand so it can't be forgotten on a new one.
LimitOption = Annotated[
    int,
    typer.Option("--limit", help="Max rows to process - never walk a project by accident"),
]

DestinationOption = Annotated[
    str,
    typer.Option("--destination", help="filesystem (default, Parquet under out/) or postgres"),
]


def _resolve_profile() -> DeploymentProfile:
    settings = Settings()  # type: ignore[call-arg]  # required fields resolved from env at runtime
    return deployment.detect(
        settings.jira_base_url,
        email=settings.jira_email,
        api_token=settings.jira_api_token,
        pat=settings.jira_pat,
        instance_id=settings.jira_instance_id,
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
    # Calls fields.fetch() + flatten.fields() directly rather than going
    # through jira_source()'s dlt-resource wrapper: this preview never
    # touches a dlt destination (M2 note above), and dlt's DltResource.
    # __iter__ always spins up a ManagedPipeIterator worker thread even for
    # a single already-fetched HTTP response - unnecessary weight for a
    # terminal preview, and one that pipeline_run.run() (used by the other
    # extract commands) properly tears down but bare iteration does not.
    raw_fields = fields_mod.fetch(profile.base_url, profile.auth)
    rows = list(itertools.islice(flatten.fields(raw_fields, profile.instance_id), limit))

    table = Table(title="Jira fields")
    table.add_column("id")
    table.add_column("name")
    table.add_column("schema_type")
    table.add_column("custom")
    for row in rows:
        table.add_row(row["field_id"], row["name"], str(row["schema_type"]), str(row["custom"]))
    console.print(table)


@extract_app.command("issues")
def extract_issues(limit: LimitOption = 20, destination: DestinationOption = "filesystem") -> None:
    """Extract issues to the filesystem (Parquet under out/) or Postgres destination.

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
        destination=destination,
        postgres_dsn=settings.postgres_dsn,
    )
    console.print(info)


@extract_app.command("changelog")
def extract_changelog(
    limit: LimitOption = 20, destination: DestinationOption = "filesystem"
) -> None:
    """Extract issue changelogs (3-tier: expand -> bulkfetch -> per-issue)."""
    settings = Settings()  # type: ignore[call-arg]  # required fields resolved from env at runtime
    profile = _resolve_profile()
    info = pipeline_run.run(
        profile,
        project=settings.jira_project,
        limit=limit,
        resources=("issue_changelog",),
        destination=destination,
        postgres_dsn=settings.postgres_dsn,
    )
    console.print(info)


@quality_app.command("check")
def quality_check(
    table: Annotated[str, typer.Argument(help=f"one of {sorted(TABLE_VALIDATORS)}")],
    out_dir: Annotated[
        Path, typer.Option(help="Root of the filesystem destination")
    ] = Path("out"),
    destination: DestinationOption = "filesystem",
) -> None:
    """Validate a table against its contract.

    filesystem (default): pandera row-shape check against Parquet under
    out_dir. postgres: whole-table SQL invariants (quality/sql_checks.py) -
    pandera on a sample can't answer "is this unique across the whole table"
    the way the database can (docs/phase2-postgres-design.md §9). Either way
    the verdict is recorded as a flowbi_ops.sync_run row.
    """
    if table not in TABLE_VALIDATORS:
        raise typer.BadParameter(f"table must be one of {sorted(TABLE_VALIDATORS)}")

    if destination == "postgres":
        _quality_check_postgres(table)
        return

    files = sorted((out_dir / "jira_raw" / table).glob("*.parquet"))
    if not files:
        console.print(f"[yellow]No Parquet files found for {table} under {out_dir}[/yellow]")
        raise typer.Exit(code=1)

    combined = pa.concat_tables([pq.read_table(f) for f in files])
    try:
        TABLE_VALIDATORS[table](combined)
    except SchemaError as exc:
        console.print(f"[red]FAILED[/red] {table}: {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]OK[/green] {table}: {combined.num_rows} rows, {len(files)} file(s)")


def _quality_check_postgres(table: str) -> None:
    settings = Settings()  # type: ignore[call-arg]  # required fields resolved from env at runtime
    if not settings.postgres_dsn:
        raise typer.BadParameter("FLOWBI_POSTGRES_DSN is required for --destination postgres")
    profile = _resolve_profile()

    started_at = datetime.now(UTC)
    result = sql_checks.run_checks(settings.postgres_dsn, table)
    finished_at = datetime.now(UTC)

    result_table = Table(title=f"SQL quality checks - {table}")
    result_table.add_column("check")
    result_table.add_column("kind")
    result_table.add_column("count")
    for name, count in result.blocking.items():
        result_table.add_row(name, "blocking", str(count))
    for name, count in result.alerting.items():
        result_table.add_row(name, "alerting", str(count))
    console.print(result_table)

    status = "succeeded" if result.passed else "quality_failed"
    error = f"blocking checks failed: {', '.join(result.failing)}" if result.failing else None

    try:
        sync_run_mod.record(
            settings.postgres_dsn,
            instance_id=profile.instance_id,
            project=settings.jira_project,
            mode=sql_checks.TABLE_MODE[table],
            pipeline_name=f"quality-check-{table}",
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            error=error,
        )
    except Exception:  # noqa: BLE001 - best-effort bookkeeping, mirrors pipeline/run.py's _mark_dirty
        logger.warning("sync_run_write_failed", table=table, exc_info=True)

    if not result.passed:
        console.print(f"[red]FAILED[/red] {table}: {', '.join(result.failing)}")
        raise typer.Exit(code=1)
    console.print(f"[green]OK[/green] {table}: all blocking checks returned 0")


if __name__ == "__main__":
    app()
