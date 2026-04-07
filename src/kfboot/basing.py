# -*- encoding: utf-8 -*-
"""
kfboot.basing module

LMDB storage for KF platform resource metadata and controller bindings.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from keri.db import dbing, koming


@dataclass(frozen=True)
class ResourceRecord:
    kind: str = ""
    eid: str = ""
    cid: str = ""
    principal: str = ""
    name: str = ""
    identifier_alias: str = ""
    region_id: str = ""
    region_name: str = ""
    url: str = ""
    public_host: str = ""
    public_port: int | None = None
    oobis: list[str] | None = None
    created_at: str = ""


@dataclass(frozen=True)
class BindingRecord:
    principal: str = ""
    cid: str = ""


@dataclass
class SessionRecord:
    """
    Represents a single onboarding session in the boot‑server flow.

    A session begins with an ephemeral principal (client‑generated AID),
    includes a server‑generated challenge, and may later be upgraded to a
    permanent AID once the client proves control of its key material.

    Attributes:
        eid: Unique session identifier assigned by the server.
        created_at: Unix timestamp when the session was created.
        expires_at: Unix timestamp when the session becomes invalid.
        upgraded_principal: Permanent AID after successful upgrade, or None.
        challenge: Challenge string the client must sign to upgrade.
    """

    eid: str
    created_at: int
    expires_at: int
    upgraded_principal: str | None = None
    challenge: str | None = None


    def to_api(self) -> dict[str, Any]:
        return {
            "session_id": self.eid,
            "status": self.status,
            "expires_at": self.expires_at,
            "principal": self.upgraded_principal,
        }

    @property
    def is_expired(self) -> bool:
        from time import time
        return int(time()) >= self.expires_at

    @property
    def status(self) -> str:
        if self.upgraded_principal:
            return "upgraded"
        if self.is_expired:
            return "expired"
        return "pending"


class PlatformBaser(dbing.LMDBer):
    """LMDB database for the KF platform service."""

    TailDirPath = ""
    AltTailDirPath = ".kf-boot"
    TempPrefix = "kf_platform_"

    def __init__(self, name="platform", headDirPath=None, reopen=True, **kwa):
        self.resources = None
        self.bindings = None

        super(PlatformBaser, self).__init__(
            name=name,
            headDirPath=headDirPath,
            reopen=reopen,
            **kwa,
        )

    def reopen(self, **kwa):
        super(PlatformBaser, self).reopen(**kwa)

        self.resources = koming.Komer(
            db=self,
            subkey="resc.",
            klas=ResourceRecord,
            schema= None,
        )
        self.bindings = koming.Komer(
            db=self,
            subkey="bind.",
            klas=BindingRecord,
            schema= None
        )
        self.sessions = koming.Komer(
            db=self,
            subkey="sess.",
            klas=SessionRecord,
            schema= SessionRecord,
        )

        return self.env


def open_baser(db_path: str) -> PlatformBaser:
    path = Path(db_path).expanduser()
    name = path.stem if path.suffix else path.name
    head = path.parent if path.parent != Path("") else Path(".")

    return PlatformBaser(
        name=name or "platform",
        headDirPath=str(head),
        reopen=True,
    )
