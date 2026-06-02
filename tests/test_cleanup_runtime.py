from __future__ import annotations

from types import SimpleNamespace

import pytest

from hio.base import doing

from kfboot.app import create_app
from kfboot.basing import (
    ACCOUNT_STATE_EXPIRED,
    CLEANUP_TASK_ACCOUNT_CLEANUP,
    CLEANUP_TASK_ACCOUNT_DELETE,
    AccountRecord,
    ResourceRecord,
)
from kfboot.boot_client import BootError, HioBootClient
from kfboot.expiring import Expirer
from kfboot.provisioning import Provisioner
from kfboot.runtime import build_doers
from kfboot.store import Store
from kfboot.sweeping import CleanupDoer, CleanupState

from .support import make_config


def probe_do(events: list[str], tymth, tock=0.0, **kwa):
    while True:
        events.append("probe")
        yield tock


def drain(gen, *, max_steps: int = 20):
    for _ in range(max_steps):
        try:
            next(gen)
        except StopIteration as ex:
            return ex.value
    raise AssertionError("generator did not finish")


class YieldingExpirer:
    def __init__(self, events: list[str]):
        self.events = events
        self.recover_calls: list[str] = []

    def recoverClaimedCleanupTasks(self, *, now: str) -> int:
        self.recover_calls.append(now)
        return 1

    def sweepDo(self, *, batch_size, time_budget_seconds, tymth, tock=0.0):
        self.events.append("sweep-start")
        yield tock
        self.events.append("sweep-middle")
        yield tock
        self.events.append("sweep-end")
        return {
            "sessions_expired": 0,
            "sessions_cleaned": 0,
            "sessions_deleted": 0,
            "accounts_expired": 0,
            "accounts_cleaned": 1,
            "accounts_deleted": 0,
        }


class FailingProvisioner:
    def __init__(self):
        self.attempts: list[str] = []

    def teardownAccountResourcesDo(self, *, account_aid: str, account=None, tymth, tock: float = 0.0):
        self.attempts.append(account_aid)
        yield tock
        raise BootError("simulated teardown failure", status_code=502)


class FailingAccountDeleteProvisioner:
    def __init__(self):
        self.attempts: list[str] = []

    def deleteAccountDo(self, *, account_aid: str, account=None, tymth, tock: float = 0.0):
        self.attempts.append(account_aid)
        yield tock
        raise BootError("simulated account delete failure", status_code=502)


class SuccessfulAccountCleanupProvisioner:
    def __init__(self):
        self.attempts: list[str] = []

    def teardownAccountResourcesDo(self, *, account_aid: str, account=None, tymth, tock: float = 0.0):
        self.attempts.append(account_aid)
        yield tock


def cleanup_ctx(store):
    return SimpleNamespace(
        store=store,
        config=SimpleNamespace(
            cleanup_batch_size=1,
            cleanup_time_budget_seconds=5,
            cleanup_failure_backoff_seconds=60,
            cleanup_failure_backoff_max_seconds=900,
            cleanup_failure_jitter_seconds=0,
            cleanup_block_after_attempts=10,
            cleanup_block_after_failure_age_seconds=86400.0,
        ),
    )


class ResourceStore:
    def __init__(self):
        self.resource = ResourceRecord(kind="witness", eid="W1", backend_id="wit-1")
        self.deleted: list[tuple[str, str]] = []
        self.saved_accounts: list[AccountRecord] = []

    def getResource(self, kind: str, eid: str):
        if self.resource is not None and self.resource.kind == kind and self.resource.eid == eid:
            return self.resource
        return None

    def deleteResource(self, kind: str, eid: str) -> None:
        self.deleted.append((kind, eid))
        if self.resource is not None and self.resource.kind == kind and self.resource.eid == eid:
            self.resource = None

    def saveAccount(self, account) -> None:
        self.saved_accounts.append(account)


class YieldingWitnessDeleteClient:
    def __init__(self):
        self.calls: list[str] = []

    def deleteWitnessDo(self, eid: str, *, tymth, tock: float = 0.0):
        self.calls.append(eid)
        yield tock


class FailingWitnessDeleteClient:
    def __init__(self, *, status_code: int):
        self.status_code = status_code
        self.calls: list[str] = []

    def deleteWitnessDo(self, eid: str, *, tymth, tock: float = 0.0):
        self.calls.append(eid)
        yield tock
        raise BootError("remote delete failed", status_code=self.status_code)


class SyncWitnessDeleteClient:
    def __init__(self):
        self.calls: list[str] = []

    def deleteWitness(self, eid: str):
        self.calls.append(eid)


def test_cleanup_doer_yields_back_to_root_runtime_between_sweep_steps():
    events: list[str] = []
    expirer = YieldingExpirer(events)
    state = CleanupState(enabled=True, interval=0.01)
    cleanup = CleanupDoer(
        expirer=expirer,
        interval=0.01,
        batch_size=2,
        time_budget_seconds=1.0,
        state=state,
    )
    probe = doing.doify(probe_do, events=events, tock=0.0)

    doing.Doist(tock=0.05, limit=0.8, real=False).do(doers=[cleanup, probe])

    assert expirer.recover_calls
    assert "sweep-start" in events
    assert "sweep-middle" in events
    assert "sweep-end" in events
    assert "probe" in events[events.index("sweep-start") + 1 : events.index("sweep-middle")]
    assert state.is_running is False
    assert state.snapshot()["last_recovery_at"] is not None
    assert state.snapshot()["last_sweep_finished_at"] is not None
    assert state.snapshot()["last_progress_at"] is not None


def test_cleanup_doer_active_tock_is_separate_from_cleanup_interval():
    events: list[str] = []
    cleanup = CleanupDoer(
        expirer=YieldingExpirer(events),
        interval=60.0,
        batch_size=2,
        time_budget_seconds=1.0,
        state=CleanupState(enabled=True, interval=60.0),
    )

    assert cleanup.tock == 0.05


def test_runtime_build_doers_configures_cleanup_hio_boot_clients(tmp_path):
    config = make_config(tmp_path, cleanup_interval_seconds=60)
    app, ctx = create_app(config=config, temp=True)
    try:
        doers = build_doers(app, ctx)

        cleanup = next(doer for doer in doers if isinstance(doer, CleanupDoer))
        provisioner = ctx.exchanger.provisioner

        assert cleanup.clienter is not None
        assert provisioner.cleanup_witness_boots
        assert all(
            isinstance(client, HioBootClient)
            for client in provisioner.cleanup_witness_boots.values()
        )
        assert all(
            client.clienter is cleanup.clienter
            for client in provisioner.cleanup_witness_boots.values()
        )
        assert isinstance(provisioner.cleanup_watcher_boot, HioBootClient)
        assert provisioner.cleanup_watcher_boot.clienter is cleanup.clienter
        assert ctx.watcher_boot is not provisioner.cleanup_watcher_boot
    finally:
        ctx.close(clear=True)


def test_sweep_do_retries_failed_account_cleanup_with_backoff(tmp_path):
    now = "2026-01-01T00:00:00+00:00"
    store = Store(str(tmp_path / "cleanup-retry-store"))
    try:
        account = AccountRecord(
            account_aid="AID_ACCOUNT",
            status=ACCOUNT_STATE_EXPIRED,
            expired_at=now,
        )
        store.saveAccount(account)
        provisioner = FailingProvisioner()
        expirer = Expirer(cleanup_ctx(store), provisioner=provisioner)

        results = drain(
            expirer.sweepDo(
                batch_size=1,
                now=now,
                tymth=lambda: 0.0,
                tock=0.0,
            )
        )

        saved = store.getAccount(account.account_aid)
        task = store.getCleanupTask(CLEANUP_TASK_ACCOUNT_CLEANUP, account.account_aid)
        assert results == {
            "sessions_expired": 0,
            "sessions_cleaned": 0,
            "sessions_deleted": 0,
            "accounts_expired": 0,
            "accounts_cleaned": 0,
            "accounts_deleted": 0,
        }
        assert provisioner.attempts == [account.account_aid]
        assert saved is not None
        assert saved.resources_cleaned_at == ""
        assert task is not None
        assert task.last_error == "simulated teardown failure"
        assert task.attempt_count == 1
        assert task.due_at == "2026-01-01T00:01:00+00:00"
    finally:
        store.close()


def test_sweep_do_retries_failed_account_delete_with_backoff(tmp_path):
    now = "2026-01-01T00:00:00+00:00"
    store = Store(str(tmp_path / "delete-retry-store"), expired_account_retention_seconds=0)
    try:
        account = AccountRecord(
            account_aid="AID_ACCOUNT",
            status=ACCOUNT_STATE_EXPIRED,
            expired_at=now,
            resources_cleaned_at=now,
        )
        store.saveAccount(account)
        provisioner = FailingAccountDeleteProvisioner()
        expirer = Expirer(cleanup_ctx(store), provisioner=provisioner)

        results = drain(
            expirer.sweepDo(
                batch_size=1,
                now=now,
                tymth=lambda: 0.0,
                tock=0.0,
            )
        )

        saved = store.getAccount(account.account_aid)
        task = store.getCleanupTask(CLEANUP_TASK_ACCOUNT_DELETE, account.account_aid)
        assert results == {
            "sessions_expired": 0,
            "sessions_cleaned": 0,
            "sessions_deleted": 0,
            "accounts_expired": 0,
            "accounts_cleaned": 0,
            "accounts_deleted": 0,
        }
        assert provisioner.attempts == [account.account_aid]
        assert saved is not None
        assert task is not None
        assert task.last_error == "simulated account delete failure"
        assert task.attempt_count == 1
        assert task.due_at == "2026-01-01T00:01:00+00:00"
    finally:
        store.close()


def test_sweep_do_processes_real_store_account_cleanup_task(tmp_path):
    now = "2026-01-01T00:00:00+00:00"
    store = Store(str(tmp_path / "cleanup-real-store"), expired_account_retention_seconds=0)
    try:
        account = AccountRecord(
            account_aid="AID_REAL_ACCOUNT",
            status=ACCOUNT_STATE_EXPIRED,
            expired_at=now,
        )
        store.saveAccount(account)
        assert store.getCleanupTask(CLEANUP_TASK_ACCOUNT_CLEANUP, account.account_aid) is not None

        provisioner = SuccessfulAccountCleanupProvisioner()
        expirer = Expirer(cleanup_ctx(store), provisioner=provisioner)

        results = drain(
            expirer.sweepDo(
                batch_size=1,
                now=now,
                tymth=lambda: 0.0,
                tock=0.0,
            )
        )

        saved = store.getAccount(account.account_aid)
        assert results["accounts_cleaned"] == 1
        assert provisioner.attempts == [account.account_aid]
        assert saved is not None
        assert saved.resources_cleaned_at == now
        assert store.getCleanupTask(CLEANUP_TASK_ACCOUNT_CLEANUP, account.account_aid) is None
        assert store.getCleanupTask(CLEANUP_TASK_ACCOUNT_DELETE, account.account_aid) is not None
    finally:
        store.close()


def test_delete_hosted_resource_do_uses_cleanup_client_and_removes_local_bindings():
    store = ResourceStore()
    account = AccountRecord(account_aid="AID_ACCOUNT", witness_eids=["W1"])
    client = YieldingWitnessDeleteClient()
    ctx = SimpleNamespace(
        store=store,
        config=SimpleNamespace(witness_backends=()),
    )
    provisioner = Provisioner(ctx, exchanger=None)
    provisioner.configureCleanupBootClients(
        witness_boots={"wit-1": client},
        watcher_boot=None,
    )

    result = drain(
        provisioner.deleteHostedResourceDo(
            kind="witness",
            eid="W1",
            account=account,
            tymth=lambda: 0.0,
            tock=0.0,
        )
    )

    assert result is None
    assert client.calls == ["W1"]
    assert store.deleted == [("witness", "W1")]
    assert store.getResource("witness", "W1") is None
    assert account.witness_eids == []
    assert store.saved_accounts == [account]


def test_delete_hosted_resource_do_keeps_local_bindings_on_remote_failure():
    store = ResourceStore()
    account = AccountRecord(account_aid="AID_ACCOUNT", witness_eids=["W1"])
    client = FailingWitnessDeleteClient(status_code=503)
    ctx = SimpleNamespace(
        store=store,
        config=SimpleNamespace(witness_backends=()),
    )
    provisioner = Provisioner(ctx, exchanger=None)
    provisioner.configureCleanupBootClients(
        witness_boots={"wit-1": client},
        watcher_boot=None,
    )

    with pytest.raises(BootError) as excinfo:
        drain(
            provisioner.deleteHostedResourceDo(
                kind="witness",
                eid="W1",
                account=account,
                tymth=lambda: 0.0,
                tock=0.0,
            )
        )

    assert str(excinfo.value) == "remote delete failed"
    assert client.calls == ["W1"]
    assert store.deleted == []
    assert store.getResource("witness", "W1") is not None
    assert account.witness_eids == ["W1"]
    assert store.saved_accounts == []


def test_delete_hosted_resource_do_tolerates_remote_404_and_removes_local_bindings():
    store = ResourceStore()
    account = AccountRecord(account_aid="AID_ACCOUNT", witness_eids=["W1"])
    client = FailingWitnessDeleteClient(status_code=404)
    ctx = SimpleNamespace(
        store=store,
        config=SimpleNamespace(witness_backends=()),
    )
    provisioner = Provisioner(ctx, exchanger=None)
    provisioner.configureCleanupBootClients(
        witness_boots={"wit-1": client},
        watcher_boot=None,
    )

    result = drain(
        provisioner.deleteHostedResourceDo(
            kind="witness",
            eid="W1",
            account=account,
            tolerate_missing_remote=True,
            tymth=lambda: 0.0,
            tock=0.0,
        )
    )

    assert result is None
    assert client.calls == ["W1"]
    assert store.deleted == [("witness", "W1")]
    assert store.getResource("witness", "W1") is None
    assert account.witness_eids == []
    assert store.saved_accounts == [account]


def test_sync_delete_hosted_resource_still_uses_sync_boot_client():
    store = ResourceStore()
    account = AccountRecord(account_aid="AID_ACCOUNT", witness_eids=["W1"])
    sync_client = SyncWitnessDeleteClient()
    cleanup_client = YieldingWitnessDeleteClient()
    ctx = SimpleNamespace(
        store=store,
        witness_boots={"wit-1": sync_client},
        watcher_boot=None,
        config=SimpleNamespace(witness_backends=()),
    )
    provisioner = Provisioner(ctx, exchanger=None)
    provisioner.configureCleanupBootClients(
        witness_boots={"wit-1": cleanup_client},
        watcher_boot=None,
    )

    provisioner.deleteHostedResource(
        kind="witness",
        eid="W1",
        account=account,
    )

    assert sync_client.calls == ["W1"]
    assert cleanup_client.calls == []
    assert store.deleted == [("witness", "W1")]
