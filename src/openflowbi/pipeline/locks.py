"""Postgres advisory lock around the dlt load step.

docs/phase2-postgres-design.md §6(b): belt-and-braces against dlt's
shared-staging-dataset TRUNCATE/INSERT race (dlt#4297) while dlt's own
`merge_scope_by_load_id` fix is still opt-in/unreleased. The per-pipeline
staging dataset (§6(a), pipeline/run.py's `staging_dataset_name_layout`)
already prevents the collision on its own; this lock is a second,
independent guard against the same failure mode, serializing only the load
step — extraction (the quota-bound, expensive part) stays fully parallel.

SQLAlchemy Core, not the ORM (CLAUDE.md library-decision-register) — arrives
free with Alembic, no new dependency.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text

# Arbitrary, stable — every process loading into this database must use the
# same key, since pg_advisory_lock's exclusivity is keyed on it alone.
LOCK_KEY = 8_314_159


@contextmanager
def load_lock(dsn: str, key: int = LOCK_KEY) -> Iterator[None]:
    """Hold a session-level Postgres advisory lock for the wrapped block.

    Blocks (does not fail) if another process already holds `key` — the load
    step is fast, so serializing it is cheap compared to the silent
    duplication/loss risk it guards against.
    """
    engine = create_engine(dsn)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
            try:
                yield
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
    finally:
        engine.dispose()
