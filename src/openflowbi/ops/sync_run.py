"""Write flowbi_ops.sync_run rows (docs/phase2-postgres-design.md §8).

Deliberately thin control-plane bookkeeping, not one of the "write only
these" product pieces under the library-first rule — one INSERT via
SQLAlchemy Core, no ORM, no new dependency (arrives with Alembic).
"""

import uuid
from datetime import datetime

import sqlalchemy as sa

from openflowbi.ops.tables import sync_run as sync_run_table


def record(
    dsn: str,
    *,
    instance_id: str,
    project: str | None,
    mode: str,
    pipeline_name: str,
    status: str,
    started_at: datetime,
    finished_at: datetime,
    dlt_load_ids: list[str] | None = None,
    rows_loaded: int | None = None,
    error: str | None = None,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    engine = sa.create_engine(dsn)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.insert(sync_run_table).values(
                    run_id=run_id,
                    instance_id=instance_id,
                    project=project,
                    mode=mode,
                    pipeline_name=pipeline_name,
                    dlt_load_ids=dlt_load_ids or [],
                    started_at=started_at,
                    finished_at=finished_at,
                    status=status,
                    rows_loaded=rows_loaded,
                    error=error,
                )
            )
        return run_id
    finally:
        engine.dispose()
