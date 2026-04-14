# kfboot/boot_exchanger.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from keri.peer.exchanging import Exchanger
from keri.help import helping
from keri.peer import exchanging

from keri.core.counting import Counter, CtrDex_1_0
from keri.core import Codens
from keri.kering import Vrsn_1_0

from kfboot.store import make_record

@dataclass
class BootContext:
    habery: Any
    hostHab: Any
    store: Any
    witness_boot: Any
    watcher_boot: Any


class OnboardingHandler:
    resource: str = ""

    def __init__(self, exn: "BootExchanger"):
        self.exn = exn

    def verify(self, serder, **kwa) -> bool:
        return True

    def handle(self, serder, **kwa):
        raise NotImplementedError


class SessionStartHandler(OnboardingHandler):
    resource = "/onboarding/session/start"

    def handle(self, serder, **kwa):
        exn = self.exn
        sender = serder.pre
        # Create session and mark as started
        session = exn.store.add_session()
        session_id = session.eid
        exn.store.update_session(session_id, state="started")

        try:
            # Allocate witness pool
            wresp = exn.witness_boot.create_witness(sender)

            witness_ids = wresp.get("eid")
            oobi = wresp.get("oobis")
            # Create record for the witness
            record = make_record(
                kind="witness",
                eid=witness_ids,
                cid=sender,                 # controller id
                principal=sender,           # owner of resource
                name=f"witness-{witness_ids[:6]}",  # arbitrary label
                identifier_alias="",        # optional
                region_id="",               # optional
                region_name="",             # optional
                public_url="",              # optional
                oobis=oobi,                # store OOBIs
            )

            # Store the witness record
            exn.store.add_resource(record)

            # Allocate watcher
            wresp2 = exn.watcher_boot.create_watcher(sender, oobi=oobi[0])
            watcher_id = wresp2.get("eid")

            # Create watcher record
            watcher_record = make_record(
                kind="watcher",
                eid=watcher_id,
                cid=sender,
                principal=sender,
                name=f"watcher-{watcher_id[:6]}",
                identifier_alias="",
                region_id="",
                region_name="",
                public_url="",
                oobis=[oobi[0]],
            )

            # Store watcher record
            exn.store.add_resource(watcher_record)

            # Record resources before replying
            exn.store.update_session(
                session_id,
                state="witness_pool_allocated",
                witness_ids=witness_ids,
                watcher_id=watcher_id,
            )
        except Exception:
            exn.store.update_session(
                session_id, state="failed"
            )
            raise

        # Build reply
        reply = {
            "session_id": session_id,
            "witnesses": witness_ids,
            "watcher": watcher_id,
            "state": "witness_pool_allocated",
        }
        msg = exn._reply(self.resource, sender, reply)
        exn.cues.append({"kin": "reply", "msg": msg})


class SessionStatusHandler(OnboardingHandler):
    resource = "/onboarding/session/status"

    def handle(self, serder, **kwa):
        exn = self.exn
        sender = serder.pre
        payload = serder.ked["a"]

        session_id = payload["session_id"]
        session = exn.store.get_session(session_id)

        reply = {
            "session_id": session_id,
            "state": session.state,
            "witnesses": session.witness_ids,
            "watcher": session.watcher_id,
        }

        msg = exn._reply(self.resource, sender, reply)
        exn.cues.append({"kin": "reply", "msg": msg})


class AccountCreateHandler(OnboardingHandler):
    resource = "/onboarding/account/create"

    def handle(self, serder, **kwa):
        exn = self.exn
        sender = serder.pre
        payload = serder.ked["a"]

        session_id = payload["session_id"]
        session = exn.store.get_session(session_id)

        # Idempotent within a session
        if session.state == "account_created":
            reply = {
                "session_id": session_id,
                "principal": session.account_aid,
                "state": "account_created",
            }
            msg = exn._reply(self.resource, sender, reply)
            exn.cues.append({"kin": "reply", "msg": msg})
            return

        try:
            exn.store.update_session(
                session_id=session_id,
                state="account_created",
                account_aid=sender,
            )
        except Exception:
            exn.store.update_session(
                session_id=session_id,
                state="failed",
            )
            raise

        reply = {
            "session_id": session_id,
            "principal": sender,
            "state": "account_created",
        }

        msg = exn._reply(self.resource, sender, reply)
        exn.cues.append({"kin": "reply", "msg": msg})


class CompleteHandler(OnboardingHandler):
    resource = "/onboarding/complete"

    def handle(self, serder, **kwa):
        exn = self.exn
        sender = serder.pre
        payload = serder.ked["a"]

        session_id = payload["session_id"]
        session = exn.store.get_session(session_id)

        # Idempotent within a session
        if session.state == "completed":
            reply = {
                "session_id": session_id,
                "principal": session.account_aid,
                "state": "completed",
            }
            msg = exn._reply(self.resource, sender, reply)
            exn.cues.append({"kin": "reply", "msg": msg})
            return

        if not session.witness_ids or not session.watcher_id:
            raise ValueError("Cannot complete onboarding: witness pool or watcher missing")

        if session.account_aid != sender:
            raise ValueError("Only the permanent account AID may complete onboarding")

        try:
            exn.store.update_session(session_id=session_id, state="completed")
        except Exception:
            exn.store.update_session(session_id=session_id, state="failed")
            raise

        reply = {
            "session_id": session_id,
            "principal": sender,
            "state": "completed",
        }

        msg = exn._reply(self.resource, sender, reply)
        exn.cues.append({"kin": "reply", "msg": msg})


class CancelHandler(OnboardingHandler):
    resource = "/onboarding/cancel"

    def handle(self, serder, **kwa):
        exn = self.exn
        sender = serder.pre
        payload = serder.ked["a"]

        session_id = payload["session_id"]
        session = exn.store.get_session(session_id)

        # Optional: enforce that only the same principal can cancel
        if session.account_aid and session.account_aid != sender:
            raise ValueError("Only the session principal may cancel onboarding")

        exn.store.update_session(session_id=session_id, state="cancelled")

        reply = {
            "session_id": session_id,
            "state": "cancelled",
        }

        msg = exn._reply(self.resource, sender, reply)
        exn.cues.append({"kin": "reply", "msg": msg})


class AccountWitnessesQueryHandler(OnboardingHandler):
    resource = "/account/witnesses"

    def handle(self, serder, **kwa):
        exn = self.exn
        sender = serder.pre

        witnesses = exn.store.list_resources(
            kind="witness",
            principal=sender,
            is_admin=False,
            page=0,
            page_size=1000,
        )

        reply = {"witnesses": witnesses}

        msg = exn._reply(self.resource, sender, reply)
        exn.cues.append({"kin": "reply", "msg": msg})


class AccountWatchersQueryHandler(OnboardingHandler):
    resource = "/account/watchers"

    def handle(self, serder, **kwa):
        exn = self.exn
        sender = serder.pre

        watchers = exn.store.list_resources(
            kind="watcher",
            principal=sender,
            is_admin=False,
            page=0,
            page_size=1000,
        )

        reply = {"watchers": watchers}

        msg = exn._reply(self.resource, sender, reply)
        exn.cues.append({"kin": "reply", "msg": msg})


class AccountWatcherStatusQueryHandler(OnboardingHandler):
    resource = "/account/watchers/status"

    def handle(self, serder, **kwa):
        exn = self.exn
        sender = serder.pre
        payload = serder.ked["a"]

        watcher_id = payload["watcher_id"]

        record = exn.store.get_resource("watcher", watcher_id)
        if record is None or record.principal != sender:
            raise ValueError("Watcher not found or not owned by account")

        status = exn.watcher_boot.watcher_status(watcher_id)

        reply = {
            "watcher_id": watcher_id,
            "status": status,
        }

        msg = exn._reply(self.resource, sender, reply)
        exn.cues.append({"kin": "reply", "msg": msg})


class AccountWitnessDeleteHandler(OnboardingHandler):
    resource = "/account/witnesses/delete"

    def handle(self, serder, **kwa):
        exn = self.exn
        sender = serder.pre
        payload = serder.ked["a"]

        witness_id = payload["witness_id"]

        record = exn.store.get_resource("witness", witness_id)
        if record is None:
            raise ValueError("Witness not found")
        
        #TODO Check sender AID to make sure the witness belongs to them
        
        exn.witness_boot.delete_witness(witness_id)
        exn.store.delete_resource("witness", witness_id)

        reply = {"witness_id": witness_id, "deleted": True}

        msg = exn._reply(self.resource, sender, reply)
        exn.cues.append({"kin": "reply", "msg": msg})


class AccountWatcherDeleteHandler(OnboardingHandler):
    resource = "/account/watchers/delete"

    def handle(self, serder, **kwa):
        exn = self.exn
        sender = serder.pre
        payload = serder.ked["a"]

        watcher_id = payload["watcher_id"]

        record = exn.store.get_resource("watcher", watcher_id)
        if record is None:
            raise ValueError("Watcher not found or not owned by account")

        exn.watcher_boot.delete_watcher(watcher_id)
        exn.store.delete_resource("watcher", watcher_id)

        reply = {"watcher_id": watcher_id, "deleted": True}

        msg = exn._reply(self.resource, sender, reply)
        exn.cues.append({"kin": "reply", "msg": msg})


class BootExchanger(Exchanger):
    def __init__(self, ctx: BootContext):
        self.ctx = ctx
        super().__init__(hby=ctx.habery, handlers=[])

        self.hab = ctx.hostHab
        self.store = ctx.store
        self.witness_boot = ctx.witness_boot
        self.watcher_boot = ctx.watcher_boot

        self.addHandler(SessionStartHandler(self))
        self.addHandler(SessionStatusHandler(self))
        self.addHandler(AccountCreateHandler(self))
        self.addHandler(CompleteHandler(self))
        self.addHandler(CancelHandler(self))
        self.addHandler(AccountWitnessesQueryHandler(self))
        self.addHandler(AccountWatchersQueryHandler(self))
        self.addHandler(AccountWatcherStatusQueryHandler(self))
        self.addHandler(AccountWitnessDeleteHandler(self))
        self.addHandler(AccountWatcherDeleteHandler(self))

    def _reply(self, route: str, recipient: str, payload: dict) -> bytes:
        dt = helping.nowIso8601()
        serder, end = exchanging.exchange(
            route=route,
            sender=self.hab.pre,
            payload=payload,
            recipient=recipient,
            date=dt,
        )
        # Sign EXN
        sigs = self.hab.sign(ser=serder.raw, indexed=True)

        msg = bytearray(serder.raw)

        # Add embedded attachments
        msg.extend(end)

        # Add ControllerIdxSigs counter
        msg.extend(
            Counter(
                CtrDex_1_0.ControllerIdxSigs,
                count=len(sigs),
                version=Vrsn_1_0,
            ).qb64b
        )

        # Add signatures
        for sig in sigs:
            msg.extend(sig.qb64b)
        return bytes(msg)
