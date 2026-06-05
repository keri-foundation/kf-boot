from __future__ import annotations

import pytest

from kfboot.basing import (
    BOOT_OPERATION_ACCOUNT_DELETE,
    BOOT_OPERATION_FAILED,
    BOOT_OPERATION_PENDING,
    BOOT_OPERATION_RUNNING,
    BOOT_OPERATION_SESSION_PROVISION,
    BOOT_OPERATION_SUCCEEDED,
    BOOT_OPERATION_WATCHER_STATUS_QUERY,
)
from kfboot.store import Store


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
