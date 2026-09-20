"""Pandera contracts for the flattened Jira tables dlt writes to Parquet.

Pure: validates a pyarrow.Table already in memory, no I/O, no glob, no
filesystem (mirrors jira/flatten.py's purity rule) — reading Parquet is the
caller's job (cli.py / debug tooling), this module only checks shape.

WRITE (thin ADAPT, really): pandera's default backend needs pandas, which
this project doesn't otherwise depend on. dlt's filesystem destination
already hands us pyarrow.Table (via pyarrow.parquet), so the `pandera[pyarrow]`
backend validates that directly — no pandas/polars dependency needed just for
this. `narwhals` is pandera[pyarrow]'s own transitive requirement, not
something we chose to add.
"""

import pandera.pyarrow as pa
import pyarrow

ISSUES_SCHEMA = pa.DataFrameSchema(
    {
        # Phase 2 (docs/phase2-postgres-design.md §3/§7): part of the
        # compound primary key so the same issue_id from two Jira instances
        # never collides.
        "instance_id": pa.Column(pyarrow.string(), nullable=False),
        # bigint, not string, from M7.2 on — flatten.issues() casts the
        # numeric-string-in-JSON id to int (§3's table shape). Existing
        # Parquet predating M7.2 is not migrated (§1); re-extract.
        "issue_id": pa.Column(pyarrow.int64(), nullable=False),
        "issue_key": pa.Column(pyarrow.string(), nullable=True),
        # flatten.issues() yields these as passthrough ISO strings, but dlt's
        # own normalizer detects the ISO shape at load time and casts them to
        # a real timestamp column in Parquet - confirmed against a live
        # extract, not assumed (CLAUDE.md: "trust the response and update
        # this file"). Checking pyarrow.string() here silently failed against
        # every real extract.
        "created_at": pa.Column(pyarrow.timestamp("us", tz="UTC"), nullable=False),
        "updated_at": pa.Column(pyarrow.timestamp("us", tz="UTC"), nullable=True),
    },
    # rule 4 (CLAUDE.md): issue_id is the only identity, scoped per instance
    # (P2-D2 — primary keys must include instance_id).
    unique=["instance_id", "issue_id"],
    strict=False,  # `fields` (passthrough JSON) and dlt's own _dlt_* columns ride along
)

CHANGELOG_SCHEMA = pa.DataFrameSchema(
    {
        "instance_id": pa.Column(pyarrow.string(), nullable=False),
        "issue_id": pa.Column(pyarrow.int64(), nullable=False),
        "history_id": pa.Column(pyarrow.string(), nullable=False),
        "item_index": pa.Column(
            pyarrow.int64(), nullable=False, checks=pa.Check.ge(0)
        ),
        "source": pa.Column(
            pyarrow.string(),
            nullable=False,
            checks=pa.Check.isin(["expand", "bulkfetch", "per_issue"]),
        ),
        "changelog_complete": pa.Column(pyarrow.bool_(), nullable=False),
        # M7.5 (docs/phase2-postgres-design.md §14): the issue's own
        # `fields.updated`, not the history row's `created_at` below — this
        # is what pipeline/source.py's issue_changelog resource uses as its
        # incremental cursor. required=False so Parquet from before this
        # milestone (column doesn't exist yet) still validates.
        "updated_at": pa.Column(
            pyarrow.timestamp("us", tz="UTC"), nullable=True, required=False
        ),
    },
    # item_index is only deterministic within one history's items[] (see
    # flatten.changelog) — the row identity is the full quadruple, matching
    # the dlt resource's primary_key in pipeline/source.py.
    unique=["instance_id", "issue_id", "history_id", "item_index"],
    strict=False,
)


def validate_issues(table: pyarrow.Table) -> pyarrow.Table:
    return ISSUES_SCHEMA.validate(table)


def validate_changelog(table: pyarrow.Table) -> pyarrow.Table:
    return CHANGELOG_SCHEMA.validate(table)
