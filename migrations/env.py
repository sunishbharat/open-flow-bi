from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from openflowbi.config import Settings
from openflowbi.ops.tables import metadata as ops_metadata

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Single user-facing DSN surface (docs/phase2-postgres-design.md §4.4) — the
# same FLOWBI_POSTGRES_DSN pipeline/run.py hands to dlt, not a second URL
# living only in alembic.ini.
settings = Settings()  # type: ignore[call-arg]  # required fields resolved from env at runtime
if not settings.postgres_dsn:
    raise RuntimeError("FLOWBI_POSTGRES_DSN is required to run Alembic migrations")
config.set_main_option("sqlalchemy.url", settings.postgres_dsn)

# P2-D1 (docs/phase2-postgres-design.md §2/§5): Alembic owns flowbi_ops. It
# must be blind to jira_raw — dlt reconciles that schema against its own
# stored schema on every run, and an out-of-band ALTER TABLE from Alembic
# would make dlt's schema and the database disagree.
#
# `analytics` is Alembic-*created* (migrations/versions/c44c33c8584c) but,
# since Phase 3a, no longer Alembic-*compared*: analytics.issue's promoted
# columns and transform/runner.py's dynamically-created bridge tables
# (src/openflowbi/ops/analytics_tables.py's own docstring) are runtime-managed,
# not migration-managed, for exactly the same reason jira_raw is excluded —
# an out-of-band schema Alembic doesn't fully control would otherwise be
# "drift" `alembic check`/`--autogenerate` tries to remove forever. Hand-written
# migrations against `analytics` (schema="analytics" in op.create_table(...))
# still work fine either way — TARGET_SCHEMAS only gates autogenerate
# comparison, never migration execution.
target_metadata = ops_metadata

TARGET_SCHEMAS = {"flowbi_ops"}


def include_name(name, type_, parent_names):
    if type_ == "schema":
        return name in TARGET_SCHEMAS
    return True


def include_object(obj, name, type_, reflected, compare_to):
    # Belt and braces: never touch anything dlt owns, even if include_name's
    # schema filter above were ever loosened by mistake.
    schema = getattr(obj, "schema", None)
    if schema and (schema.startswith("jira_raw") or schema == "jira_raw"):
        return False
    return True


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        include_schemas=True,
        include_name=include_name,
        include_object=include_object,
        version_table_schema="flowbi_ops",
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        # Alembic creates its version table (in version_table_schema, below)
        # *before* running any migration script — including migration 0001,
        # whose own "CREATE SCHEMA IF NOT EXISTS flowbi_ops" therefore runs
        # too late on a genuinely fresh database. Ensure the schema exists
        # here first, outside the migration transaction, so
        # `alembic upgrade head` works from empty every time, not just after
        # a first manual bootstrap.
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS flowbi_ops"))
        connection.commit()

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            include_name=include_name,
            include_object=include_object,
            # Keeps alembic_version out of `public` (docs/phase2-postgres-design.md
            # §5) — nobody is tempted to put anything there.
            version_table_schema="flowbi_ops",
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
