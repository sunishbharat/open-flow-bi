from collections.abc import Iterable, Iterator
from typing import Any

from openflowbi.jira.changelog import ChangelogBatch


def issues(raw_issues: Iterable[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Flatten raw Jira issue objects into flat rows.

    Pure: no I/O, no clock, no global state (CLAUDE.md rule 3). issue_id is
    the only identity carried forward — issue_key is a mutable attribute Jira
    changes when an issue moves project, never used as a key (rule 4).
    created_at/updated_at are promoted out of `fields` because downstream
    interval-building needs `created_at` to seed the first status interval,
    since the changelog only records transitions, not the initial state.
    """
    for issue in raw_issues:
        fields = issue.get("fields") or {}
        yield {
            "issue_id": issue["id"],
            "issue_key": issue.get("key"),
            "created_at": fields.get("created"),
            "updated_at": fields.get("updated"),
            "fields": fields,
        }


def changelog(batches: Iterable[ChangelogBatch]) -> Iterator[dict[str, Any]]:
    """Flatten (issue_id, source, complete, histories) batches into flat item rows.

    Pure: no I/O, no clock, no global state (rule 3). item_index comes from a
    deterministic sort of items[] within each history — by (field, fieldId),
    never array position, since Jira does not guarantee ordering and a
    re-order would silently duplicate rows on re-ingest. field_id is often
    null for system fields; that is normal, not a parse failure.
    """
    for issue_id, source, complete, histories in batches:
        # Note: an issue with complete=False and no histories (per-issue tier
        # itself unavailable) yields zero rows here — there is no row to carry
        # changelog_complete for that issue. debug/queries.sql's "incomplete
        # changelogs" check must therefore diff against the issues table, not
        # rely on finding a changelog_complete=False row.
        for history in histories:
            items = sorted(
                history.get("items") or [],
                key=lambda item: (item.get("field") or "", item.get("fieldId") or ""),
            )
            for item_index, item in enumerate(items):
                yield {
                    "issue_id": issue_id,
                    "history_id": history["id"],
                    "item_index": item_index,
                    "source": source,
                    "changelog_complete": complete,
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
