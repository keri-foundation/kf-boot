from __future__ import annotations

import math
import secrets
import time
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from kfboot.basing import BindingRecord, ResourceRecord, open_baser, SessionRecord

SESSION_TTL_SECONDS = 5 * 60  # 5 minutes

def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def parse_public_url(url: str) -> tuple[str, int | None]:
    parts = urlsplit(url)
    return parts.hostname or "", parts.port


def _to_api(record: ResourceRecord) -> dict[str, Any]:
    data = asdict(record)
    data.pop("kind", None)
    data.pop("principal", None)
    data["oobis"] = list(record.oobis or [])
    return data


class Store:
    def __init__(self, path: str):
        self.baser = open_baser(path)

    def close(self) -> None:
        self.baser.close()

    def allow_controller(self, principal: str, cid: str, is_admin: bool) -> bool:
        if is_admin or principal == cid:
            return True

        return self.baser.bindings.get(keys=(principal, cid)) is not None

    def add_binding(self, principal: str, cid: str) -> None:
        self.baser.bindings.pin(
            keys=(principal, cid),
            val=BindingRecord(principal=principal, cid=cid),
        )

    def add_resource(self, record: ResourceRecord) -> None:
        self.baser.resources.pin(keys=(record.kind, record.eid), val=record)

    def get_resource(self, kind: str, eid: str) -> ResourceRecord | None:
        return self.baser.resources.get(keys=(kind, eid))

    def delete_resource(self, kind: str, eid: str) -> None:
        self.baser.resources.rem(keys=(kind, eid))

    def count_resources(self, kind: str) -> int:
        return sum(1 for _, _ in self.baser.resources.getItemIter(keys=(kind,)))

    def list_resources(
        self,
        *,
        kind: str,
        principal: str,
        is_admin: bool,
        page: int,
        page_size: int,
        filter_term: str | None,
        order: list[str] | None,
    ) -> dict[str, Any]:
        records = [
            record for _, record in self.baser.resources.getItemIter(keys=(kind,))
            if self._visible(record, principal, is_admin)
        ]

        if filter_term:
            term = filter_term.lower()
            records = [
                record for record in records
                if term in record.name.lower()
                or term in record.eid.lower()
                or term in record.identifier_alias.lower()
                or term in record.cid.lower()
            ]

        _sort_records(records, order)

        count = len(records)
        page = max(page, 0)
        start = page * page_size
        end = start + page_size
        rows = records[start:end]

        key = "witnesses" if kind == "witness" else "watchers"
        num_pages = math.ceil(count / page_size) if count else 1
        return {
            "page": page,
            "num_pages": num_pages,
            "count": count,
            key: [_to_api(record) for record in rows],
        }

    def _visible(self, record: ResourceRecord, principal: str, is_admin: bool) -> bool:
        if is_admin:
            return True

        return (
            record.principal == principal
            or record.cid == principal
            or self.baser.bindings.get(keys=(principal, record.cid)) is not None
        )

    def add_session(self) -> SessionRecord:
        eid = _new_session_id()
        principal = _new_ephemeral_principal()
        now = int(time.time())
        expires_at = now + SESSION_TTL_SECONDS

        record = SessionRecord(
            eid=eid,
            created_at=now,
            expires_at=expires_at,
            upgraded_principal=None,
        )
        self.baser.sessions.pin(keys=(eid,), val=record)
        return record

    def get_session(self, eid: str) -> SessionRecord | None:
        return self.baser.sessions.get(keys=(eid))

    def update_session(self, record: SessionRecord) -> None:
        self.baser.sessions.pin(keys=(record.eid), val=record)

    def delete_session(self, eid: str) -> None:
        self.baser.sessions.rem(keys=(eid))

def _new_session_id() -> str:
    return f"sess_{secrets.token_urlsafe(16)}"


def _new_ephemeral_principal() -> str:
    return f"ephem_{secrets.token_urlsafe(16)}"


def make_record(
    *,
    kind: str,
    eid: str,
    cid: str,
    principal: str,
    name: str,
    identifier_alias: str,
    region_id: str,
    region_name: str,
    public_url: str,
    oobis: list[str],
) -> ResourceRecord:
    public_host, public_port = parse_public_url(public_url)
    return ResourceRecord(
        kind=kind,
        eid=eid,
        cid=cid,
        principal=principal,
        name=name,
        identifier_alias=identifier_alias,
        region_id=region_id,
        region_name=region_name,
        url=public_url,
        public_host=public_host,
        public_port=public_port,
        oobis=list(oobis),
        created_at=now_iso(),
    )


def _sort_records(records: list[ResourceRecord], order: list[str] | None) -> None:
    specs = _order_specs(order)
    for field, reverse in reversed(specs):
        records.sort(
            key=lambda record, field=field: _sort_value(getattr(record, field)),
            reverse=reverse,
        )


def _order_specs(order: list[str] | None) -> list[tuple[str, bool]]:
    allowed = {
        "name",
        "eid",
        "identifier_alias",
        "region_name",
        "cid",
        "created_at",
    }
    if not order:
        return [("created_at", True)]

    specs: list[tuple[str, bool]] = []
    for entry in order:
        if not entry:
            continue
        reverse = entry[0] != "+"
        field = entry[1:] if entry[0] in "+-" else entry
        if field not in allowed:
            continue
        specs.append((field, reverse))

    return specs or [("created_at", True)]


def _sort_value(value: Any) -> Any:
    if value is None:
        return ""
    return value
