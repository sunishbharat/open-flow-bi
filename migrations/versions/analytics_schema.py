"""analytics schema

Revision ID: c44c33c8584c
Revises: 948acc1618ac
Create Date: 2026-09-20 21:39:48.147478

Creates the empty `analytics` schema (docs/phase2-postgres-design.md §3: "Alembic
owns. EMPTY in Phase 2. Phase 3 fills it."). Alembic-owned, not dlt's — belongs
here rather than as a hand-run psql command so a fresh database (CI, a new
dev's docker-compose) gets it from `alembic upgrade head` alone, and so
`migrations/sql/roles.sql`'s `GRANT ... ON SCHEMA analytics TO cube_reader`
has something to grant on regardless of whether Phase 3 has landed yet.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c44c33c8584c"
down_revision: str | Sequence[str] | None = "948acc1618ac"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS analytics")


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS analytics CASCADE")
