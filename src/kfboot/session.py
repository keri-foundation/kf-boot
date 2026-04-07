from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
import falcon
import secrets
from kfboot.store import Store
from kfboot.basing import SessionRecord 

from keri.core import Prefixer, Verfer, Siger


def _request_json(req: falcon.Request) -> Any:
    try:
        return req.get_media()
    except Exception as exc:
        raise falcon.HTTPBadRequest(
            title="Invalid request body",
            description=str(exc),
        ) from exc


class SessionCollectionEnd:
    def __init__(self, ctx):
        self.ctx = ctx

    def on_post(self, _req: falcon.Request, rep: falcon.Response) -> None:
        # Create a new session 
        record = self.ctx.store.add_session()

        # Attach a random challenge for the permanent AID to sign later
        record.challenge = secrets.token_urlsafe(32)
        self.ctx.store.update_session(record)

        rep.status = falcon.HTTP_201
        rep.media = {
            "session_id": record.eid,
            "status": record.status,
            "challenge": record.challenge,
            "expires_at": record.expires_at,
        }



class SessionResourceEnd:
    def __init__(self, ctx):
        self.ctx = ctx

    def on_get(self, _req: falcon.Request, rep: falcon.Response, session_id: str) -> None:
        record = self.ctx.store.get_session(session_id)
        if record is None:
            raise falcon.HTTPNotFound(title="Session not found")

        rep.media = record.to_api()


class SessionUpgradeEnd:
    def __init__(self, ctx):
        self.ctx = ctx

    def on_post(self, req: falcon.Request, rep: falcon.Response, session_id: str) -> None:
        body = _request_json(req)
        if not isinstance(body, dict):
            raise falcon.HTTPBadRequest(
                title="Invalid request body",
                description="Expected JSON object",
            )

        cid = body.get("cid")   # permanent AID prefix
        sig = body.get("sig")   # signature over challenge

        if not isinstance(cid, str) or not cid:
            raise falcon.HTTPBadRequest(
                title="Invalid controller",
                description="Field 'cid' is required",
            )

        if not isinstance(sig, str) or not sig:
            raise falcon.HTTPBadRequest(
                title="Invalid signature",
                description="Field 'sig' is required",
            )

        record = self.ctx.store.get_session(session_id)
        if record is None:
            raise falcon.HTTPNotFound(title="Session not found")

        if record.is_expired:
            raise falcon.HTTPGone(title="Session expired")

        if record.upgraded_principal:
            # idempotent
            rep.media = record.to_api()
            return

        if not record.challenge:
            raise falcon.HTTPBadRequest(
                title="Invalid session state",
                description="Missing challenge for this session",
            )

        prefixer = Prefixer(qb64=cid)
        public_key = prefixer.raw  # raw public key bytes
        code = prefixer.code       # derivation code (e.g. Ed25519)

        verfer = Verfer(raw=public_key, code=code)

        siger = Siger(qb64=sig)

        valid = verfer.verify(siger.raw, record.challenge.encode())
        
        if not valid:
            raise falcon.HTTPUnauthorized(
                title="Invalid signature",
                description="Signature does not match permanent AID"
            )

        record.upgraded_principal = cid
        self.ctx.store.update_session(record)

        rep.media = record.to_api()