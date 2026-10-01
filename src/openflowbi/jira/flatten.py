from collections.abc import Iterable, Iterator
from typing import Any

import pendulum

from openflowbi.jira.changelog import ChangelogBatch
from openflowbi.jira.fields import Field


def _instant(value: str | None) -> pendulum.DateTime | None:
    """Parse a Jira timestamp string into a timezone-aware instant.

    `updated_at` is the dlt incremental cursor, and dlt compares cursor values
    with plain `max()`: over raw strings that is a lexical compare, so
    `...02:10+0100` (01:10Z) sorts below `...02:30+0200` (00:30Z) and a
    genuinely newer update is dropped (architecture review finding 1).
    """
    if value is None:
        return None
    parsed = pendulum.parse(value)
    if not isinstance(parsed, pendulum.DateTime):
        raise ValueError(f"expected a full ISO datetime, got {value!r}")
    return parsed


def fields(raw_fields: Iterable[Field], instance_id: str) -> Iterator[dict[str, Any]]:
    """Flatten Field objects into passthrough dicts for the dlt `fields` resource.

    Pure: no I/O, no clock, no global state (rule 3). Trivial today, but kept
    here (not inline in pipeline/source.py) so it can be exercised directly
    without going through dlt's resource-iteration machinery, which — unlike
    pipeline.run() — doesn't clean up its worker thread deterministically on
    early return; see tests/pipeline/test_run.py's fields-resource tests for
    the actually-reliable way to exercise the dlt wrapper end-to-end.

    instance_id (docs/phase2-postgres-design.md §7): part of the compound
    primary key so the same field id from two different Jira instances never
    collides.
    """
    for field in raw_fields:
        yield {
            "instance_id": instance_id,
            "field_id": field.id,
            "name": field.name,
            "schema_type": field.schema_type,
            "custom": field.custom,
        }


def issues(raw_issues: Iterable[dict[str, Any]], instance_id: str) -> Iterator[dict[str, Any]]:
    """Flatten raw Jira issue objects into flat rows.

    Pure: no I/O, no clock, no global state. issue_id is
    the only identity carried forward — issue_key is a mutable attribute Jira
    changes when an issue moves project, never used as a key (rule 4).
    created_at/updated_at are promoted out of `fields` because downstream
    interval-building needs `created_at` to seed the first status interval,
    since the changelog only records transitions, not the initial state.

    instance_id (docs/phase2-postgres-design.md §7): part of the compound
    primary key, first column of every row. issue_id is cast to int here —
    Jira's JSON always carries it as a numeric string, but the Postgres
    column is bigint (§3), so the cast belongs in this pure layer rather than
    relying on a destination to infer it.
    """
    for issue in raw_issues:
        fields = issue.get("fields") or {}
        yield {
            "instance_id": instance_id,
            "issue_id": int(issue["id"]),
            "issue_key": issue.get("key"),
            "created_at": fields.get("created"),
            "updated_at": _instant(fields.get("updated")),
            "fields": fields,
        }


def changelog(
    batches: Iterable[ChangelogBatch],
    instance_id: str,
    issue_updated: dict[str, str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Flatten (issue_id, source, complete, histories) batches into flat item rows.

    Pure: no I/O, no clock, no global state (rule 3). item_index comes from a
    deterministic sort of items[] within each history — by (field, fieldId),
    never array position, since Jira does not guarantee ordering and a
    re-order would silently duplicate rows on re-ingest. field_id is often
    null for system fields; that is normal, not a parse failure.

    instance_id (docs/phase2-postgres-design.md §7): part of the compound
    primary key. issue_id is cast to int to match flatten.issues() — same
    numeric-string-in-JSON, bigint-in-Postgres reasoning.

    issue_updated (M7.5, docs/phase2-postgres-design.md §14): an optional
    {raw issue_id (str) -> the issue's own `fields.updated`} map, stamped
    onto every row as `updated_at` — distinct from `created_at` below, which
    is the *history* entry's created timestamp, not the issue's. This is what
    `pipeline/source.py`'s issue_changelog resource uses as its
    `dlt.sources.incremental` cursor field, mirroring `issues` (parsed to an
    aware datetime for the same reason as `issues`' cursor). Callers that
    don't pass it (e.g. existing tests) get `updated_at=None` throughout.
    """
    # Not `issue_updated or {}`: the caller's dict starts empty and is
    # mutated lazily as `batches` is pulled (pipeline/source.py's issue_changelog
    # taps it during iteration) — an `or` on a still-empty-but-live dict would
    # silently rebind to a throwaway `{}` on first access and never see the
    # caller's later mutations.
    if issue_updated is None:
        issue_updated = {}
    for issue_id, source, complete, histories in batches:
        # An issue with no histories yields zero rows here, so these rows can't
        # say whether an issue's changelog was extracted at all:
        # changelog_status() below is the per-issue record of that.
        for history in histories:
            items = sorted(
                history.get("items") or [],
                key=lambda item: (item.get("field") or "", item.get("fieldId") or ""),
            )
            for item_index, item in enumerate(items):
                yield {
                    "instance_id": instance_id,
                    "issue_id": int(issue_id),
                    "history_id": history["id"],
                    "item_index": item_index,
                    "source": source,
                    "changelog_complete": complete,
                    "updated_at": _instant(issue_updated.get(str(issue_id))),
                    "author": (history.get("author") or {}).get("name"),
                    "created_at": history.get("created"),
                    "field": item.get("field"),
                    "field_id": item.get("fieldId"),
                    "field_type": item.get("fieldtype"),
                    "from_id": item.get("from"),
                    "from_value": item.get("fromString"),
                    "to_id": item.get("to"),
                    "to_value": item.get("toString"),
                }


def changelog_status(
    batch: ChangelogBatch, instance_id: str, issue_updated: dict[str, str]
) -> dict[str, Any]:
    """One row per extracted issue saying whether its changelog is complete.

    Pure, like changelog() above. The item rows can't carry this: an issue
    with no histories (never transitioned, or the per-issue tier unavailable)
    has no item row, so an incomplete extraction looked exactly like a
    complete one with nothing in it (architecture review finding 4).
    `updated_at` is the issue's `fields.updated` at extraction time: the
    transform only trusts a changelog extracted for the issue's current
    version, and it is also the resource's incremental cursor.
    """
    issue_id, source, complete, histories = batch
    return {
        "instance_id": instance_id,
        "issue_id": int(issue_id),
        "source": source,
        "changelog_complete": complete,
        "history_count": len(histories),
        "updated_at": _instant(issue_updated.get(str(issue_id))),
    }
