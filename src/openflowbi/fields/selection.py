"""Versioned field selection: promote/demote, export/import YAML.

docs/phase3-field-selection-design.md §2.1/§3/§5, Phase 3a.2. `field_selection`
is append-only — "this IS the audit trail" — so `save_selection` never updates
a row in place; every save writes a full new-version snapshot (every
currently-selected field, live or deprecated, with the requested changes
applied). `current_selection` at the latest version is then always a
complete, self-consistent view — no caller ever needs to union rows across
versions to know "what's selected right now".

No new dependency beyond PyYAML (already transitive via dlt/vcrpy, now
declared explicitly since this module imports it directly — pendulum's M5
precedent, docs/session-status-2026-09-18.md).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
import yaml

from openflowbi.ops.tables import field_definition, field_selection

VALID_TARGETS = ("column", "bridge_table")

# transform/runner.py interpolates column_name directly into DDL (ALTER TABLE
# ADD COLUMN, bridge table names) — bind params can't parameterise SQL
# identifiers, so an unsafe value must never reach save_selection in the
# first place. Lowercase snake_case, Postgres' 63-byte identifier limit.
# This is operator-chosen (--column), so requiring a clean convention is
# reasonable — unlike field_id below, which is not operator-chosen.
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

# field_id comes from Jira's own /field endpoint, never from the operator,
# and is embedded as a JSON key in generated SQL (fields ->> 'field_id'), not
# as a raw identifier the way column_name is. Real Jira field ids are always
# alphanumeric + underscore, but NOT always lowercase — found live that
# "Fix Version/s"'s own id is "fixVersions" (camelCase), which _IDENTIFIER_RE
# above wrongly rejected, aborting a whole `flowbi transform --rebuild-all`
# run over one unrelated field. Less strict than _IDENTIFIER_RE for that
# reason; still blocks anything that could break out of the single-quoted
# JSON-key string it gets embedded in.
_FIELD_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")


def validate_identifier(name: str, *, what: str = "column name") -> None:
    """Raise ValueError unless `name` is safe to interpolate as a Postgres
    identifier. Called at promotion time (here) and again, defensively,
    wherever transform/runner.py actually builds DDL from it.
    """
    if not _IDENTIFIER_RE.match(name):
        raise ValueError(
            f"{what} {name!r} is not a valid identifier - use lowercase letters, "
            "digits and underscores, starting with a letter"
        )


def validate_field_id(field_id: str) -> None:
    """Raise ValueError unless `field_id` is safe to embed as a JSON key in
    generated SQL. See _FIELD_ID_RE's comment for why this is more permissive
    than validate_identifier.
    """
    if not _FIELD_ID_RE.match(field_id):
        raise ValueError(
            f"field id {field_id!r} is not a valid identifier - use letters, digits and "
            "underscores, starting with a letter"
        )


@dataclass(frozen=True)
class SelectionChange:
    """One requested edit — the CLI's `promote`/`demote` and `import_yaml`'s
    per-field entries all build one of these before calling save_selection().
    """

    field_name: str
    schema_type: str
    promote: bool
    field_id: str | None = None
    column_name: str | None = None
    target: str = "column"
    note: str | None = None


@dataclass(frozen=True)
class SelectionRow:
    field_name: str
    schema_type: str
    field_id: str | None
    promote: bool
    column_name: str | None
    target: str
    deprecated_at: datetime | None
    note: str | None


def resolve_field_name(
    dsn: str, instance_id: str, field_name: str, schema_type: str | None = None
) -> tuple[str, str | None]:
    """Look up field_definition by display name (§2.1: "keyed by NAME, not id").

    Returns (schema_type, field_id). Raises ValueError if zero or more than
    one field_definition row matches — the CLI/service layer's job is to turn
    that into a clear message, not a stack trace.
    """
    engine = sa.create_engine(dsn)
    try:
        with engine.connect() as conn:
            query = sa.select(field_definition.c.schema_type, field_definition.c.field_id).where(
                field_definition.c.instance_id == instance_id,
                field_definition.c.name == field_name,
            )
            if schema_type is not None:
                query = query.where(field_definition.c.schema_type == schema_type)
            rows = conn.execute(query).fetchall()
    finally:
        engine.dispose()

    if not rows:
        raise ValueError(
            f"no field named {field_name!r} found for this instance - "
            "run `flowbi fields discover` first, or check --schema-type"
        )
    if len(rows) > 1:
        types = ", ".join(sorted({r.schema_type for r in rows}))
        raise ValueError(
            f"{field_name!r} is ambiguous ({len(rows)} matches, schema types: {types}) - "
            "disambiguate with --schema-type"
        )
    return rows[0].schema_type, rows[0].field_id


def _latest_version(conn: sa.Connection, instance_id: str) -> int:
    return conn.execute(
        sa.select(sa.func.coalesce(sa.func.max(field_selection.c.version), 0)).where(
            field_selection.c.instance_id == instance_id
        )
    ).scalar_one()


def current_selection(
    dsn: str, instance_id: str, version: int | None = None
) -> tuple[int, dict[tuple[str, str], SelectionRow]]:
    """The full selection at `version` (default: the latest). Returns
    (version_used, {(field_name, schema_type): SelectionRow}); version_used
    is 0 and the dict is empty if nothing has ever been saved for this instance.
    """
    engine = sa.create_engine(dsn)
    try:
        with engine.connect() as conn:
            target_version = version if version is not None else _latest_version(conn, instance_id)
            if target_version == 0:
                return 0, {}
            rows = conn.execute(
                sa.select(field_selection).where(
                    field_selection.c.instance_id == instance_id,
                    field_selection.c.version == target_version,
                )
            ).fetchall()
    finally:
        engine.dispose()

    return target_version, {
        (r.field_name, r.schema_type): SelectionRow(
            field_name=r.field_name,
            schema_type=r.schema_type,
            field_id=r.field_id,
            promote=r.promote,
            column_name=r.column_name,
            target=r.target,
            deprecated_at=r.deprecated_at,
            note=r.note,
        )
        for r in rows
    }


def _check_no_column_name_collisions(working: dict[tuple[str, str], SelectionRow]) -> None:
    """§14 open question #4: two fields both wanting the same column - reject
    at save time with a clear message, rather than discovering it at rebuild.
    Only live (not deprecated), column-target rows can collide.
    """
    claimed: dict[str, tuple[str, str]] = {}
    for key, row in working.items():
        if row.target != "column" or row.deprecated_at is not None or not row.column_name:
            continue
        other = claimed.get(row.column_name)
        if other is not None and other != key:
            raise ValueError(
                f"column name {row.column_name!r} is claimed by both "
                f"{other[0]!r} and {key[0]!r} - rename one with --column"
            )
        claimed[row.column_name] = key


def save_selection(
    dsn: str, instance_id: str, changes: list[SelectionChange], actor: str
) -> int:
    """Write a new version: the prior version's rows, with `changes` applied.
    Never updates a row in place (§2.1: append-only). Returns the new version
    number, starting at 1.
    """
    if not changes:
        raise ValueError("no changes to save")
    for change in changes:
        if change.target not in VALID_TARGETS:
            raise ValueError(f"target must be one of {VALID_TARGETS}, got {change.target!r}")
        if change.column_name is not None:
            validate_identifier(change.column_name)
        if change.promote and change.target == "bridge_table" and change.schema_type != "array":
            raise ValueError(
                f"target 'bridge_table' is for array fields, got schema_type {change.schema_type!r}"
            )
        if change.promote and change.target == "column" and change.schema_type == "array":
            raise ValueError("array fields must use --target bridge_table, not column")

    engine = sa.create_engine(dsn)
    try:
        with engine.begin() as conn:
            current_version = _latest_version(conn, instance_id)
            existing = (
                conn.execute(
                    sa.select(field_selection).where(
                        field_selection.c.instance_id == instance_id,
                        field_selection.c.version == current_version,
                    )
                ).fetchall()
                if current_version
                else []
            )
            working: dict[tuple[str, str], SelectionRow] = {
                (r.field_name, r.schema_type): SelectionRow(
                    field_name=r.field_name,
                    schema_type=r.schema_type,
                    field_id=r.field_id,
                    promote=r.promote,
                    column_name=r.column_name,
                    target=r.target,
                    deprecated_at=r.deprecated_at,
                    note=r.note,
                )
                for r in existing
            }

            for change in changes:
                key = (change.field_name, change.schema_type)
                if change.promote:
                    prior = working.get(key)
                    working[key] = SelectionRow(
                        field_name=change.field_name,
                        schema_type=change.schema_type,
                        field_id=change.field_id or (prior.field_id if prior else None),
                        promote=True,
                        column_name=change.column_name or (prior.column_name if prior else None),
                        target=change.target,
                        deprecated_at=None,  # promoting un-deprecates, if it was
                        note=(
                            change.note
                            if change.note is not None
                            else (prior.note if prior else None)
                        ),
                    )
                else:
                    prior = working.get(key)
                    if prior is None:
                        raise ValueError(
                            f"cannot demote {change.field_name!r} ({change.schema_type}): "
                            "not currently selected"
                        )
                    working[key] = SelectionRow(
                        field_name=prior.field_name,
                        schema_type=prior.schema_type,
                        field_id=prior.field_id,
                        promote=prior.promote,
                        column_name=prior.column_name,
                        target=prior.target,
                        deprecated_at=datetime.now(UTC),
                        note=change.note if change.note is not None else prior.note,
                    )

            _check_no_column_name_collisions(working)

            new_version = current_version + 1
            conn.execute(
                sa.insert(field_selection).values(
                    [
                        {
                            "version": new_version,
                            "instance_id": instance_id,
                            "field_name": row.field_name,
                            "schema_type": row.schema_type,
                            "field_id": row.field_id,
                            "promote": row.promote,
                            "column_name": row.column_name,
                            "target": row.target,
                            "deprecated_at": row.deprecated_at,
                            "created_by": actor,
                            "note": row.note,
                        }
                        for row in working.values()
                    ]
                )
            )
        return new_version
    finally:
        engine.dispose()


def export_yaml(dsn: str, instance_id: str, version: int | None = None) -> str:
    """The selection at `version` (default: latest) as YAML text — a
    materialisation of the database, not the other way round (§3).
    """
    resolved_version, selection = current_selection(dsn, instance_id, version=version)
    doc = {
        "instance_id": instance_id,
        "version": resolved_version,
        "fields": [
            {
                "name": row.field_name,
                "schema_type": row.schema_type,
                **({"field_id": row.field_id} if row.field_id else {}),
                "column_name": row.column_name,
                "target": row.target,
                "deprecated": row.deprecated_at is not None,
                **({"note": row.note} if row.note else {}),
            }
            for row in sorted(selection.values(), key=lambda r: (r.field_name, r.schema_type))
        ],
    }
    return yaml.safe_dump(doc, sort_keys=False)


def import_yaml(dsn: str, instance_id: str, text: str, actor: str) -> int:
    """Parse a `fields export`-shaped YAML doc and save it as a new version."""
    doc = yaml.safe_load(text) or {}
    fields = doc.get("fields") or []
    changes = [
        SelectionChange(
            field_name=f["name"],
            schema_type=f["schema_type"],
            promote=not f.get("deprecated", False),
            field_id=f.get("field_id"),
            column_name=f.get("column_name"),
            target=f.get("target", "column"),
            note=f.get("note"),
        )
        for f in fields
    ]
    if not changes:
        raise ValueError("YAML has no fields to import")
    return save_selection(dsn, instance_id, changes, actor)
