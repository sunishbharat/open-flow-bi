"""Postgres advisory lock around the dlt load step.

Belt-and-braces against dlt's shared-staging-dataset TRUNCATE/INSERT race
(dlt#4297) while dlt's own `merge_scope_by_load_id` fix is still
opt-in/unreleased. The per-pipeline staging dataset (pipeline/run.py's
`staging_dataset_name_layout`)
already prevents the collision on its own; this lock is a second,
independent guard against the same failure mode, serializing only the load
step — extraction (the quota-bound, expensive part) stays fully parallel.

SQLAlchemy Core, not the ORM — arrives
free with Alembic, no new dependency.
"""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager

import structlog
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

logger = structlog.get_logger(__name__)

# Arbitrary, stable — every process loading into this database must use the
# same key, since pg_advisory_lock's exclusivity is keyed on it alone.
LOCK_KEY = 8_314_159

# Held by `flowbi transform` for its whole run, incremental pass and
# rebuild-queue drain alike, so overlapping runs take turns.
TRANSFORM_LOCK_KEY = 8_314_160


def load_lock(dsn: str) -> AbstractContextManager[None]:
    """Blocks (does not fail) if another process is loading — the load step
    is fast, so serializing it is cheap compared to the silent
    duplication/loss risk it guards against.
    """
    return advisory_lock(dsn, LOCK_KEY)


def transform_lock(dsn: str) -> AbstractContextManager[None]:
    """Serializes transforms on this database. Two overlapping runs (a
    scheduled one and a manual --rebuild-all) would otherwise race on
    issue_status_interval's primary key and on the rebuild queue. Blocks
    rather than fails: the second run then finds less to do.
    """
    return advisory_lock(dsn, TRANSFORM_LOCK_KEY)


@contextmanager
def advisory_lock(dsn: str, key: int) -> Iterator[None]:
    """Hold a session-level Postgres advisory lock for the wrapped block,
    blocking until any other holder of `key` releases it.
    """
    engine = create_engine(dsn)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
            try:
                yield
            finally:
                try:
                    conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                except DBAPIError:
                    # The connection dropped, which released a session lock
                    # already. Raising here would fail a load that committed.
                    logger.warning("advisory_unlock_failed", key=key, exc_info=True)
    finally:
        engine.dispose()
