"""Account ownership metadata for the standalone mailbox provider.

This module does not host messages. Recipient consent is checked before a durable
Boot operation calls the provider's private registration API.
"""
from __future__ import annotations

from datetime import UTC, datetime
from secrets import token_urlsafe

import falcon
from keri.core import Parser
from keri.core.eventing import validateSigs
from keri.kering import Ilks, ValidationError, Vrsn_2_0

from kfboot.basing import MailboxRecord

BINDING_MAX_AGE_SECONDS = 300
# Wallet clocks may run slightly ahead of Boot.
BINDING_CLOCK_SKEW_SECONDS = 30


class MailboxStore:
    """Keep account ownership and recoverable provider allocation metadata."""

    def __init__(self, baser):
        self.db = baser

    def create(self, *, account_aid, recipient_aid, binding, limit):
        record = self.get(recipient_aid)
        if record and record.status != "deleted":
            if record.account_aid != account_aid:
                raise falcon.HTTPConflict(title="Recipient already has a mailbox")
            if record.status == "deleting":
                raise falcon.HTTPConflict(title="Mailbox deletion pending")
            if record.eid:
                return record
        else:
            if record and (binding.said == record.binding_said
                           or datetime.fromisoformat(binding.stamp) <= datetime.fromisoformat(record.deleted_at)):
                raise falcon.HTTPConflict(title="A new recipient binding is required")
            if len(self.list(account_aid)) >= limit:
                raise falcon.HTTPConflict(title="Mailbox allocation limit reached")
            record = MailboxRecord(mailbox_id=token_urlsafe(18), recipient_aid=recipient_aid,
                                   account_aid=account_aid)
        record.status = "pending"
        record.binding_said = binding.said
        self.save(record)
        return record

    def get(self, recipient_aid):
        return self.db.mailboxes.get(keys=(recipient_aid,))

    def save(self, record):
        self.db.mailboxes.pin(keys=(record.recipient_aid,), val=record)

    def list(self, account_aid):
        return [row for _, row in self.db.mailboxes.getTopItemIter()
                if row.account_aid == account_aid and row.status != "deleted"]

    def require(self, recipient_aid, *, account_aid, mailbox_id):
        record = self.get(recipient_aid)
        if record is None or record.account_aid != account_aid:
            raise falcon.HTTPNotFound(title="Mailbox not found")
        if record.mailbox_id != mailbox_id:
            raise falcon.HTTPConflict(title="Mailbox allocation changed")
        return record

    def delete(self, recipient_aid, *, account_aid, mailbox_id):
        record = self.require(recipient_aid, account_aid=account_aid, mailbox_id=mailbox_id)
        if record.status != "deleted":
            record.status = "deleted"
            record.deleted_at = datetime.now(UTC).isoformat()
            self.save(record)
        return record

    def deleteAccount(self, account_aid):
        # Never discard the only cleanup reference before the provider confirms removal.
        if self.list(account_aid):
            raise RuntimeError("Mailbox registrations must be removed before account metadata")


def parseMessages(raw, *, limit=1024):
    """Extract complete messages without parser dispatch or swallowed errors."""
    ims = bytearray(raw)
    parser = Parser(version=Vrsn_2_0)
    messages = []
    try:
        while ims:
            if len(messages) >= limit:
                raise ValueError("Too many messages")
            generator = parser.msgParsator(ims=ims, framed=True, local=False)
            try:
                next(generator)
            except StopIteration as done:
                messages.append(done.value)
            else:
                generator.close()
                raise ValueError("Incomplete CESR frame")
    except Exception as exc:
        raise falcon.HTTPBadRequest(title="Invalid CESR stream") from exc
    return messages


def verifyCurrentSignature(hby, parsed):
    """Require the sender's current signing threshold before any mailbox access."""
    serder = parsed.serder
    try:
        kever = hby.kevers[serder.pre]
    except KeyError as exc:
        raise falcon.HTTPUnauthorized(title="Unknown sender key state") from exc
    sigers = list(parsed.sigers)
    for prefixer, number, diger, attached in parsed.tsgs:
        if (prefixer.qb64 == serder.pre and number.num == kever.lastEst.s
                and diger.qb64 == kever.lastEst.d):
            sigers.extend(attached)
    for prefixer, attached in parsed.lsgs:
        if prefixer.qb64 == serder.pre:
            sigers.extend(attached)
    try:
        if sigers:
            _, valid = validateSigs(serder, sigers, kever.verfers, kever.tholder)
            if valid:
                return
        if not kever.prefixer.transferable:
            for cigar in parsed.cigars:
                if cigar.verfer.qb64 == serder.pre and cigar.verfer.verify(cigar.raw, serder.raw):
                    return
    except ValidationError:
        pass
    raise falcon.HTTPUnauthorized(title="Invalid sender signature")


def validateBinding(ctx, payload):
    binding = payload.get("binding")
    if not isinstance(binding, str) or len(binding.encode()) > 65536:
        raise falcon.HTTPBadRequest(title="Recipient binding is required")
    messages = parseMessages(binding.encode())
    if len(messages) != 1:
        raise falcon.HTTPBadRequest(title="One recipient binding is required")
    parsed = messages[0]
    serder = parsed.serder
    if (serder.ilk != Ilks.exn or serder.pvrsn != Vrsn_2_0
            or serder.route != "/mailbox/bind" or serder.pre != payload.get("recipient_aid")
            or serder.ked.get("ri") != ctx.host_hab.pre
            or serder.ked.get("a") != {"account_aid": payload.get("account_aid"), "recipient_aid": serder.pre}):
        raise falcon.HTTPBadRequest(title="Recipient binding does not match this allocation")
    verifyCurrentSignature(ctx.habery, parsed)
    try:
        age = (datetime.now(UTC) - datetime.fromisoformat(serder.stamp)).total_seconds()
        if not -BINDING_CLOCK_SKEW_SECONDS <= age <= BINDING_MAX_AGE_SECONDS:
            raise ValueError("Binding expired")
    except (ValueError, TypeError) as exc:
        raise falcon.HTTPBadRequest(title="Recipient binding has expired") from exc
    return serder


def mailboxPayload(ctx, record):
    return {"mailbox_id": record.mailbox_id, "recipient_aid": record.recipient_aid,
            "eid": record.eid, "url": record.url, "oobis": list(record.oobis),
            "status": record.status}
