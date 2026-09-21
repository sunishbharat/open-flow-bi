import getpass
import itertools
import sys
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
from openflowbi.fields import discovery, selection
from openflowbi.fields import service as fields_service
from openflowbi.jira import deployment, flatten
from openflowbi.jira import fields as fields_mod
from openflowbi.jira.deployment import DeploymentProfile
from openflowbi.logging import configure_logging
from openflowbi.ops import sync_run as sync_run_mod
from openflowbi.pipeline import run as pipeline_run
from openflowbi.quality import checks, sql_checks
from openflowbi.transform import runner as transform_runner

logger = structlog.get_logger(__name__)

# Some real Jira field names carry non-ASCII characters (e.g. Jira's built-in
# aggregate-progress fields, whose display name starts with a Greek sigma)
# that a legacy Windows console's cp1252 codepage can't encode - found live
# while printing `flowbi fields list` (UnicodeEncodeError crashed the whole
# table mid-render). Degrade to '?' instead of crashing; this only affects
# terminal display, never stored data.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

app = typer.Typer(help="flowbi — Jira flow-metrics extractor")
extract_app = typer.Typer(help="Extract Jira data")
quality_app = typer.Typer(help="Validate extracted Parquet against pandera contracts")
fields_app = typer.Typer(help="Discover Jira custom fields and their fill rates (Phase 3a)")
app.add_typer(extract_app, name="extract")
app.add_typer(quality_app, name="quality")
app.add_typer(fields_app, name="fields")
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


SampleOption = Annotated[
    int, typer.Option("--sample", help="Max issues to sample when computing fill rates")
]
ProjectOption = Annotated[
    str | None,
    typer.Option("--project", help="Scope to one project key (default: FLOWBI_JIRA_PROJECT)"),
]


def _require_postgres_dsn(settings: Settings) -> str:
    if not settings.postgres_dsn:
        raise typer.BadParameter("FLOWBI_POSTGRES_DSN is required")
    return settings.postgres_dsn


def _resolve_instance_id(settings: Settings) -> str:
    # No live Jira call: unlike _resolve_profile() (needed by commands that
    # actually talk to Jira), a command that only reads flowbi_ops/analytics
    # tables shouldn't require Jira to be reachable just to name the row.
    return settings.jira_instance_id or deployment.derive_instance_id(settings.jira_base_url)


def _cli_actor() -> str:
    # §2.1: created_by is "from the JWT, or 'cli:<os user>'" - no JWT exists
    # until 3b's admin UI, so every 3a.2 write is attributed this way.
    try:
        return f"cli:{getpass.getuser()}"
    except Exception:  # noqa: BLE001 - getuser() can fail with no controlling terminal/env
        return "cli:unknown"


@fields_app.command("discover")
def fields_discover(
    project: ProjectOption = None, sample: SampleOption = discovery.DEFAULT_SAMPLE_SIZE
) -> None:
    """Refresh flowbi_ops.field_definition from /field and recompute field_stats fill rates."""
    settings = Settings()  # type: ignore[call-arg]  # required fields resolved from env at runtime
    dsn = _require_postgres_dsn(settings)
    profile = _resolve_profile()
    result = discovery.discover(
        dsn,
        profile.instance_id,
        profile.base_url,
        profile.auth,
        project=project or settings.jira_project,
        sample_size=sample,
    )
    console.print(
        f"[green]OK[/green] {result.fields_seen} fields seen "
        f"({result.fields_disappeared} disappeared this run), "
        f"{result.fields_with_stats} field_stats rows recomputed "
        f"from {result.sampled_issues} sampled issues"
    )


@fields_app.command("list")
def fields_list(
    custom_only: Annotated[bool, typer.Option("--custom-only")] = False,
    min_fill: Annotated[float | None, typer.Option("--min-fill", help="e.g. 0.1 for 10%")] = None,
) -> None:
    """Show field_definition + field_stats, joined and sorted by fill rate."""
    settings = Settings()  # type: ignore[call-arg]  # required fields resolved from env at runtime
    dsn = _require_postgres_dsn(settings)
    instance_id = _resolve_instance_id(settings)
    rows = fields_service.list_fields(
        dsn, instance_id, custom_only=custom_only, min_fill=min_fill
    )
    if not rows:
        console.print("[yellow]No fields found - run `flowbi fields discover` first[/yellow]")
        raise typer.Exit(code=1)

    table = Table(title="Jira fields")
    table.add_column("field_id")
    table.add_column("name")
    table.add_column("type")
    table.add_column("custom")
    table.add_column("fill rate")
    table.add_column("distinct")
    table.add_column("projects")
    for row in rows:
        table.add_row(
            row.field_id,
            row.name,
            str(row.schema_type),
            "yes" if row.is_custom else "no",
            f"{row.fill_rate:.1%}" if row.fill_rate is not None else "-",
            str(row.distinct_count) if row.distinct_count is not None else "-",
            str(row.project_count) if row.project_count is not None else "-",
        )
    console.print(table)


def _resolve_and_select(
    dsn: str,
    instance_id: str,
    field_name: str,
    schema_type: str | None,
    change_kwargs: dict,
) -> int:
    """Shared body of `promote`/`demote`: resolve the field, apply one
    change, translate a domain ValueError (ambiguous name, unknown field,
    demoting something never selected, a column-name collision) into a clean
    CLI error instead of a stack trace.
    """
    try:
        resolved_schema_type, field_id = selection.resolve_field_name(
            dsn, instance_id, field_name, schema_type
        )
        return selection.save_selection(
            dsn,
            instance_id,
            [
                selection.SelectionChange(
                    field_name=field_name,
                    schema_type=resolved_schema_type,
                    field_id=field_id,
                    **change_kwargs,
                )
            ],
            actor=_cli_actor(),
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


SchemaTypeOption = Annotated[
    str | None,
    typer.Option("--schema-type", help="Disambiguate when the field name matches >1 schema type"),
]
NoteOption = Annotated[str | None, typer.Option("--note", help="Free-text audit note")]


@fields_app.command("promote")
def fields_promote(
    field_name: Annotated[str, typer.Argument(help="Field display name, e.g. 'Story Points'")],
    column: Annotated[str, typer.Option("--column", help="Target column name (snake_case)")],
    schema_type: SchemaTypeOption = None,
    target: Annotated[str, typer.Option("--target", help="'column' or 'bridge_table'")] = "column",
    note: NoteOption = None,
) -> None:
    """Promote a field: select it for a real column/bridge table in analytics.issue.

    Writes a new field_selection version (§2.1: append-only, never in place).
    Nothing is rebuilt yet - analytics.issue doesn't exist until 3a.4.
    """
    settings = Settings()  # type: ignore[call-arg]  # required fields resolved from env at runtime
    dsn = _require_postgres_dsn(settings)
    instance_id = _resolve_instance_id(settings)
    version = _resolve_and_select(
        dsn,
        instance_id,
        field_name,
        schema_type,
        {"promote": True, "column_name": column, "target": target, "note": note},
    )
    console.print(f"[green]OK[/green] version {version}: promoted {field_name!r} -> {column!r}")


@fields_app.command("demote")
def fields_demote(
    field_name: Annotated[str, typer.Argument(help="Field display name")],
    schema_type: SchemaTypeOption = None,
    note: NoteOption = None,
) -> None:
    """Demote a field: stop treating it as selected.

    Never drops the underlying column (design doc §10 rule 1) - just stamps
    deprecated_at in a new version. Fails clearly if the field was never
    promoted in the first place.
    """
    settings = Settings()  # type: ignore[call-arg]  # required fields resolved from env at runtime
    dsn = _require_postgres_dsn(settings)
    instance_id = _resolve_instance_id(settings)
    version = _resolve_and_select(
        dsn, instance_id, field_name, schema_type, {"promote": False, "note": note}
    )
    console.print(f"[green]OK[/green] version {version}: demoted {field_name!r}")


@fields_app.command("export")
def fields_export(
    version: Annotated[int | None, typer.Option("--version", help="Default: latest")] = None,
) -> None:
    """Print the field selection as YAML to stdout (redirect to a file yourself)."""
    settings = Settings()  # type: ignore[call-arg]  # required fields resolved from env at runtime
    dsn = _require_postgres_dsn(settings)
    instance_id = _resolve_instance_id(settings)
    text = selection.export_yaml(dsn, instance_id, version=version)
    # typer.echo, not console.print: this output is meant to be redirected to
    # a file (README: `flowbi fields export > config/fields.yml`) - rich
    # would wrap it in box-drawing characters that aren't valid YAML.
    typer.echo(text, nl=False)


@fields_app.command("import")
def fields_import(
    path: Annotated[Path, typer.Argument(help="A YAML file from `flowbi fields export`")],
) -> None:
    """Import a field-selection YAML file, writing it as a new version."""
    settings = Settings()  # type: ignore[call-arg]  # required fields resolved from env at runtime
    dsn = _require_postgres_dsn(settings)
    instance_id = _resolve_instance_id(settings)
    text = path.read_text(encoding="utf-8")
    try:
        version = selection.import_yaml(dsn, instance_id, text, actor=_cli_actor())
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"[green]OK[/green] imported as version {version}")


@fields_app.command("request-rebuild")
def fields_request_rebuild(
    project: ProjectOption = None,
    version: Annotated[
        int | None, typer.Option("--version", help="field_selection version - default: latest")
    ] = None,
) -> None:
    """Queue a full rebuild of promoted columns (Phase 3a.5).

    Writes a flowbi_ops.rebuild_request row and returns immediately - a
    rebuild takes minutes, this call doesn't. `flowbi transform` drains the
    queue. This is the CLI's stand-in for 3b's "Save & rebuild" button.
    """
    settings = Settings()  # type: ignore[call-arg]  # required fields resolved from env at runtime
    dsn = _require_postgres_dsn(settings)
    instance_id = _resolve_instance_id(settings)
    try:
        request_id = fields_service.request_rebuild(
            dsn, instance_id, scope=project, actor=_cli_actor(), version=version
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"[green]OK[/green] queued rebuild request {request_id}")


@app.command()
def transform(
    rebuild_all: Annotated[
        bool, typer.Option("--rebuild-all", help="Rebuild every issue, not just the dirty set")
    ] = False,
    project: ProjectOption = None,
) -> None:
    """Rebuild analytics.issue_status_interval and analytics.issue's promoted
    columns (Phase 3a.3/3a.4), then drain flowbi_ops.rebuild_request
    (Phase 3a.5).

    Default: incremental, driven by flowbi_ops.issue_dirty (claim-by-delete,
    so a crash re-queues nothing twice). --rebuild-all: the same SQL models
    with the issue-id scope replaced by "every issue" (optionally narrowed to
    one --project) - one code path for both, not two to keep in sync.
    """
    settings = Settings()  # type: ignore[call-arg]  # required fields resolved from env at runtime
    dsn = _require_postgres_dsn(settings)
    instance_id = _resolve_instance_id(settings)
    result = transform_runner.run_transform(
        dsn, instance_id, rebuild_all=rebuild_all, project=project
    )

    table = Table(title="flowbi transform")
    table.add_column("metric")
    table.add_column("value")
    table.add_row("claimed from issue_dirty", str(result.claimed))
    table.add_row("skipped (incomplete changelog)", str(result.skipped_incomplete))
    table.add_row("issue_status_interval rows written", str(result.interval_rows))
    table.add_row("analytics.issue rows written", str(result.issue_rows))
    table.add_row("bridge table rows written", str(result.bridge_rows))
    console.print(table)


if __name__ == "__main__":
    app()
