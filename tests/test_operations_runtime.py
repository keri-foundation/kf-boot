from __future__ import annotations

from hio.base import doing

from kfboot.app import create_app
from kfboot.basing import (
    BOOT_OPERATION_PENDING,
    BOOT_OPERATION_FAILED,
    BOOT_OPERATION_SESSION_PROVISION,
    BOOT_OPERATION_SUCCEEDED,
    BOOT_OPERATION_WATCHER_STATUS_QUERY,
)
from kfboot.boot_client import BootError, HioBootClient
from kfboot.operating import BootOperationDoer
from kfboot.runtime import build_doers
from kfboot.store import Store

from .support import drain_do, make_config


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
    config = make_config(tmp_path, cleanup_interval_seconds=0)
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
    finally:
        ctx.close(clear=True)
