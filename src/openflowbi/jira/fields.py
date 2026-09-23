from dataclasses import dataclass

from dlt.sources.helpers.rest_client.auth import AuthConfigBase

from openflowbi.cloud.http import ClientCert, make_client


@dataclass(frozen=True)
class Field:
    id: str
    name: str
    schema_type: str | None
    custom: bool


class FieldMap:
    """Maps Jira fields by (name, schema type) — never by id.

    customfield_10001 is not the same field on two Jira instances
    — callers must look fields up by name.
    """

    def __init__(self, fields: list[Field]) -> None:
        self.fields = fields
        self._by_key = {(f.name, f.schema_type): f for f in fields}

    def id_for(self, name: str, schema_type: str | None = None) -> str:
        try:
            return self._by_key[(name, schema_type)].id
        except KeyError as exc:
            raise KeyError(f"No field named {name!r} with schema type {schema_type!r}") from exc


def fetch(
    base_url: str, auth: AuthConfigBase | None = None, *, client_cert: ClientCert | None = None
) -> list[Field]:
    """GET /field and parse the (name, schema type) -> id map.

    WRITE: field ids are per-instance; nothing generic maps them
    (customfield ids differ per instance).
    """
    client = make_client(base_url, auth, client_cert=client_cert)
    raw = client.get("/rest/api/2/field").json()
    return [
        Field(
            id=item["id"],
            name=item["name"],
            schema_type=(item.get("schema") or {}).get("type"),
            custom=item.get("custom", False),
        )
        for item in raw
    ]
