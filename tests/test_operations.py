from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hio.base import doing
from keri.app import habbing

from kfboot.app import create_app
from kfboot.basing import (
    BOOT_OPERATION_ACCOUNT_DELETE,
    BOOT_OPERATION_FAILED,
    BOOT_OPERATION_PENDING,
    BOOT_OPERATION_RUNNING,
    BOOT_OPERATION_SESSION_PROVISION,
    BOOT_OPERATION_SUCCEEDED,
    BOOT_OPERATION_WATCHER_STATUS_QUERY,
    CLEANUP_TASK_SESSION_CLEANUP,
    SESSION_STATE_FAILED,
)
from kfboot.boot_client import BootError, HioBootClient
from kfboot.operating import BootOperationDoer
from kfboot.runtime import build_doers
from kfboot.store import Store

from .support import assert_reply_frame, build_exn, drain_do, make_config, post_cesr, register_aid, start_session


@pytest.fixture
def store(tmp_path):
    instance = Store(str(tmp_path / "operation-store" / "kf-boot"), session_ttl_seconds=60)
    yield instance
    instance.close()


def test_boot_operation_create_reuse_persistence_and_payload_copy(tmp_path):
    path = str(tmp_path / "persisted-operations" / "kf-boot")
    first = Store(path, session_ttl_seconds=60)
    try:
        payload = {"session_id": "sess_1", "resources": ["witness"]}
        operation = first.ensureBootOperation(
            kind=BOOT_OPERATION_SESSION_PROVISION,
            subject="sess_1",
            requester="E1",
            route="/onboarding/session/start",
            idempotency_key="session:sess_1",
            payload=payload,
            due_at="2026-01-01T00:00:00+00:00",
            now="2026-01-01T00:00:00+00:00",
        )
        payload["resources"].append("mutated")

        duplicate = first.ensureBootOperation(
            kind=BOOT_OPERATION_SESSION_PROVISION,
            subject="sess_1",
            requester="E1",
            route="/onboarding/session/start",
            idempotency_key="session:sess_1:retry",
            payload={"session_id": "sess_1", "resources": ["other"]},
            due_at="2026-01-01T00:01:00+00:00",
            now="2026-01-01T00:01:00+00:00",
        )
        payload_view = first.bootOperationPayload(operation)
        payload_view["payload"]["resources"].append("view-mutated")

        assert operation.operation_id.startswith("op_")
        assert duplicate.operation_id == operation.operation_id
        assert first.getBootOperation(operation.operation_id).payload == {
            "session_id": "sess_1",
            "resources": ["witness"],
        }
    finally:
        first.close()

    second = Store(path, session_ttl_seconds=60)
    try:
        recovered = second.getBootOperation(operation.operation_id)

        assert recovered is not None
        assert recovered.kind == BOOT_OPERATION_SESSION_PROVISION
        assert recovered.subject == "sess_1"
        assert recovered.requester == "E1"
        assert recovered.route == "/onboarding/session/start"
        assert recovered.state == BOOT_OPERATION_PENDING
        assert recovered.payload == {"session_id": "sess_1", "resources": ["witness"]}
        claimed = second.claimDueBootOperation(now="2026-01-01T00:00:00+00:00")
        assert claimed is not None
        assert claimed.operation_id == operation.operation_id
    finally:
        second.close()


def test_boot_operation_claim_recover_and_due_ordering(store):
    first = store.ensureBootOperation(
        kind=BOOT_OPERATION_WATCHER_STATUS_QUERY,
        subject="watcher:WA1",
        requester="A1",
        route="/account/watchers/status",
        payload={"watcher_id": "WA1"},
        due_at="2026-01-01T00:00:00+00:00",
        now="2026-01-01T00:00:00+00:00",
    )
    second = store.ensureBootOperation(
        kind=BOOT_OPERATION_WATCHER_STATUS_QUERY,
        subject="watcher:WA2",
        requester="A1",
        route="/account/watchers/status",
        payload={"watcher_id": "WA2"},
        due_at="2026-01-01T00:00:05+00:00",
        now="2026-01-01T00:00:00+00:00",
    )

    assert store.claimDueBootOperation(now="2025-12-31T23:59:59+00:00") is None

    claimed = store.claimDueBootOperation(now="2026-01-01T00:00:04+00:00")
    assert claimed is not None
    assert claimed.operation_id == first.operation_id
    assert claimed.state == BOOT_OPERATION_RUNNING
    assert claimed.due_at == ""
    assert claimed.claimed_at == "2026-01-01T00:00:04+00:00"
    assert claimed.last_attempt_at == "2026-01-01T00:00:04+00:00"
    assert claimed.attempt_count == 1
    assert store.claimDueBootOperation(now="2026-01-01T00:00:04+00:00") is None

    recovered = store.requeueClaimedBootOperations(now="2026-01-01T00:00:02+00:00")
    assert recovered == 1
    assert store.getBootOperation(first.operation_id).state == BOOT_OPERATION_PENDING
    assert store.getBootOperation(first.operation_id).claimed_at == ""
    assert store.getBootOperation(first.operation_id).due_at == "2026-01-01T00:00:02+00:00"
    assert store.claimDueBootOperation(now="2026-01-01T00:00:05+00:00").operation_id == first.operation_id
    assert store.claimDueBootOperation(now="2026-01-01T00:00:05+00:00").operation_id == second.operation_id


def test_boot_operation_reschedule_success_failure_and_terminal_recreation(store):
    operation = store.ensureBootOperation(
        kind=BOOT_OPERATION_ACCOUNT_DELETE,
        subject="account:A1",
        requester="A1",
        route="/account/delete",
        payload={"account_aid": "A1"},
        due_at="2026-01-01T00:00:00+00:00",
        now="2026-01-01T00:00:00+00:00",
    )

    assert store.claimDueBootOperation(now="2026-01-01T00:00:01+00:00").attempt_count == 1

    rescheduled = store.rescheduleBootOperation(
        operation.operation_id,
        due_at="2026-01-01T00:00:10+00:00",
        now="2026-01-01T00:00:02+00:00",
        last_error="downstream unavailable",
    )
    assert rescheduled is not None
    assert rescheduled.state == BOOT_OPERATION_PENDING
    assert rescheduled.claimed_at == ""
    assert rescheduled.last_error == "downstream unavailable"
    assert rescheduled.attempt_count == 1
    assert store.claimDueBootOperation(now="2026-01-01T00:00:09+00:00") is None

    assert store.claimDueBootOperation(now="2026-01-01T00:00:10+00:00").attempt_count == 2
    succeeded = store.succeedBootOperation(
        operation.operation_id,
        result={"deleted": True},
        now="2026-01-01T00:00:11+00:00",
    )
    assert succeeded is not None
    assert succeeded.state == BOOT_OPERATION_SUCCEEDED
    assert succeeded.result == {"deleted": True}
    assert succeeded.last_error == ""
    assert succeeded.due_at == ""
    assert store.claimDueBootOperation(now="2026-01-01T00:00:11+00:00") is None

    next_operation = store.ensureBootOperation(
        kind=BOOT_OPERATION_ACCOUNT_DELETE,
        subject="account:A1",
        requester="A1",
        route="/account/delete",
        payload={"account_aid": "A1"},
        due_at="2026-01-01T00:00:12+00:00",
        now="2026-01-01T00:00:12+00:00",
    )
    assert next_operation.operation_id != operation.operation_id

    failed = store.failBootOperation(
        next_operation.operation_id,
        last_error="permanent failure",
        result={"status": 409},
        now="2026-01-01T00:00:13+00:00",
    )
    assert failed is not None
    assert failed.state == BOOT_OPERATION_FAILED
    assert failed.last_error == "permanent failure"
    assert failed.result == {"status": 409}
    assert failed.due_at == ""
    assert store.getBootOperation(failed.operation_id).state == BOOT_OPERATION_FAILED


def test_boot_operation_payload_and_result_must_be_dicts(store):
    with pytest.raises(ValueError):
        store.ensureBootOperation(
            kind=BOOT_OPERATION_SESSION_PROVISION,
            subject="sess_1",
            requester="E1",
            route="/onboarding/session/start",
            payload=["not", "a", "dict"],
        )

    operation = store.ensureBootOperation(
        kind=BOOT_OPERATION_SESSION_PROVISION,
        subject="sess_2",
        requester="E2",
        route="/onboarding/session/start",
    )
    with pytest.raises(ValueError):
        store.succeedBootOperation(operation.operation_id, result=["not", "a", "dict"])


def test_failed_session_provision_operation_marks_session_failed(store):
    session = store.createSession(
        ephemeral_aid="E1",
        account_aid="A1",
        account_alias="alias",
        chosen_profile_code="1-of-1",
        client_ip="127.0.0.1",
        region_id="default",
        region_name="Default",
        watcher_required=True,
        witness_count=1,
        toad=1,
        account_tier="free",
    )
    operation = store.ensureBootOperation(
        kind=BOOT_OPERATION_SESSION_PROVISION,
        subject=session.session_id,
        requester=session.ephemeral_aid,
        route="/onboarding/session/start",
        payload={"session_id": session.session_id},
        due_at="2026-01-01T00:00:00+00:00",
        now="2026-01-01T00:00:00+00:00",
    )

    store.failBootOperation(
        operation.operation_id,
        last_error="witness limit is 0",
        result={"status_code": 409},
        now="2026-01-01T00:00:01+00:00",
    )

    saved = store.getSession(session.session_id)
    assert saved.state == SESSION_STATE_FAILED
    assert saved.failure_reason == "witness limit is 0"
    assert saved.updated_at == "2026-01-01T00:00:01+00:00"
    assert store.getCleanupTask(CLEANUP_TASK_SESSION_CLEANUP, session.session_id) is not None


def test_operations_status_allows_operation_requester_on_onboarding_surface(contract):
    with habbing.openHab(name="operation-requester", temp=True, transferable=False) as (_, requester):
        register_aid(contract, "/onboarding", requester)
        operation = contract.ctx.store.ensureBootOperation(
            kind=BOOT_OPERATION_SESSION_PROVISION,
            subject="sess_requested",
            requester=requester.pre,
            route="/onboarding/session/start",
            payload={"session_id": "sess_requested"},
            due_at="2026-01-01T00:00:00+00:00",
            now="2026-01-01T00:00:00+00:00",
        )

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                requester,
                route="/operations/status",
                payload={"operation_id": operation.operation_id},
            ),
        )

    _, reply = assert_reply_frame(contract, response, route="/operations/status")
    body = reply.ked["a"]
    assert body["operation"]["operation_id"] == operation.operation_id
    assert body["operation"]["kind"] == BOOT_OPERATION_SESSION_PROVISION
    assert body["operation"]["subject"] == "sess_requested"
    assert body["operation"]["state"] == "pending"
    assert body["operation"]["payload"] == {"session_id": "sess_requested"}


def test_operations_status_refreshes_session_provision_lease_for_requester(contract):
    with habbing.openHab(name="operation-lease-refresh", temp=True, transferable=False) as (_, requester):
        register_aid(contract, "/onboarding", requester)
        _, _, start_reply = start_session(contract, requester, drain_operations=False)
        session_id = start_reply.ked["a"]["session_id"]
        operation = contract.ctx.store.findActiveBootOperation(
            kind=BOOT_OPERATION_SESSION_PROVISION,
            subject=session_id,
            requester=requester.pre,
        )
        assert operation is not None
        session = contract.ctx.store.getSession(session_id)
        original_expires_at = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
        session.expires_at = original_expires_at
        contract.ctx.store.saveSession(session)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                requester,
                route="/operations/status",
                payload={"operation_id": operation.operation_id},
            ),
        )

    _, reply = assert_reply_frame(contract, response, route="/operations/status")
    refreshed = contract.ctx.store.getSession(session_id)
    assert reply.ked["a"]["operation"]["operation_id"] == operation.operation_id
    assert datetime.fromisoformat(refreshed.expires_at) > datetime.fromisoformat(original_expires_at)


def test_operations_status_rejects_wrong_sender(contract):
    with (
        habbing.openHab(name="operation-owner", temp=True, transferable=False) as (_, owner),
        habbing.openHab(name="operation-other", temp=True, transferable=False) as (_, other),
    ):
        register_aid(contract, "/onboarding", owner)
        register_aid(contract, "/onboarding", other)
        operation = contract.ctx.store.ensureBootOperation(
            kind=BOOT_OPERATION_SESSION_PROVISION,
            subject="sess_private",
            requester=owner.pre,
            route="/onboarding/session/start",
            payload={"session_id": "sess_private"},
            due_at="2026-01-01T00:00:00+00:00",
            now="2026-01-01T00:00:00+00:00",
        )

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                other,
                route="/operations/status",
                payload={"operation_id": operation.operation_id},
            ),
        )

    assert response.status_code == 401
    assert response.json["title"] == "Wrong operation principal"


def test_session_status_includes_linked_session_provision_operation(contract):
    with habbing.openHab(name="operation-session-owner", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)
        _, _, start_reply = start_session(contract, ephemeral, drain_operations=False)
        session_id = start_reply.ked["a"]["session_id"]
        operation = contract.ctx.store.findActiveBootOperation(
            kind=BOOT_OPERATION_SESSION_PROVISION,
            subject=session_id,
            requester=ephemeral.pre,
        )
        assert operation is not None

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/session/status",
                payload={"session_id": session_id},
            ),
        )

    _, reply = assert_reply_frame(contract, response, route="/onboarding/session/status")
    assert reply.ked["a"]["session_provision_operation"]["operation_id"] == operation.operation_id
    assert reply.ked["a"]["session_provision_operation"]["state"] == "pending"


def test_operations_status_allows_account_subject_owner_on_account_surface(onboarded_bundle):
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    operation = contract.ctx.store.ensureBootOperation(
        kind=BOOT_OPERATION_ACCOUNT_DELETE,
        subject=f"account:{account.pre}",
        requester="system",
        route="/account/delete",
        payload={"account_aid": account.pre},
        due_at="2026-01-01T00:00:00+00:00",
        now="2026-01-01T00:00:00+00:00",
    )

    response = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/operations/status",
            payload={"operation_id": operation.operation_id},
        ),
    )

    _, reply = assert_reply_frame(contract, response, route="/operations/status")
    assert reply.ked["a"]["operation"]["operation_id"] == operation.operation_id
    assert reply.ked["a"]["operation"]["subject"] == f"account:{account.pre}"


def test_operations_status_returns_404_for_missing_operation(contract):
    with habbing.openHab(name="operation-missing", temp=True, transferable=False) as (_, requester):
        register_aid(contract, "/onboarding", requester)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                requester,
                route="/operations/status",
                payload={"operation_id": "op_missing"},
            ),
        )

    assert response.status_code == 404
    assert response.json["title"] == "Operation not found"


def probe_do(events: list[str], tymth, tock=0.0, **kwa):
    while True:
        events.append("probe")
        yield tock


class YieldingProcessor:
    def __init__(self, events: list[str] | None = None):
        self.events = events if events is not None else []
        self.operations: list[str] = []

    def processBootOperationDo(self, *, operation, witness_boots, watcher_boot, tymth, tock=0.0):
        self.operations.append(operation.operation_id)
        self.events.append(f"start:{operation.operation_id}")
        yield tock
        self.events.append(f"finish:{operation.operation_id}")
        return {"operation_id": operation.operation_id, "processed": True}


class FailingProcessor:
    def __init__(self, status_code: int = 503):
        self.operations: list[str] = []
        self.status_code = status_code

    def processBootOperationDo(self, *, operation, witness_boots, watcher_boot, tymth, tock=0.0):
        self.operations.append(operation.operation_id)
        yield tock
        raise BootError("downstream unavailable", status_code=self.status_code)


class BrokenProcessor:
    def processBootOperationDo(self, *, operation, witness_boots, watcher_boot, tymth, tock=0.0):
        yield tock
        raise RuntimeError("bug")


def make_operation(store: Store, *, subject: str, kind: str = BOOT_OPERATION_SESSION_PROVISION):
    return store.ensureBootOperation(
        kind=kind,
        subject=subject,
        requester="AID1",
        route="/operations/test",
        payload={"subject": subject},
        due_at="2026-01-01T00:00:00+00:00",
        now="2026-01-01T00:00:00+00:00",
    )


def test_boot_operation_doer_process_due_is_bounded_by_batch_size(tmp_path):
    store = Store(str(tmp_path / "bounded-operation-store"))
    try:
        first = make_operation(store, subject="sess_1")
        second = make_operation(store, subject="sess_2")
        processor = YieldingProcessor()
        doer = BootOperationDoer(store=store, processor=processor, batch_size=1)

        processed = drain_do(
            doer.processDueDo(
                tymth=lambda: 0.0,
                tock=0.0,
            )
        )

        processed_ids = set(processor.operations)

        assert processed == 1
        assert len(processed_ids) == 1
        assert processed_ids <= {first.operation_id, second.operation_id}
        assert sum(
            1
            for operation_id in {first.operation_id, second.operation_id}
            if store.getBootOperation(operation_id).state == BOOT_OPERATION_SUCCEEDED
        ) == 1
        assert sum(
            1
            for operation_id in {first.operation_id, second.operation_id}
            if store.getBootOperation(operation_id).state == BOOT_OPERATION_PENDING
        ) == 1
    finally:
        store.close()


def test_boot_operation_doer_recovers_claimed_operations_on_startup(tmp_path):
    store = Store(str(tmp_path / "recovered-operation-store"))
    try:
        operation = make_operation(store, subject="sess_1")
        claimed = store.claimDueBootOperation(now="2026-01-01T00:00:01+00:00")
        assert claimed.operation_id == operation.operation_id

        processor = YieldingProcessor()
        doer = BootOperationDoer(store=store, processor=processor, interval=0.01)

        doing.Doist(tock=0.05, limit=0.3, real=False).do(doers=[doer])

        updated = store.getBootOperation(operation.operation_id)
        assert doer.recovered_claimed_operations == 1
        assert updated.state == BOOT_OPERATION_SUCCEEDED
        assert updated.claimed_at == ""
        assert processor.operations == [operation.operation_id]
    finally:
        store.close()


def test_boot_operation_doer_yields_to_root_runtime_while_processing(tmp_path):
    store = Store(str(tmp_path / "yielding-operation-store"))
    try:
        operation = make_operation(store, subject="watcher:WA1", kind=BOOT_OPERATION_WATCHER_STATUS_QUERY)
        events: list[str] = []
        processor = YieldingProcessor(events)
        doer = BootOperationDoer(store=store, processor=processor, interval=0.01)
        probe = doing.doify(probe_do, events=events, tock=0.0)

        doing.Doist(tock=0.05, limit=0.3, real=False).do(doers=[doer, probe])

        start = events.index(f"start:{operation.operation_id}")
        finish = events.index(f"finish:{operation.operation_id}")
        assert "probe" in events[start + 1 : finish]
        assert store.getBootOperation(operation.operation_id).state == BOOT_OPERATION_SUCCEEDED
    finally:
        store.close()


def test_boot_operation_doer_reschedules_processor_failure(tmp_path):
    store = Store(str(tmp_path / "failing-operation-store"))
    try:
        operation = make_operation(store, subject="sess_1")
        processor = FailingProcessor()
        doer = BootOperationDoer(
            store=store,
            processor=processor,
            batch_size=1,
            failure_backoff_seconds=30,
            failure_backoff_max_seconds=30,
        )

        processed = drain_do(
            doer.processDueDo(
                tymth=lambda: 0.0,
                tock=0.0,
            )
        )

        updated = store.getBootOperation(operation.operation_id)
        assert processed == 1
        assert updated.state == BOOT_OPERATION_PENDING
        assert updated.attempt_count == 1
        assert updated.last_error == "downstream unavailable"
        assert updated.due_at > updated.last_attempt_at
    finally:
        store.close()


def test_boot_operation_doer_fails_non_retryable_boot_errors(tmp_path):
    store = Store(str(tmp_path / "non-retryable-operation-store"))
    try:
        operation = make_operation(store, subject="sess_1")
        processor = FailingProcessor(status_code=400)
        doer = BootOperationDoer(store=store, processor=processor, batch_size=1)

        processed = drain_do(
            doer.processDueDo(
                tymth=lambda: 0.0,
                tock=0.0,
            )
        )

        updated = store.getBootOperation(operation.operation_id)
        assert processed == 1
        assert updated.state == BOOT_OPERATION_FAILED
        assert updated.attempt_count == 1
        assert updated.last_error == "downstream unavailable"
        assert updated.result == {"status_code": 400}
        assert updated.due_at == ""
    finally:
        store.close()


def test_boot_operation_doer_fails_retryable_boot_errors_after_attempt_limit(tmp_path):
    store = Store(str(tmp_path / "retry-limit-operation-store"))
    try:
        operation = make_operation(store, subject="sess_1")
        processor = FailingProcessor(status_code=503)
        doer = BootOperationDoer(
            store=store,
            processor=processor,
            batch_size=1,
            failure_max_attempts=1,
        )

        processed = drain_do(
            doer.processDueDo(
                tymth=lambda: 0.0,
                tock=0.0,
            )
        )

        updated = store.getBootOperation(operation.operation_id)
        assert processed == 1
        assert updated.state == BOOT_OPERATION_FAILED
        assert updated.attempt_count == 1
        assert updated.last_error == "downstream unavailable"
        assert updated.result == {"status_code": 503}
        assert updated.due_at == ""
    finally:
        store.close()


def test_boot_operation_doer_fails_unexpected_processor_errors(tmp_path):
    store = Store(str(tmp_path / "broken-operation-store"))
    try:
        operation = make_operation(store, subject="sess_1")
        doer = BootOperationDoer(store=store, processor=BrokenProcessor(), batch_size=1)

        processed = drain_do(
            doer.processDueDo(
                tymth=lambda: 0.0,
                tock=0.0,
            )
        )

        updated = store.getBootOperation(operation.operation_id)
        assert processed == 1
        assert updated.state == BOOT_OPERATION_FAILED
        assert updated.last_error == "bug"
        assert updated.due_at == ""
    finally:
        store.close()


def test_runtime_build_doers_configures_operation_hio_boot_clients(tmp_path):
    config = make_config(
        tmp_path,
        cleanup_interval_seconds=0,
        cleanup_block_after_attempts=2,
        operation_failure_max_attempts=3,
    )
    app, ctx = create_app(config=config, temp=True)
    try:
        doers = build_doers(app, ctx)

        operation_doer = next(doer for doer in doers if isinstance(doer, BootOperationDoer))

        assert operation_doer.clienter is not None
        assert operation_doer.witness_boots
        assert all(isinstance(client, HioBootClient) for client in operation_doer.witness_boots.values())
        assert all(client.clienter is operation_doer.clienter for client in operation_doer.witness_boots.values())
        assert isinstance(operation_doer.watcher_boot, HioBootClient)
        assert operation_doer.watcher_boot.clienter is operation_doer.clienter
        assert operation_doer.failure_max_attempts == 3
    finally:
        ctx.close(clear=True)
