from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
import falcon
import secrets
from kfboot.store import Store
from kfboot.basing import SessionRecord 

from keri.core import Prefixer, Verfer, Siger, coring
from keri.end import ending

def _request_json(req: falcon.Request) -> Any:
    """
    Parse and return the JSON body from a Falcon request.

    Raises:
        falcon.HTTPBadRequest: If the body cannot be parsed as JSON.
    """
    try:
        return req.get_media()
    except Exception as exc:
        raise falcon.HTTPBadRequest(
            title="Invalid request body",
            description=str(exc),
        ) from exc

def make_signature_header_from_sigers(sigers):
    """
    Build a CESR HTTP Signature header from one or more Siger instances.

    The sigers are indexed and wrapped in a Signage object, which is then
    serialized into a `Signature` header.

    Args:
        sigers: Iterable of Siger instances to include in the header.

    Returns:
        dict: A mapping containing the `Signature` header value.
    """
    markers = {str(i): siger for i, siger in enumerate(sigers)}

    signage = ending.Signage(
        markers=markers,
        indexed=True,
        signer=None,
        ordinal=None,
        digest=None,
        kind=None,
    )

    return ending.signature([signage])

def extract_siger_from_header(sig_header: str) -> Siger:
    """
    Parse Signature header and return the first Siger.
    """
    signages = ending.designature(sig_header)
    if not signages:
        raise ValueError("Invalid Signature header")

    signage = signages[0]
    sig = list(signage.markers.values())[0]
    return sig


class SessionCollectionEnd:
    """
    Falcon resource for creating new ephemeral sessions.

    This endpoint:
      - Validates and SAID-ifies the request body.
      - Verifies an ephemeral AID signature over the SAID.
      - Creates a new session record.
      - Returns a SAID-ified, host-signed session response.
    """
    def __init__(self, ctx):
        """
        Initialize the session collection resource.

        Args:
            ctx: Application context providing `hostHab` and `store`.
        """
        self.ctx = ctx
        self.hostHab = ctx.hostHab

    def on_post(self, _req: falcon.Request, rep: falcon.Response) -> None:
        """
        Handle POST /session to create a new ephemeral session.

        Expects a JSON body with:
          - i: ephemeral AID prefix
          - ts: timestamp (not currently used, could be useful for later uses)
          - d: placeholder for SAID

        Verifies the signature over the SAID, creates a session, and responds
        with a SAID-ified, host-signed session object.
        """
        # Parse JSON body
        body = _request_json(_req)

        if not isinstance(body, dict):
            raise falcon.HTTPBadRequest(
                title="Invalid request body",
                description="Expected JSON object",
            )

        # SAID-ify the body
        saider, body = coring.Saider.saidify(sad=dict(body), label="d")
        said = saider.qb64

        # Extract ephemeral AID
        eaid = body.get("i")
        if not isinstance(eaid, str):
            raise falcon.HTTPBadRequest(
                title="Invalid ephemeral AID",
                description="Field 'i' is required",
            )

        # Extract signature header
        sig_header = _req.get_header("Signature")
        if not sig_header:
            raise falcon.HTTPUnauthorized(
                title="Missing signature",
                description="Signature header required",
            )

        # Verify signature over SAID
        prefixer = Prefixer(qb64=eaid)
        verfer = Verfer(raw=prefixer.raw, code=prefixer.code)

        siger = extract_siger_from_header(sig_header)

        if not verfer.verify(siger.raw, said.encode("utf-8")):
            raise falcon.HTTPUnauthorized(
                title="Invalid signature",
                description="Ephemeral AID signature invalid",
            )

        # Create a new session 
        record = self.ctx.store.add_session()

        # Build response
        respBody = {
            "session_id": record.eid,
            "status": record.status,
            "expires_at": record.expires_at,
            "d":""
        }

        # SAID-ify + sign response
        respSaid, respBody = coring.Saider.saidify(sad=respBody, label="d")
        sigers = self.hostHab.sign(ser=respSaid.qb64.encode("utf-8"))
        header = make_signature_header_from_sigers(sigers)

        rep.media = respBody
        rep.status = falcon.HTTP_201
        rep.set_header("Signature", header["Signature"])


class SessionResourceEnd:
    """
    Falcon resource for retrieving a single session by ID.
    """
    def __init__(self, ctx):
        """
        Initialize the session resource.

        Args:
            ctx: Application context providing access to the session store.
        """
        self.ctx = ctx

    def on_get(self, _req: falcon.Request, rep: falcon.Response, session_id: str) -> None:
        """
        Handle GET /session/{session_id} to fetch session details.

        Args:
            _req: Incoming Falcon request (unused).
            rep: Falcon response to populate.
            session_id: Identifier of the session to retrieve.

        Raises:
            falcon.HTTPNotFound: If the session does not exist.
        """
        record = self.ctx.store.get_session(session_id)
        if record is None:
            raise falcon.HTTPNotFound(title="Session not found")

        rep.media = record.to_api()


class SessionUpgradeEnd:
    """
    Falcon resource for upgrading a pending session to a permanent principal.

    This endpoint:
      - SAID-ifies the upgrade request body.
      - Verifies a permanent AID signature over the SAID.
      - Ensures the referenced session exists and is not expired.
      - Binds the permanent AID as the upgraded principal.
      - Returns a SAID-ified, host-signed upgraded session response.
    """
    def __init__(self, ctx):
        self.ctx = ctx
        self.hostHab = ctx.hostHab

    def on_post(self, req: falcon.Request, rep: falcon.Response) -> None:
        # Parse JSON body
        body = _request_json(req)
        
        if not isinstance(body, dict):
            raise falcon.HTTPBadRequest(
                title="Invalid request body",
                description="Expected JSON object",
            )
    
        # Saidify
        saider, body = coring.Saider.saidify(sad=dict(body), label="d")
        said = saider.qb64

        # Get CID
        cid = body.get("cid")

        if not isinstance(cid, str) or not cid:
            raise falcon.HTTPBadRequest(
                title="Invalid controller",
                description="Field 'cid' is required",
            )

        # Get session ID
        sessionId = body.get("session_id")

        # Load session record
        record = self.ctx.store.get_session(sessionId)

        # Check if session exists
        if record is None:
            raise falcon.HTTPNotFound(title="Session ID not found")

        # Check if session expired
        if record.is_expired:
            raise falcon.HTTPGone(title="Session expired")

        # Get Signature
        sig_header = req.get_header("Signature") 

        if not sig_header:
            raise falcon.HTTPUnauthorized(
                title="Missing signature",
                description="Signature header required",
            )

        # Verify signature over SAID
        siger = extract_siger_from_header(sig_header)

        pre = Prefixer(qb64=cid)
        verfer = Verfer(raw=pre.raw, code=pre.code)

        if not verfer.verify(siger.raw, said.encode("utf-8")):
            raise falcon.HTTPUnauthorized(
                title="Invalid signature",
                description="Permanent AID signature invalid",
            )

        # Bind permanent AID
        record.upgraded_principal = cid
        self.ctx.store.update_session(record)

        # Build response body
        resp_body = record.to_api()
        resp_body["d"] = "" 

        # SAID-ify + sign response
        resp_saider, resp_body = coring.Saider.saidify(sad=resp_body, label="d")
        resp_said = resp_saider.qb64

        sigers = self.hostHab.sign(ser=resp_said.encode("utf-8"))
        header = make_signature_header_from_sigers(sigers)

        rep.media = resp_body
        rep.status = falcon.HTTP_200
        rep.set_header("Signature", header["Signature"])