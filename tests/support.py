from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

from falcon import testing
from keri.app.httping import CESR_ATTACHMENT_HEADER, CESR_CONTENT_TYPE
from keri.core import exchange
from keri.core.serdering import SerderKERI
from keri.kering import Kinds, Vrsn_2_0
from kfboot.store import Store, makeRecord
from kfboot.basing import CLEANUP_TASK_SESSION_CLEANUP, CLEANUP_TASK_SESSION_EXPIRE

from kfboot.boot_client import BootError
from kfboot.config import Config, WitnessBackend
from kfboot.operating import BootOperationDoer, BootOperationProcessor


class FakeWitnessBoot:
    def __init__(
        self,
        backend_id: str,
        *,
        base_url: str,
        public_url: str,
        create_error: Exception | None = None,
        delete_error: Exception | None = None,
    ):
        self.backend_id = backend_id
        self.base_url = base_url
        self.public_url = public_url
        self.create_calls = 0
        self.create_cids: list[str] = []
        self.created_eids: list[str] = []
        self.delete_calls: list[str] = []
        self.create_error = create_error
        self.delete_error = delete_error

    def createWitness(self, cid: str) -> dict[str, Any]:
        self.create_calls += 1
        self.create_cids.append(cid)
        if self.create_error is not None:
            raise self.create_error
        eid = f"{self.backend_id.upper().replace('-', '_')}_{self.create_calls}"
        self.created_eids.append(eid)
        return {
            "eid": eid,
            "cid": cid,
            "name": f"{self.backend_id} witness {self.create_calls}",
            "oobis": [f"{self.public_url}/oobi/{eid}/controller"],
            "status": "allocated",
        }

    def allocateWitness(self, account_aid: str) -> dict[str, Any]:
        return self.createWitness(account_aid)

    def allocateWitnessDo(
        self,
        account_aid: str,
        *,
        idempotency_key: str = "",
        tymth,
        tock: float = 0.0,
    ):
        yield tock
        return self.allocateWitness(account_aid)

    def deleteWitness(self, eid: str) -> None:
        self.delete_calls.append(eid)
        if self.delete_error is not None:
            raise self.delete_error

    def deleteWitnessDo(self, eid: str, *, tymth, tock: float = 0.0):
        yield tock
        self.deleteWitness(eid)


class FakeWatcherBoot:
    base_url = "http://boot.local/watchers"

    def __init__(
        self,
        *,
        create_error: Exception | None = None,
        delete_error: Exception | None = None,
        status_error: Exception | None = None,
        status_response: dict[str, Any] | None = None,
    ):
        self.create_calls = 0
        self.create_cids: list[str] = []
        self.create_oobis: list[str | None] = []
        self.delete_calls: list[str] = []
        self.status_calls: list[str] = []
        self.create_error = create_error
        self.delete_error = delete_error
        self.status_error = status_error
        self.status_response = status_response or {
            "controller_id": "AID_ACCOUNT",
            "summary": {
                "total_witnesses": 1,
                "responsive_witnesses": 1,
            },
        }

    def createWatcher(self, cid: str, oobi: str | None = None) -> dict[str, Any]:
        self.create_calls += 1
        self.create_cids.append(cid)
        self.create_oobis.append(oobi)
        if self.create_error is not None:
            raise self.create_error
        eid = f"WAT_{self.create_calls}"
        return {
            "eid": eid,
            "cid": cid,
            "name": f"Watcher {self.create_calls}",
            "oobis": [f"https://watcher-{self.create_calls}.example/oobi/{eid}/controller"],
            "status": "created",
            "oobi": oobi or "",
        }

    def allocateWatcher(self, account_aid: str, oobi: str | None = None) -> dict[str, Any]:
        return self.createWatcher(account_aid, oobi=oobi)

    def allocateWatcherDo(
        self,
        account_aid: str,
        *,
        oobi: str | None = None,
        idempotency_key: str = "",
        tymth,
        tock: float = 0.0,
    ):
        yield tock
        return self.allocateWatcher(account_aid, oobi=oobi)

    def deleteWatcher(self, eid: str) -> None:
        self.delete_calls.append(eid)
        if self.delete_error is not None:
            raise self.delete_error

    def deleteWatcherDo(self, eid: str, *, tymth, tock: float = 0.0):
        yield tock
        self.deleteWatcher(eid)

    def watcherStatus(self, eid: str) -> dict[str, Any]:
        self.status_calls.append(eid)
        if self.status_error is not None:
            raise self.status_error
        return {"eid": eid, **self.status_response}

    def watcherStatusDo(self, eid: str, *, tymth, tock: float = 0.0):
        yield tock
        return self.watcherStatus(eid)


def make_witness_backends(count: int = 4) -> tuple[WitnessBackend, ...]:
    return tuple(
        WitnessBackend(
            id=f"wit-{index}",
            boot_url=f"http://127.0.0.1:{5630 + (index * 10) + 1}",
            public_url=f"https://boot.example.com:{5630 + (index * 10) + 2}",
        )
        for index in range(1, count + 1)
    )


def make_witness_boots(
    backends: tuple[WitnessBackend, ...],
    *,
    overrides: dict[str, FakeWitnessBoot] | None = None,
) -> dict[str, FakeWitnessBoot]:
    boots = {
        backend.id: FakeWitnessBoot(
            backend.id,
            base_url=backend.boot_url,
            public_url=backend.public_url,
        )
        for backend in backends
    }
    if overrides:
        boots.update(overrides)
    return boots


def total_witness_create_calls(ctx) -> int:
    return sum(client.create_calls for client in ctx.witness_boots.values())


def total_witness_created_eids(ctx) -> list[str]:
    eids: list[str] = []
    for backend in ctx.config.witness_backends:
        client = ctx.witness_boots.get(backend.id)
        if client is None:
            continue
        eids.extend(client.created_eids)
    return eids


def total_witness_delete_calls(ctx) -> list[str]:
    calls: list[str] = []
    for backend in ctx.config.witness_backends:
        client = ctx.witness_boots.get(backend.id)
        if client is None:
            continue
        calls.extend(client.delete_calls)
    return calls


def drain_do(gen, *, max_steps: int = 200):
    for _ in range(max_steps):
        try:
            next(gen)
        except StopIteration as ex:
            return ex.value
    raise AssertionError("do generator did not finish")


def sweep_do(
    expirer,
    *,
    now: str | None = None,
    batch_size: int | None = None,
    time_budget_seconds: float | None = None,
):
    return drain_do(
        expirer.sweepDo(
            batch_size=batch_size,
            time_budget_seconds=time_budget_seconds,
            now=now,
            tymth=lambda: 0.0,
            tock=0.0,
        )
    )


def run_boot_operations(client: testing.TestClient, *, max_steps: int = 200):
    doer = BootOperationDoer(
        store=client.ctx.store,
        witness_boots=client.ctx.witness_boots,
        watcher_boot=client.ctx.watcher_boot,
        processor=BootOperationProcessor(provisioner=client.ctx.exchanger.provisioner),
        batch_size=100,
    )
    return drain_do(
        doer.processDueDo(
            tymth=lambda: 0.0,
            tock=0.0,
        ),
        max_steps=max_steps,
    )


def make_config(tmp_path, *, index: int = 0, **overrides: Any) -> Config:
    witness_backends = overrides.pop("witness_backends", make_witness_backends())
    data = {
        "host": "127.0.0.1",
        "port": 9723,
        "db_path": str(tmp_path / f"store-{index}" / "kf-boot"),
        "keri_dir": str(tmp_path / f"var-{index}"),
        "keri_name": f"kf-boot-test-{index}",
        "boot_hab_name": "boot-server",
        "onboarding_path": "/onboarding",
        "account_path": "/account",
        "onboarding_public_url": "http://127.0.0.1:9723/onboarding",
        "account_public_url": "http://127.0.0.1:9723/account",
        "region_id": "test-region",
        "region_name": "Test Region",
        "witness_limit": 100,
        "watcher_limit": 100,
        "wit_boot_url": witness_backends[0].boot_url,
        "wit_public_url": witness_backends[0].public_url,
        "wat_boot_url": "http://boot.local/watchers",
        "wat_public_url": "https://watcher.example",
        "bootstrap_account_options": ("1-of-1", "3-of-4"),
        "bootstrap_watcher_required": True,
        "bootstrap_accounts_per_ip": 1,
        "bootstrap_aids_per_ip": 10,
        "bootstrap_api_requests_per_minute": 10,
        "boot_api_timeout_seconds": 10,
        "account_ttl_seconds": 3600,
        "closed_session_retention_seconds": 300,
        "cleanup_runner_enabled": True,
        "session_ttl_seconds": 300,
        "cleanup_interval_seconds": 0,
        "cleanup_batch_size": 100,
        "cleanup_time_budget_seconds": 5,
        "cleanup_failure_backoff_seconds": 60,
        "cleanup_failure_backoff_max_seconds": 900,
        "cleanup_failure_jitter_seconds": 0,
        "cleanup_block_after_attempts": 10,
        "cleanup_block_after_failure_age_seconds": 86400,
        "expired_account_retention_seconds": 0,
        "witness_backends": witness_backends,
    }
    data.update(overrides)
    return Config(**data)


def freeze_boot_time(monkeypatch, current: datetime):
    import kfboot.expiring as expiring
    import kfboot.limiting as limiting
    import kfboot.store as store
    
    class FrozenDateTime:
        value = current

        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return cls.value.replace(tzinfo=None)
            if cls.value.tzinfo is None:
                return cls.value.replace(tzinfo=tz)
            return cls.value.astimezone(tz)

        @classmethod
        def fromisoformat(cls, value: str):
            return datetime.fromisoformat(value)

    monkeypatch.setattr(expiring, "datetime", FrozenDateTime)
    monkeypatch.setattr(limiting, "datetime", FrozenDateTime)
    monkeypatch.setattr(store, "datetime", FrozenDateTime)
    return FrozenDateTime


def start_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "account_aid": "AID_ACCOUNT",
        "account_alias": "alpha",
        "chosen_profile_code": "1-of-1",
        "region_id": "test-region",
        "watcher_required": True,
    }
    payload.update(overrides)
    return payload


def account_create_payload(start_reply: SerderKERI, account_aid: str, **overrides: Any) -> dict[str, Any]:
    session = start_reply.ked["a"]
    watcher = session.get("watcher") or {}
    payload = {
        "session_id": session["session_id"],
        "account_aid": account_aid,
        "account_alias": session["account_alias"],
        "chosen_profile_code": session["chosen_profile_code"],
        "region_id": session["region_id"],
        "witness_eids": [row["eid"] for row in session["witnesses"]],
        "watcher_eid": watcher.get("eid", ""),
    }
    payload.update(overrides)
    return payload


def build_signed_serder(hab, serder: SerderKERI, *, end: bytes | bytearray = b"") -> bytes:
    ims = hab.endorse(serder=serder, last=False, framed=True)
    attachment = bytearray(ims)
    del attachment[:serder.size]
    if end:
        attachment.extend(end)
    return bytes(serder.raw) + bytes(attachment)


def build_exn(hab, *, route: str, payload: dict[str, Any]) -> bytes:
    serder = exchange(
        sender=hab.pre,
        route=route,
        attributes=payload,
        version=Vrsn_2_0,
        pvrsn=Vrsn_2_0,
        gvrsn=Vrsn_2_0,
        kind=Kinds.json,
    )
    return build_signed_serder(hab, serder)


def split_cesr_message(ims: bytes | bytearray) -> tuple[bytes, bytes]:
    buf = bytearray(ims)
    serder = SerderKERI(raw=bytes(buf))
    return bytes(buf[:serder.size]), bytes(buf[serder.size:])


def post_cesr(
    client: testing.TestClient,
    path: str,
    ims: bytes | bytearray,
    *,
    remote_addr: str | None = None,
):
    body, attachment = split_cesr_message(ims)
    headers = {"Content-Type": CESR_CONTENT_TYPE}
    if attachment:
        headers[CESR_ATTACHMENT_HEADER] = attachment.decode("utf-8")
    return client.simulate_post(path, body=body, headers=headers, remote_addr=remote_addr)


def split_cesr_stream(ims: bytes | bytearray) -> list[SerderKERI]:
    serders: list[SerderKERI] = []
    buf = bytearray(ims)
    while buf:
        serder = SerderKERI(raw=bytes(buf))
        serders.append(serder)
        del buf[:serder.size]
        while buf and buf[0] != 0x7B:
            del buf[:1]
    return serders


def parse_reply_stream(stream: bytes | bytearray) -> tuple[list[SerderKERI], SerderKERI]:
    serders = split_cesr_stream(stream)
    return serders, serders[-1]


def assert_reply_frame(client: testing.TestClient, response, *, route: str) -> tuple[list[SerderKERI], SerderKERI]:
    assert response.status_code == 200
    assert response.content_type == CESR_CONTENT_TYPE
    assert response.content.startswith(client.ctx.exchanger.hostKELReplay())
    serders, reply = parse_reply_stream(response.content)
    assert len(serders) >= 2
    assert reply.ked["r"] == route
    assert reply.ked["i"] == client.ctx.host_hab.pre
    return serders, reply


def register_aid(client: testing.TestClient, path: str, hab) -> None:
    response = post_cesr(client, path, hab.msgOwnInception())
    assert response.status_code == 204


# def own_inception_message(hab) -> bytes | bytearray:
#     if hasattr(hab, "makeOwnInception"):
#         return hab.makeOwnInception()
#     return hab.msgOwnInception(framed=True)


# def own_event_message(hab, *, sn: int) -> bytes | bytearray:
#     if hasattr(hab, "makeOwnEvent"):
#         return hab.makeOwnEvent(sn=sn)
#     return hab.msgOwnEvent(sn=sn, framed=True)


def session_status(client: testing.TestClient, hab, session_id: str) -> tuple[Any, list[SerderKERI], SerderKERI]:
    response = post_cesr(
        client,
        "/onboarding",
        build_exn(hab, route="/onboarding/session/status", payload={"session_id": session_id}),
    )
    serders, reply = assert_reply_frame(client, response, route="/onboarding/session/status")
    return response, serders, reply


def start_session(
    client: testing.TestClient,
    hab,
    *,
    drain_operations: bool = True,
    **overrides: Any,
) -> tuple[Any, list[SerderKERI], SerderKERI]:
    response = post_cesr(
        client,
        "/onboarding",
        build_exn(hab, route="/onboarding/session/start", payload=start_payload(**overrides)),
    )
    serders, reply = assert_reply_frame(client, response, route="/onboarding/session/start")
    if drain_operations:
        run_boot_operations(client)
        _, _, status_reply = session_status(client, hab, reply.ked["a"]["session_id"])
        ked = dict(status_reply.ked)
        ked["r"] = reply.ked["r"]
        reply = SimpleNamespace(ked=ked)
    return response, serders, reply


def create_account(
    client: testing.TestClient,
    hab,
    start_reply: SerderKERI,
    *,
    account_aid: str | None = None,
    **overrides: Any,
) -> tuple[Any, list[SerderKERI], SerderKERI]:
    session = start_reply.ked["a"]
    payload = account_create_payload(start_reply, account_aid or session["account_aid"], **overrides)
    response = post_cesr(
        client,
        "/onboarding",
        build_exn(hab, route="/onboarding/account/create", payload=payload),
    )
    serders, reply = assert_reply_frame(client, response, route="/onboarding/account/create")
    return response, serders, reply


def complete_session(
    client: testing.TestClient,
    hab,
    *,
    session_id: str,
    account_aid: str | None = None,
) -> tuple[Any, list[SerderKERI], SerderKERI]:
    payload_account_aid = account_aid or "AID_ACCOUNT"
    response = post_cesr(
        client,
        "/onboarding",
        build_exn(
            hab,
            route="/onboarding/complete",
            payload={"session_id": session_id, "account_aid": payload_account_aid},
        ),
    )
    serders, reply = assert_reply_frame(client, response, route="/onboarding/complete")
    return response, serders, reply


def boot_error(status_code: int, description: str | None = None) -> BootError:
    return BootError(description or f"boot error {status_code}", status_code=status_code)


def makeBlockedTask(tmp_path):
    """Helper to create a blocked cleanup task"""
    store = Store(str(tmp_path / "cli-store" / "kf-boot"), session_ttl_seconds=60)
    session = store.createSession(
        ephemeral_aid="E-cli",
        account_aid="A-cli",
        account_alias="alpha",
        chosen_profile_code="1-of-1",
        client_ip="127.0.0.1",
        region_id="test-region",
        region_name="Test Region",
        watcher_required=True,
        witness_count=1,
        toad=1,
        account_tier="trial",
    )
    session.expires_at = "2024-01-01T00:00:00+00:00"
    store.saveSession(session)
    store.claimDueCleanupTask(now="2024-01-01T00:00:05+00:00")
    store.blockCleanupTask(
        CLEANUP_TASK_SESSION_EXPIRE,
        session.session_id,
        now="2024-01-01T00:00:06+00:00",
        blocked_reason="simulated operator task",
        last_error="simulated failure",
        first_failed_at="2024-01-01T00:00:05+00:00",
    )
    store.close()
    return str(tmp_path / "cli-store" / "kf-boot"), session.session_id


def makeBlockedOrphanTask(tmp_path):
    """Helper to create a blocked cleanup task with no associated session"""
    db_path = str(tmp_path / "cli-orphan" / "kf-boot")
    store = Store(db_path, session_ttl_seconds=60)
    store.scheduleCleanupTask(
        CLEANUP_TASK_SESSION_CLEANUP,
        "missing-session",
        due_at="2024-01-01T00:00:00+00:00",
        now="2024-01-01T00:00:00+00:00",
    )
    store.blockCleanupTask(
        CLEANUP_TASK_SESSION_CLEANUP,
        "missing-session",
        now="2024-01-01T00:00:01+00:00",
        blocked_reason="orphaned task",
        last_error="simulated failure",
        first_failed_at="2024-01-01T00:00:00+00:00",
    )
    store.close()
    return db_path


def makeBlockedOrphanTaskWithResource(tmp_path):
    """Helper to create a blocked orphaned session task with leftover resources."""

    db_path = str(tmp_path / "cli-orphan-resource" / "kf-boot")
    store = Store(db_path, session_ttl_seconds=60)
    store.scheduleCleanupTask(
        CLEANUP_TASK_SESSION_CLEANUP,
        "missing-session",
        due_at="2024-01-01T00:00:00+00:00",
        now="2024-01-01T00:00:00+00:00",
    )
    store.blockCleanupTask(
        CLEANUP_TASK_SESSION_CLEANUP,
        "missing-session",
        now="2024-01-01T00:00:01+00:00",
        blocked_reason="orphaned task with resources",
        last_error="simulated failure",
        first_failed_at="2024-01-01T00:00:00+00:00",
    )
    store.addResource(
        makeRecord(
            kind="watcher",
            eid="WAT_ORPHAN",
            backend_id="wat-1",
            cid="",
            principal="",
            session_id="missing-session",
            name="orphan watcher",
            identifier_alias="alpha",
            region_id="test-region",
            region_name="Test Region",
            public_url="https://watcher.example",
            boot_url="http://boot.local/watchers",
            oobis=[],
            status="created",
        )
    )
    store.close()
    return db_path
