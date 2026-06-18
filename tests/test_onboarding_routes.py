from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from types import SimpleNamespace
import falcon
import pytest
from keri.app import habbing

from kfboot.basing import (
    ACCOUNT_STATE_FAILED,
    ACCOUNT_STATE_ONBOARDED,
    ACCOUNT_STATE_EXPIRED,
    ACCOUNT_STATE_PENDING_ONBOARDING,
    BOOT_OPERATION_FAILED,
    BOOT_OPERATION_PENDING,
    BOOT_OPERATION_SESSION_PROVISION,
    BOOT_OPERATION_SUCCEEDED,
    CLEANUP_TASK_SESSION_CLEANUP,
    SESSION_STATE_ACCOUNT_CREATED,
    SESSION_STATE_CANCELLED,
    SESSION_STATE_COMPLETED,
    SESSION_STATE_EXPIRED,
    SESSION_STATE_FAILED,
    SESSION_STATE_STARTED,
    SESSION_STATE_WITNESS_POOL_ALLOCATED,
)
from kfboot.boot_client import BootError
from kfboot.config import AccountProfile
from kfboot.limiting import Limiter
from kfboot.operating import BootOperationDoer, BootOperationProcessor
from .support import (
    FakeWatcherBoot,
    account_create_payload,
    assert_reply_frame,
    boot_error,
    build_exn,
    complete_session,
    create_account,
    drain_do,
    freeze_boot_time,
    make_witness_backends,
    post_cesr,
    register_aid,
    run_boot_operations,
    start_payload,
    start_session,
    sweep_do,
    total_witness_create_calls,
    total_witness_delete_calls,
    make_config,
)
from kfboot.store import Store


def test_onboarding_flow_persists_state_transitions_and_bound_resources(contract):
    with (
        habbing.openHab(name="flow-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name="flow-account", temp=True) as (_, account),
    ):
        register_aid(contract, "/onboarding", ephemeral)
        _, _, start_reply = start_session(contract, ephemeral, account_aid=account.pre)

        session_id = start_reply.ked["a"]["session_id"]
        witness_id = start_reply.ked["a"]["witnesses"][0]["eid"]
        watcher_id = start_reply.ked["a"]["watcher"]["eid"]
        assert "boot_url" in start_reply.ked["a"]["witnesses"][0]
        assert "boot_url" in start_reply.ked["a"]["watcher"]

        session = contract.ctx.store.getSession(session_id)
        assert session.state == SESSION_STATE_WITNESS_POOL_ALLOCATED
        assert session.witness_eids == [witness_id]
        assert session.watcher_eid == watcher_id
        assert contract.ctx.store.getResource("witness", witness_id).principal == ""
        assert contract.ctx.store.getResource("witness", witness_id).cid == ""
        assert contract.ctx.store.getResource("watcher", watcher_id).principal == ""
        assert contract.ctx.store.getResource("watcher", watcher_id).cid == ""

        status_response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/session/status",
                payload={"session_id": session_id},
            ),
        )
        _, status_reply = assert_reply_frame(contract, status_response, route="/onboarding/session/status")
        assert status_reply.ked["a"]["state"] == SESSION_STATE_WITNESS_POOL_ALLOCATED
        assert status_reply.ked["a"]["session_provision_operation"]["state"] == BOOT_OPERATION_SUCCEEDED
        assert "boot_url" in status_reply.ked["a"]["witnesses"][0]
        assert "boot_url" in status_reply.ked["a"]["watcher"]

        _, _, create_reply = create_account(contract, ephemeral, start_reply, account_aid=account.pre)
        assert create_reply.ked["a"]["account"]["account_aid"] == account.pre

        session = contract.ctx.store.getSession(session_id)
        account_record = contract.ctx.store.getAccount(account.pre)
        assert session.state == SESSION_STATE_ACCOUNT_CREATED
        assert session.account_aid == account.pre
        assert account_record.status == ACCOUNT_STATE_PENDING_ONBOARDING
        assert contract.ctx.store.getResource("witness", witness_id).principal == account.pre
        assert contract.ctx.store.getResource("witness", witness_id).cid == account.pre
        assert contract.ctx.store.getResource("watcher", watcher_id).principal == account.pre
        assert contract.ctx.store.getResource("watcher", watcher_id).cid == account.pre

        _, _, complete_reply = complete_session(
            contract,
            ephemeral,
            session_id=session_id,
            account_aid=account.pre,
        )
        assert complete_reply.ked["a"]["state"] == SESSION_STATE_COMPLETED

        session = contract.ctx.store.getSession(session_id)
        account_record = contract.ctx.store.getAccount(account.pre)
        assert session.state == SESSION_STATE_COMPLETED
        assert account_record.status == ACCOUNT_STATE_ONBOARDED
        assert account_record.onboarded_at


def test_session_start_returns_pending_operation_before_downstream_allocation(contract):
    with (
        habbing.openHab(name="async-start-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name="async-start-account", temp=True) as (_, account),
    ):
        register_aid(contract, "/onboarding", ephemeral)
        _, _, start_reply = start_session(
            contract,
            ephemeral,
            account_aid=account.pre,
            drain_operations=False,
        )
        session_id = start_reply.ked["a"]["session_id"]
        operation = start_reply.ked["a"]["session_provision_operation"]

        assert start_reply.ked["a"]["witnesses"] == []
        assert start_reply.ked["a"]["watcher"] is None
        assert operation["kind"] == BOOT_OPERATION_SESSION_PROVISION
        assert operation["state"] == BOOT_OPERATION_PENDING

        blocked = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/account/create",
                payload={"session_id": session_id, "account_aid": account.pre},
            ),
        )
        assert blocked.status_code == 409
        assert blocked.json["title"] == "Session provisioning pending"

        run_boot_operations(contract)
        _, _, create_reply = create_account(contract, ephemeral, start_reply, account_aid=account.pre)

        updated = contract.ctx.store.getBootOperation(operation["operation_id"])
        assert updated.state == BOOT_OPERATION_SUCCEEDED
        assert create_reply.ked["a"]["account"]["account_aid"] == account.pre


def test_in_flight_provisioning_does_not_resurrect_cancelled_session(contract):
    with habbing.openHab(name="cancel-during-provisioning", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)
        _, _, start_reply = start_session(contract, ephemeral, drain_operations=False)
        session_id = start_reply.ked["a"]["session_id"]
        operation = contract.ctx.store.listBootOperations(
            kind=BOOT_OPERATION_SESSION_PROVISION,
            subject=session_id,
        )[0]
        doer = BootOperationDoer(
            store=contract.ctx.store,
            witness_boots=contract.ctx.witness_boots,
            watcher_boot=contract.ctx.watcher_boot,
            processor=BootOperationProcessor(provisioner=contract.ctx.exchanger.provisioner),
            batch_size=100,
        )
        provisioning = doer.processDueDo(tymth=lambda: 0.0, tock=0.0)
        next(provisioning)

        cancel = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/cancel", payload={"session_id": session_id}),
        )
        assert cancel.status_code == 200
        assert contract.ctx.store.getCleanupTask(CLEANUP_TASK_SESSION_CLEANUP, session_id) is not None

        drain_do(provisioning)

        cancelled = contract.ctx.store.getSession(session_id)
        updated_operation = contract.ctx.store.getBootOperation(operation.operation_id)
        assert cancelled.state == SESSION_STATE_CANCELLED
        assert cancelled.resources_cleaned_at == ""
        assert updated_operation.state == BOOT_OPERATION_FAILED
        assert updated_operation.last_error == "The onboarding session was cancelled."
        assert updated_operation.result == {"status_code": 409}
        assert contract.ctx.store.getCleanupTask(CLEANUP_TASK_SESSION_CLEANUP, session_id) is not None
        assert contract.ctx.store.countResources("witness") == 1
        assert contract.ctx.store.countResources("watcher") == 0
        assert contract.ctx.watcher_boot.create_calls == 0


def test_session_status_rejects_expired_session_without_refreshing_lease(contract):
    """Test that requests are rejected if session is expired"""
    with (
        habbing.openHab(name="expired-status-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name="expired-status-account", temp=True) as (_, account),
    ):
        register_aid(contract, "/onboarding", ephemeral)
        _, _, start_reply = start_session(contract, ephemeral, account_aid=account.pre)

        # Retrieve session and expire it 
        session_id = start_reply.ked["a"]["session_id"]
        session = contract.ctx.store.getSession(session_id)
        session.expires_at = "2000-01-01T00:00:00+00:00"

        # Save session to trigger clean up tasks
        contract.ctx.store.saveSession(session)

        # Send an onboarding status request
        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/session/status",
                payload={"session_id": session_id},
            ),
        )

        # Assert session is expired and request is rejected
        updated = contract.ctx.store.getSession(session_id)
        assert response.status_code == 410
        assert response.json["title"] == "Session expired"
        assert updated is not None
        assert updated.state == SESSION_STATE_EXPIRED
        assert updated.expires_at == "2000-01-01T00:00:00+00:00"


def test_session_start_is_idempotent_and_does_not_duplicate_allocations(contract):
    with habbing.openHab(name="start-idempotent", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)

        _, _, first = start_session(contract, ephemeral, chosen_profile_code="3-of-4")
        _, _, second = start_session(contract, ephemeral, chosen_profile_code="3-of-4")
        session = contract.ctx.store.getSession(first.ked["a"]["session_id"])
        records = [
            contract.ctx.store.getResource("witness", row["eid"])
            for row in first.ked["a"]["witnesses"]
        ]
        backend_ids = [record.backend_id for record in records]

        assert first.ked["a"]["session_id"] == second.ked["a"]["session_id"]
        assert first.ked["a"]["witnesses"] == second.ked["a"]["witnesses"]
        assert first.ked["a"]["watcher"] == second.ked["a"]["watcher"]
        assert len(set(backend_ids)) == 4
        assert session.witness_backend_ids == backend_ids
        assert total_witness_create_calls(contract.ctx) == 4
        assert contract.ctx.watcher_boot.create_calls == 1
        assert contract.ctx.store.countResources("witness") == 4
        assert contract.ctx.store.countResources("watcher") == 1
        for backend_id in session.witness_backend_ids:
            assert contract.ctx.witness_boots[backend_id].create_cids == ["AID_ACCOUNT"]
        for record in records:
            backend = next(
                backend
                for backend in contract.ctx.config.witness_backends
                if backend.id == record.backend_id
            )
            assert record.url == backend.public_url
            assert record.boot_url == backend.boot_url
            assert record.cid == ""
            assert record.principal == ""

        watcher_record = contract.ctx.store.getResource("watcher", first.ked["a"]["watcher"]["eid"])
        assert watcher_record.cid == ""
        assert watcher_record.principal == ""
        assert contract.ctx.watcher_boot.create_cids == ["AID_ACCOUNT"]


def test_session_start_rejects_fresh_ephemeral_retry_for_active_account(contract):
    with (
        habbing.openHab(name="start-active-account-owner", temp=True, transferable=False) as (_, owner),
        habbing.openHab(name="start-active-account-other", temp=True, transferable=False) as (_, other),
    ):
        register_aid(contract, "/onboarding", owner)
        register_aid(contract, "/onboarding", other)
        _, _, first = start_session(contract, owner)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(other, route="/onboarding/session/start", payload=start_payload()),
        )

    assert response.status_code == 409
    assert response.json["title"] == "Account session already active"
    assert total_witness_create_calls(contract.ctx) == 1
    assert contract.ctx.watcher_boot.create_calls == 1
    session = contract.ctx.store.getSession(first.ked["a"]["session_id"])
    assert session.ephemeral_aid == owner.pre


def test_session_start_rejects_already_onboarded_account(contract):
    with (
        habbing.openHab(name="start-onboarded-owner", temp=True, transferable=False) as (_, owner),
        habbing.openHab(name="start-onboarded-account", temp=True) as (_, account),
        habbing.openHab(name="start-onboarded-retry", temp=True, transferable=False) as (_, retry),
    ):
        register_aid(contract, "/onboarding", owner)
        register_aid(contract, "/onboarding", retry)
        _, _, start_reply = start_session(contract, owner, account_aid=account.pre)
        create_account(contract, owner, start_reply, account_aid=account.pre)
        complete_session(contract, owner, session_id=start_reply.ked["a"]["session_id"], account_aid=account.pre)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(retry, route="/onboarding/session/start", payload=start_payload(account_aid=account.pre)),
        )

    assert response.status_code == 409
    assert response.json["title"] == "Account already onboarded"
    assert total_witness_create_calls(contract.ctx) == 1
    assert contract.ctx.watcher_boot.create_calls == 1


@pytest.mark.parametrize("state", [SESSION_STATE_CANCELLED, SESSION_STATE_EXPIRED])
def test_session_start_rejects_closed_existing_session_without_new_allocations(contract, state):
    with habbing.openHab(name=f"start-closed-{state}", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)
        _, _, start_reply = start_session(contract, ephemeral)
        session_id = start_reply.ked["a"]["session_id"]
        session = contract.ctx.store.getSession(session_id)
        session.state = state
        contract.ctx.store.saveSession(session)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/session/start", payload=start_payload()),
        )

    assert response.status_code == 409
    assert response.json["title"] == "Session closed"
    assert contract.ctx.store.getSession(session_id).state == state
    assert total_witness_create_calls(contract.ctx) == 1
    assert contract.ctx.watcher_boot.create_calls == 1


def test_session_start_marks_past_due_session_expired_and_blocks_until_cleanup_finishes(contract):
    """Expired sessions must finish cleanup before a fresh allocation can be created."""
    with habbing.openHab(name="start-past-due-session", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)
        _, _, first = start_session(contract, ephemeral)
        first_session_id = first.ked["a"]["session_id"]

        # Force the existing session past its lease so the next start request
        # must close it instead of reusing or refreshing it.
        stale = contract.ctx.store.getSession(first_session_id)
        stale.expires_at = "2000-01-01T00:00:00+00:00"
        contract.ctx.store.saveSession(stale)

        # The retry marks the stale session expired, but it should not hand out
        # a brand-new session until cleanup has reclaimed the old resources.
        retry = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/session/start", payload=start_payload()),
        )
        refreshed_old = contract.ctx.store.getSession(first_session_id)

        assert retry.status_code == 409
        assert retry.json["title"] == "Session cleanup pending"
        assert refreshed_old is not None
        assert refreshed_old.state == SESSION_STATE_EXPIRED
        assert refreshed_old.resources_cleaned_at == ""
        assert total_witness_create_calls(contract.ctx) == 1
        assert contract.ctx.watcher_boot.create_calls == 1

        # Once cleanup runs, a follow-up start may allocate a fresh session safely.
        cleaned = sweep_do(
            contract.ctx.exchanger.expirer,
            now="2099-01-01T00:00:00+00:00",
            batch_size=1,
        )
        assert cleaned["sessions_cleaned"] == 1

        second_retry = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/session/start", payload=start_payload()),
        )
        _, retry_reply = assert_reply_frame(contract, second_retry, route="/onboarding/session/start")
        assert second_retry.status_code == 200
        assert retry_reply.ked["a"]["session_id"] != first_session_id
        run_boot_operations(contract)
        assert total_witness_create_calls(contract.ctx) == 2
        assert contract.ctx.watcher_boot.create_calls == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_aid", "different-account"),
        ("account_alias", "different-alias"),
        ("chosen_profile_code", "3-of-4"),
        ("region_id", "different-region"),
    ],
)
def test_session_start_retry_parameter_mismatch_conflicts_without_new_allocations(contract, field, value):
    with habbing.openHab(name=f"start-mismatch-{field}", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)
        _, _, first = start_session(contract, ephemeral)
        session_id = first.ked["a"]["session_id"]
        original_session = contract.ctx.store.getSession(session_id)

        retry_payload = start_payload()
        retry_payload[field] = value
        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/session/start", payload=retry_payload),
        )

        assert response.status_code == 409
        assert response.json["title"] == "Session parameter mismatch"
        assert total_witness_create_calls(contract.ctx) == 1
        assert contract.ctx.watcher_boot.create_calls == 1
        assert contract.ctx.store.getSession(session_id).account_alias == original_session.account_alias


def test_session_start_retry_rejects_watcher_requirement_mismatch_when_optional(contract_factory):
    contract = contract_factory(bootstrap_watcher_required=False)

    with habbing.openHab(name="start-mismatch-watcher-required", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)
        start_session(contract, ephemeral, watcher_required=True)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/session/start",
                payload=start_payload(watcher_required=False),
            ),
        )

    assert response.status_code == 409
    assert response.json["title"] == "Session parameter mismatch"
    assert total_witness_create_calls(contract.ctx) == 1
    assert contract.ctx.watcher_boot.create_calls == 1


@pytest.mark.parametrize(
    ("payload", "expected_title"),
    [
        ({"chosen_profile_code": "2-of-3"}, "Unsupported witness profile"),
        ({"watcher_required": False}, "Watcher required"),
    ],
)
def test_session_start_rejects_invalid_profile_and_missing_required_watcher(contract, payload, expected_title):
    with habbing.openHab(name=f"invalid-start-{expected_title}", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)
        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/session/start", payload=start_payload(**payload)),
        )

        assert response.status_code == 400
        assert response.json["title"] == expected_title
        assert contract.ctx.store.findActiveSessionForEphemeral(ephemeral.pre) is None


def test_session_start_rejects_profile_not_supported_by_configured_witness_pool(contract_factory):
    contract = contract_factory(witness_backends=make_witness_backends(1))

    with habbing.openHab(name="invalid-start-configured-pool", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)
        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/session/start",
                payload=start_payload(chosen_profile_code="3-of-4"),
            ),
        )

    assert response.status_code == 400
    assert response.json["title"] == "Unsupported witness profile"
    assert contract.ctx.store.findActiveSessionForEphemeral(ephemeral.pre) is None


def test_session_status_requires_existing_session_and_onboarding_principal(contract):
    with (
        habbing.openHab(name="status-owner", temp=True, transferable=False) as (_, owner),
        habbing.openHab(name="status-other", temp=True, transferable=False) as (_, other),
    ):
        register_aid(contract, "/onboarding", owner)
        register_aid(contract, "/onboarding", other)
        _, _, start_reply = start_session(contract, owner)
        session_id = start_reply.ked["a"]["session_id"]

        missing = post_cesr(
            contract,
            "/onboarding",
            build_exn(owner, route="/onboarding/session/status", payload={"session_id": "sess_missing"}),
        )
        assert missing.status_code == 404
        assert missing.json["title"] == "Session not found"

        wrong_principal = post_cesr(
            contract,
            "/onboarding",
            build_exn(other, route="/onboarding/session/status", payload={"session_id": session_id}),
        )
        assert wrong_principal.status_code == 401
        assert wrong_principal.json["title"] == "Wrong principal"


def test_session_status_refreshes_session_lease(contract):
    with habbing.openHab(name="status-refresh", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)
        _, _, start_reply = start_session(contract, ephemeral)
        session_id = start_reply.ked["a"]["session_id"]
        session = contract.ctx.store.getSession(session_id)
        session.expires_at = "2099-01-01T00:00:00+00:00"
        contract.ctx.store.saveSession(session)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/session/status",
                payload={"session_id": session_id},
            ),
        )
        assert response.status_code == 200

        refreshed = contract.ctx.store.getSession(session_id)
        assert refreshed.expires_at != "2099-01-01T00:00:00+00:00"


def test_partial_downstream_failure_stays_visible_on_operation(contract_factory):
    contract = contract_factory(
        watcher_boot=FakeWatcherBoot(create_error=boot_error(503, "simulated watcher failure"))
    )

    with habbing.openHab(name="partial-failure", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/session/start",
                payload=start_payload(chosen_profile_code="3-of-4"),
            ),
        )
        assert response.status_code == 200
        _, start_reply = assert_reply_frame(contract, response, route="/onboarding/session/start")
        session_id = start_reply.ked["a"]["session_id"]

        run_boot_operations(contract)
        session = contract.ctx.store.findSessionForEphemeral(ephemeral.pre)
        operation = contract.ctx.store.listBootOperations(
            kind=BOOT_OPERATION_SESSION_PROVISION,
            subject=session_id,
        )[0]
        assert operation.state == BOOT_OPERATION_PENDING
        assert operation.last_error == "simulated watcher failure"
        assert session.state == SESSION_STATE_WITNESS_POOL_ALLOCATED
        assert len(session.witness_eids) == 4
        assert session.watcher_eid == ""
        assert total_witness_create_calls(contract.ctx) == 4
        assert contract.ctx.watcher_boot.create_calls == 1
        assert contract.ctx.store.countResources("witness") == 4
        assert contract.ctx.store.countResources("watcher") == 0

        account_create = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/account/create",
                payload={"session_id": session_id, "account_aid": session.account_aid},
            ),
        )
        assert account_create.status_code == 409
        assert account_create.json["title"] == "Session provisioning pending"


def test_failed_session_teardown_is_retried_by_cleanup_tasks(contract_factory, monkeypatch):
    """Test that teardown failure can be picked up again for cleanup"""
    contract = contract_factory(
        bootstrap_accounts_per_ip=100,
        bootstrap_aids_per_ip=100,
    )

    with (
        habbing.openHab(name="failed-cleanup-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name="failed-cleanup-account", temp=True) as (_, account),
    ):
        register_aid(contract, "/onboarding", ephemeral)
        _, _, start_reply = start_session(contract, ephemeral, account_aid=account.pre)
        create_account(contract, ephemeral, start_reply, account_aid=account.pre)

        session_id = start_reply.ked["a"]["session_id"]
        witness_id = start_reply.ked["a"]["witnesses"][0]["eid"]
        watcher_id = start_reply.ked["a"]["watcher"]["eid"]
        session = contract.ctx.store.getSession(session_id)
        account_record = contract.ctx.store.getAccount(account.pre)
        attempts: list[str] = []

        def flaky_teardown_do(*, session: Any, account=None, tymth, tock: float = 0.0):
            attempts.append(session.session_id)
            yield tock
            if len(attempts) == 1:
                raise BootError("simulated cleanup failure", status_code=502)
            contract.ctx.store.deleteResource("witness", witness_id)
            contract.ctx.store.deleteResource("watcher", watcher_id)
            session.witness_eids = []
            session.watcher_eid = ""
            if account is not None:
                account.witness_eids = []
                account.watcher_eid = ""
                account.session_id = ""

        monkeypatch.setattr(contract.ctx.exchanger.provisioner, "teardownSessionResourcesDo", flaky_teardown_do)

        # Fail session clean up
        contract.ctx.exchanger.expirer.failSession(
            session=session,
            reason="simulated provisioning failure",
            account=account_record,
            teardown=True,
        )
        
        # Assert failed session
        failed = contract.ctx.store.getSession(session_id)
        assert failed is not None
        assert failed.state == SESSION_STATE_FAILED
        assert failed.resources_cleaned_at == ""

        # The first teardown attempt now happens in the cleanup worker.
        failed_cleanup = sweep_do(
            contract.ctx.exchanger.expirer,
            now="2099-01-01T00:00:00+00:00",
            batch_size=1,
        )
        task = contract.ctx.store.getCleanupTask("session_cleanup", session_id)
        assert failed_cleanup["sessions_cleaned"] == 0
        assert task.due_at == "2099-01-01T00:01:00+00:00"

        cleaned = sweep_do(
            contract.ctx.exchanger.expirer,
            now="2099-01-01T00:01:00+00:00",
            batch_size=1,
        )

        # Assert sucessful teardown
        recovered = contract.ctx.store.getSession(session_id)
        failed_account = contract.ctx.store.getAccount(account.pre)
        assert cleaned["sessions_cleaned"] == 1
        assert recovered is not None
        assert recovered.resources_cleaned_at == "2099-01-01T00:01:00+00:00"
        assert failed_account is not None
        assert failed_account.status == ACCOUNT_STATE_FAILED
        assert failed_account.resources_cleaned_at == "2099-01-01T00:01:00+00:00"
        assert failed_account.session_id == ""
        assert contract.ctx.store.getResource("witness", witness_id) is None
        assert contract.ctx.store.getResource("watcher", watcher_id) is None
        assert attempts == [session_id, session_id]


def test_cleanup_expired_sessions_failure_does_not_report_false_success(contract_factory, monkeypatch):
    """Cleanup retries should stay visible as pending work instead of looking successful."""
    contract = contract_factory(
        bootstrap_accounts_per_ip=100,
        bootstrap_aids_per_ip=100,
    )

    with habbing.openHab(name="cleanup-failure-session", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)
        _, _, start_reply = start_session(contract, ephemeral)

        session_id = start_reply.ked["a"]["session_id"]
        session = contract.ctx.store.getSession(session_id)
        session.expires_at = "2000-01-01T00:00:00+00:00"
        contract.ctx.store.saveSession(session)
        sweep_do(
            contract.ctx.exchanger.expirer,
            now="2026-01-01T00:00:00+00:00",
            batch_size=1,
        )

        def always_fail_do(*, session: Any, account=None, tymth, tock: float = 0.0):
            yield tock
            raise BootError("simulated cleanup retry failure", status_code=502)

        monkeypatch.setattr(contract.ctx.exchanger.provisioner, "teardownSessionResourcesDo", always_fail_do)

        cleaned = sweep_do(
            contract.ctx.exchanger.expirer,
            now="2026-01-01T00:00:00+00:00"
        )

        updated = contract.ctx.store.getSession(session_id)
        task = contract.ctx.store.getCleanupTask("session_cleanup", session_id)
        assert cleaned["sessions_cleaned"] == 0
        assert updated is not None
        assert updated.resources_cleaned_at == ""
        assert task is not None
        assert task.last_error == "simulated cleanup retry failure"
        assert task.attempt_count == 1
        assert task.due_at == "2026-01-01T00:01:00+00:00"


def test_cleanup_expired_sessions_blocks_non_retryable_failures(contract_factory, monkeypatch):
    """Permanent cleanup failures should be quarantined instead of retried forever."""
    contract = contract_factory(
        bootstrap_accounts_per_ip=100,
        bootstrap_aids_per_ip=100,
    )

    with habbing.openHab(name="cleanup-blocked-session", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)
        _, _, start_reply = start_session(contract, ephemeral)

        session_id = start_reply.ked["a"]["session_id"]
        session = contract.ctx.store.getSession(session_id)
        session.expires_at = "2000-01-01T00:00:00+00:00"
        contract.ctx.store.saveSession(session)
        sweep_do(
            contract.ctx.exchanger.expirer,
            now="2026-01-01T00:00:00+00:00",
            batch_size=1,
        )

        # Simulate a malformed request with error code 400
        def non_retryable_do(*, session: Any, account=None, tymth, tock: float = 0.0):
            yield tock
            raise BootError("simulated malformed cleanup request", status_code=400)

        monkeypatch.setattr(
            contract.ctx.exchanger.provisioner,
            "teardownSessionResourcesDo",
            non_retryable_do,
        )

        # Run the cleanup
        cleaned = sweep_do(
            contract.ctx.exchanger.expirer,
            now="2026-01-01T00:00:00+00:00",
            batch_size=1,
        )

        # Get the cleanup task and backlog
        task = contract.ctx.store.getCleanupTask("session_cleanup", session_id)
        snapshot = contract.ctx.store.cleanupBacklogSnapshot(now="2026-01-01T00:00:10+00:00")

        # Assert cleanning failed and task is blocked without retry attempts
        assert cleaned["sessions_cleaned"] == 0
        assert task is not None

        # Assert blocked task is cleared from due metadata and has blocked metadata
        assert task.due_at == ""
        assert task.claimed_at == ""
        assert task.blocked_at == "2026-01-01T00:00:00+00:00"
        assert "Non-retryable cleanup failure" in task.blocked_reason
        assert task.last_error == "simulated malformed cleanup request"
        assert snapshot["blocked_tasks"] == 1


def test_expired_sessions_reclaim_hosted_resources_and_fail_pending_account(contract):
    with (
        habbing.openHab(name="expiry-owner-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name="expiry-owner-account", temp=True) as (_, account),
        habbing.openHab(name="expiry-next-ephemeral", temp=True, transferable=False) as (_, next_ephemeral),
    ):
        register_aid(contract, "/onboarding", ephemeral)
        _, _, start_reply = start_session(contract, ephemeral, account_aid=account.pre)
        create_account(contract, ephemeral, start_reply, account_aid=account.pre)

        session_id = start_reply.ked["a"]["session_id"]
        witness_id = start_reply.ked["a"]["witnesses"][0]["eid"]
        watcher_id = start_reply.ked["a"]["watcher"]["eid"]

        expired = contract.ctx.store.getSession(session_id)
        expired.expires_at = "2024-01-01T00:00:00+00:00"
        contract.ctx.store.saveSession(expired)

        sweep = sweep_do(
            contract.ctx.exchanger.expirer,
            batch_size=10,
        )

        register_aid(contract, "/onboarding", next_ephemeral)
        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(next_ephemeral, route="/onboarding/session/start", payload=start_payload(account_aid="AID_NEXT")),
        )

        assert response.status_code == 200

        expired = contract.ctx.store.getSession(session_id)
        account_record = contract.ctx.store.getAccount(account.pre)
        assert sweep["sessions_expired"] == 1
        assert sweep["sessions_cleaned"] == 1
        assert sweep["accounts_deleted"] == 1
        assert expired is None
        assert account_record is None
        assert contract.ctx.store.getResource("witness", witness_id) is None
        assert contract.ctx.store.getResource("watcher", watcher_id) is None
        assert witness_id in total_witness_delete_calls(contract.ctx)
        assert watcher_id in contract.ctx.watcher_boot.delete_calls


def test_session_start_enforces_witness_capacity(contract_factory):
    contract = contract_factory(witness_limit=0)

    with habbing.openHab(name="witness-capacity", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)
        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/session/start", payload=start_payload()),
        )

    assert response.status_code == 200
    run_boot_operations(contract)
    session = contract.ctx.store.findSessionForEphemeral(ephemeral.pre)
    operation = contract.ctx.store.listBootOperations(
        kind=BOOT_OPERATION_SESSION_PROVISION,
        subject=session.session_id,
    )[0]
    assert operation.state == BOOT_OPERATION_FAILED
    assert "witness limit is 0" in operation.last_error
    assert session.state == SESSION_STATE_FAILED
    assert "witness limit is 0" in session.failure_reason
    assert session.witness_eids == []
    assert session.watcher_eid == ""
    assert contract.ctx.store.getCleanupTask(CLEANUP_TASK_SESSION_CLEANUP, session.session_id) is not None
    assert contract.ctx.store.countResources("witness") == 0


def test_session_start_enforces_watcher_capacity_and_blocks_blind_retry(contract_factory):
    contract = contract_factory(watcher_limit=0)

    with habbing.openHab(name="watcher-capacity", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)
        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/session/start", payload=start_payload()),
        )
        assert response.status_code == 200

        run_boot_operations(contract)
        session = contract.ctx.store.findSessionForEphemeral(ephemeral.pre)
        operation = contract.ctx.store.listBootOperations(
            kind=BOOT_OPERATION_SESSION_PROVISION,
            subject=session.session_id,
        )[0]
        assert operation.state == BOOT_OPERATION_FAILED
        assert "watcher limit is 0" in operation.last_error
        assert session.state == SESSION_STATE_FAILED
        assert "watcher limit is 0" in session.failure_reason
        assert len(session.witness_eids) == 1
        assert session.watcher_eid == ""
        assert contract.ctx.store.getCleanupTask(CLEANUP_TASK_SESSION_CLEANUP, session.session_id) is not None
        assert contract.ctx.store.countResources("witness") == 1
        assert contract.ctx.store.countResources("watcher") == 0
        assert len(total_witness_delete_calls(contract.ctx)) == 0

        retry = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/session/start", payload=start_payload()),
        )
        assert retry.status_code == 409
        assert retry.json["title"] == "Session failed"
        run_boot_operations(contract)
        assert total_witness_create_calls(contract.ctx) == 1
        assert contract.ctx.watcher_boot.create_calls == 0
        assert contract.ctx.store.countResources("witness") == 1
        assert contract.ctx.store.countResources("watcher") == 0


def test_session_start_enforces_per_ip_account_limit(contract_factory):
    contract = contract_factory(bootstrap_accounts_per_ip=1, bootstrap_aids_per_ip=10)

    with (
        habbing.openHab(name="ip-account-owner", temp=True, transferable=False) as (_, first),
        habbing.openHab(name="ip-account-other", temp=True, transferable=False) as (_, second),
    ):
        register_aid(contract, "/onboarding", first)
        register_aid(contract, "/onboarding", second)
        first_response = post_cesr(
            contract,
            "/onboarding",
            build_exn(first, route="/onboarding/session/start", payload=start_payload()),
            remote_addr="127.0.0.1",
        )
        assert first_response.status_code == 200

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(second, route="/onboarding/session/start", payload=start_payload(account_aid="AID_OTHER")),
            remote_addr="127.0.0.1",
        )

    assert response.status_code == 429
    assert response.json["title"] == "Per-IP onboarding account limit exceeded"


def test_session_start_counts_uncleaned_closed_sessions_toward_per_ip_limits(contract_factory):
    """Closed sessions still count against IP admission until their resources are reclaimed."""
    contract = contract_factory(bootstrap_accounts_per_ip=1, bootstrap_aids_per_ip=10)

    with (
        habbing.openHab(name="ip-cleanup-debt-owner", temp=True, transferable=False) as (_, first),
        habbing.openHab(name="ip-cleanup-debt-other", temp=True, transferable=False) as (_, second),
    ):
        register_aid(contract, "/onboarding", first)
        register_aid(contract, "/onboarding", second)

        # Send an Onboarding session start request
        first_response = post_cesr(
            contract,
            "/onboarding",
            build_exn(first, route="/onboarding/session/start", payload=start_payload(account_aid="AID_FIRST")),
            remote_addr="127.0.0.1",
        )
        assert first_response.status_code == 200
        _, first_reply = assert_reply_frame(contract, first_response, route="/onboarding/session/start")

        # Set session as stale
        stale = contract.ctx.store.getSession(first_reply.ked["a"]["session_id"])
        stale.expires_at = "2000-01-01T00:00:00+00:00"
        contract.ctx.store.saveSession(stale)
        contract.ctx.exchanger.expirer.markSessionExpired(stale, now="2000-01-01T00:00:00+00:00")

        # Try to start a 2nd onboarding session
        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(second, route="/onboarding/session/start", payload=start_payload(account_aid="AID_OTHER")),
            remote_addr="127.0.0.1",
        )

    assert response.status_code == 429
    assert response.json["title"] == "Per-IP onboarding account limit exceeded"


def test_session_start_counts_past_due_non_terminal_sessions_toward_per_ip_limits(contract_factory):
    """Past-due sessions should still burn admission capacity until cleanup can run."""
    contract = contract_factory(bootstrap_accounts_per_ip=1, bootstrap_aids_per_ip=10)

    with (
        habbing.openHab(name="ip-past-due-owner", temp=True, transferable=False) as (_, first),
        habbing.openHab(name="ip-past-due-other", temp=True, transferable=False) as (_, second),
    ):
        register_aid(contract, "/onboarding", first)
        register_aid(contract, "/onboarding", second)

        first_response = post_cesr(
            contract,
            "/onboarding",
            build_exn(first, route="/onboarding/session/start", payload=start_payload(account_aid="AID_FIRST")),
            remote_addr="127.0.0.1",
        )
        assert first_response.status_code == 200
        _, first_reply = assert_reply_frame(contract, first_response, route="/onboarding/session/start")

        # Leave the session non-terminal, but make its lease past due. The hosted
        # resources are still live until the sweeper marks and cleans the session.
        stale = contract.ctx.store.getSession(first_reply.ked["a"]["session_id"])
        stale.expires_at = "2000-01-01T00:00:00+00:00"
        contract.ctx.store.saveSession(stale)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(second, route="/onboarding/session/start", payload=start_payload(account_aid="AID_OTHER")),
            remote_addr="127.0.0.1",
        )

    assert response.status_code == 429
    assert response.json["title"] == "Per-IP onboarding account limit exceeded"


def test_session_start_enforces_per_ip_onboarding_principal_limit(contract_factory):
    contract = contract_factory(bootstrap_accounts_per_ip=10, bootstrap_aids_per_ip=1)

    with (
        habbing.openHab(name="ip-principal-owner", temp=True, transferable=False) as (_, first),
        habbing.openHab(name="ip-principal-other", temp=True, transferable=False) as (_, second),
    ):
        register_aid(contract, "/onboarding", first)
        register_aid(contract, "/onboarding", second)
        post_cesr(
            contract,
            "/onboarding",
            build_exn(first, route="/onboarding/session/start", payload=start_payload()),
            remote_addr="127.0.0.1",
        )

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(second, route="/onboarding/session/start", payload=start_payload(account_aid="AID_OTHER")),
            remote_addr="127.0.0.1",
        )

    assert response.status_code == 429
    assert response.json["title"] == "Per-IP onboarding principal limit exceeded"


def test_session_start_does_not_consume_onboarded_account_quota(contract_factory):
    """Test using someone else's AID does not burn their quotas."""
    contract = contract_factory(
        bootstrap_accounts_per_ip=10,
        bootstrap_aids_per_ip=10,
        bootstrap_account_options=("1-of-1",),
        account_profiles=(
            AccountProfile(
                tier="trial",
                code="1-of-1",
                max_accounts=100,
                max_requests_per_minute=1,
                api_budget=100,
            ),
        ),
    )

    with (
        habbing.openHab(name="quota-victim-ephemeral", temp=True, transferable=False) as (_, victim_ephemeral),
        habbing.openHab(name="quota-victim-account", temp=True) as (_, victim_account),
        habbing.openHab(name="quota-attacker-ephemeral", temp=True, transferable=False) as (_, attacker),
    ):
        register_aid(contract, "/onboarding", victim_ephemeral)
        register_aid(contract, "/onboarding", attacker)
        register_aid(contract, "/account", victim_account)

        _, _, start_reply = start_session(contract, victim_ephemeral, account_aid=victim_account.pre)
        create_account(contract, victim_ephemeral, start_reply, account_aid=victim_account.pre)
        complete_session(
            contract,
            victim_ephemeral,
            session_id=start_reply.ked["a"]["session_id"],
            account_aid=victim_account.pre,
        )

        rejected = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                attacker,
                route="/onboarding/session/start",
                payload=start_payload(account_aid=victim_account.pre),
            ),
        )
        assert rejected.status_code == 409
        assert rejected.json["title"] == "Account already onboarded"

        allowed = post_cesr(
            contract,
            "/account",
            build_exn(victim_account, route="/account/witnesses", payload={"account_aid": victim_account.pre}),
        )

    assert allowed.status_code == 200


def test_account_route_request_rate_limit_resets_after_minute(contract_factory, monkeypatch):
    """Test validated account-route throttles clear when the minute window rolls over."""
    clock = freeze_boot_time(monkeypatch, datetime(2026, 1, 1, tzinfo=UTC))
    contract = contract_factory(
        bootstrap_accounts_per_ip=10,
        bootstrap_aids_per_ip=10,
        bootstrap_account_options=("1-of-1",),
        account_profiles=(
            AccountProfile(
                tier="trial",
                code="1-of-1",
                max_accounts=100,
                max_requests_per_minute=2,
                api_budget=100,
            ),
        ),
    )

    with (
        habbing.openHab(name="account-rate-rollover-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name="account-rate-rollover-account", temp=True) as (_, account),
    ):
        register_aid(contract, "/onboarding", ephemeral)
        register_aid(contract, "/account", account)
        _, _, start_reply = start_session(contract, ephemeral, account_aid=account.pre)
        create_account(contract, ephemeral, start_reply, account_aid=account.pre)
        complete_session(
            contract,
            ephemeral,
            session_id=start_reply.ked["a"]["session_id"],
            account_aid=account.pre,
        )

        first = post_cesr(
            contract,
            "/account",
            build_exn(account, route="/account/witnesses", payload={"account_aid": account.pre}),
        )
        assert first.status_code == 200

        second = post_cesr(
            contract,
            "/account",
            build_exn(account, route="/account/witnesses", payload={"account_aid": account.pre}),
        )
        assert second.status_code == 200

        rejected = post_cesr(
            contract,
            "/account",
            build_exn(account, route="/account/witnesses", payload={"account_aid": account.pre}),
        )
        assert rejected.status_code == 429
        assert rejected.json["title"] == "Account request rate limit exceeded"

        clock.value += timedelta(seconds=61)
        accepted = post_cesr(
            contract,
            "/account",
            build_exn(account, route="/account/witnesses", payload={"account_aid": account.pre}),
        )

    assert accepted.status_code == 200


def test_account_route_request_rate_limit_is_scoped_per_account(contract_factory):
    """One account exhausting its validated route quota must not throttle another account."""
    contract = contract_factory(
        bootstrap_accounts_per_ip=10,
        bootstrap_aids_per_ip=10,
        bootstrap_account_options=("1-of-1",),
        account_profiles=(
            AccountProfile(
                tier="trial",
                code="1-of-1",
                max_accounts=100,
                max_requests_per_minute=2,
                api_budget=100,
            ),
        ),
    )

    with (
        habbing.openHab(name="account-rate-a-ephemeral", temp=True, transferable=False) as (_, first_ephemeral),
        habbing.openHab(name="account-rate-a-account", temp=True) as (_, first_account),
        habbing.openHab(name="account-rate-b-ephemeral", temp=True, transferable=False) as (_, second_ephemeral),
        habbing.openHab(name="account-rate-b-account", temp=True) as (_, second_account),
    ):
        register_aid(contract, "/onboarding", first_ephemeral)
        register_aid(contract, "/account", first_account)
        register_aid(contract, "/onboarding", second_ephemeral)
        register_aid(contract, "/account", second_account)

        _, _, first_start = start_session(
            contract,
            first_ephemeral,
            account_aid=first_account.pre,
            account_alias="alpha-a",
        )
        create_account(contract, first_ephemeral, first_start, account_aid=first_account.pre)
        complete_session(
            contract,
            first_ephemeral,
            session_id=first_start.ked["a"]["session_id"],
            account_aid=first_account.pre,
        )

        _, _, second_start = start_session(
            contract,
            second_ephemeral,
            account_aid=second_account.pre,
            account_alias="alpha-b",
        )
        create_account(contract, second_ephemeral, second_start, account_aid=second_account.pre)
        complete_session(
            contract,
            second_ephemeral,
            session_id=second_start.ked["a"]["session_id"],
            account_aid=second_account.pre,
        )

        first = post_cesr(
            contract,
            "/account",
            build_exn(first_account, route="/account/witnesses", payload={"account_aid": first_account.pre}),
        )
        assert first.status_code == 200

        second = post_cesr(
            contract,
            "/account",
            build_exn(first_account, route="/account/witnesses", payload={"account_aid": first_account.pre}),
        )
        assert second.status_code == 200

        rejected = post_cesr(
            contract,
            "/account",
            build_exn(first_account, route="/account/witnesses", payload={"account_aid": first_account.pre}),
        )
        assert rejected.status_code == 429
        assert rejected.json["title"] == "Account request rate limit exceeded"

        accepted = post_cesr(
            contract,
            "/account",
            build_exn(second_account, route="/account/witnesses", payload={"account_aid": second_account.pre}),
        )

    assert accepted.status_code == 200


def test_onboarding_request_quota_blocks_client_ip_for_configured_period(contract_factory, monkeypatch):
    """Verify onboarding request throttles are enforced per client IP before account-specific quotas."""
    clock = freeze_boot_time(monkeypatch, datetime(2026, 1, 1, tzinfo=UTC))
    blocked_ip = "198.51.100.10"
    other_ip = "198.51.100.11"
    contract = contract_factory(
        bootstrap_accounts_per_ip=10,
        bootstrap_aids_per_ip=10,
        bootstrap_api_requests_per_minute=2,
        bootstrap_account_options=("1-of-1",),
        account_profiles=(
            AccountProfile(
                tier="trial",
                code="1-of-1",
                max_accounts=100,
                max_requests_per_minute=100,
                api_budget=100,
            ),
        ),
    )

    with habbing.openHab(name="onboarding-ip-quota", temp=True, transferable=False) as (_, ephemeral):
        register_aid(contract, "/onboarding", ephemeral)

        # Make 2 requests for onboarding
        started = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/session/start", payload=start_payload()),
            remote_addr=blocked_ip,
        )
        _, start_reply = assert_reply_frame(contract, started, route="/onboarding/session/start")
        session_id = start_reply.ked["a"]["session_id"]

        accepted = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/session/status", payload={"session_id": session_id}),
            remote_addr=blocked_ip,
        )
        assert accepted.status_code == 200

        contract.ctx.exchanger.limiter = Limiter(contract.ctx)

        # Third request gets rejected
        rejected = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/session/status", payload={"session_id": session_id}),
            remote_addr=blocked_ip,
        )
        assert rejected.status_code == 429
        assert rejected.json["title"] == "Onboarding request rate limit exceeded"

        # Try with another IP
        scoped_to_ip = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/session/status", payload={"session_id": session_id}),
            remote_addr=other_ip,
        )

        # Assert the other IP passes
        assert scoped_to_ip.status_code == 200

        # Move the clock
        clock.value += timedelta(seconds=59)
        still_blocked = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/session/status", payload={"session_id": session_id}),
            remote_addr=blocked_ip,
        )

        # Still blocked because the minute window has not rolled over yet
        assert still_blocked.status_code == 429

        # Move past the minute window
        clock.value += timedelta(seconds=2)
        unblocked = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/session/status", payload={"session_id": session_id}),
            remote_addr=blocked_ip,
        )
    # Assert it was unblocked
    assert unblocked.status_code == 200


def test_boot_exchanger_normalizes_tuple_client_ip(contract):
    contract.ctx.exchanger.setClientIp(("127.0.0.1", 12345))

    assert contract.ctx.exchanger.client_ip == "127.0.0.1"


def test_onboarding_request_quota_survives_store_reopen(tmp_path):
    config = make_config(
        tmp_path,
        bootstrap_api_requests_per_minute=2,
    )
    store_path = config.db_path

    first_store = Store(store_path, session_ttl_seconds=config.session_ttl_seconds)
    try:
        limiter = Limiter(SimpleNamespace(config=config, store=first_store))
        limiter.enforceOnboardingRequestQuota(
            route="/onboarding/session/start",
            client_ip="198.51.100.20",
        )
        limiter.enforceOnboardingRequestQuota(
            route="/onboarding/session/status",
            client_ip="198.51.100.20",
        )
    finally:
        first_store.close()

    reopened_store = Store(store_path, session_ttl_seconds=config.session_ttl_seconds)
    try:
        limiter = Limiter(SimpleNamespace(config=config, store=reopened_store))
        with pytest.raises(falcon.HTTPTooManyRequests) as excinfo:
            limiter.enforceOnboardingRequestQuota(
                route="/onboarding/account/create",
                client_ip="198.51.100.20",
            )

        assert excinfo.value.title == "Onboarding request rate limit exceeded"
    finally:
        reopened_store.close()

def test_session_start_rejects_account_alias_over_limit(contract_factory):
    """Verify onboarding rejects a new session when the alias already has the max onboarded accounts."""
    contract = contract_factory(
        bootstrap_accounts_per_ip=100,
        bootstrap_aids_per_ip=100,
        bootstrap_account_options=("1-of-1",),
        account_profiles=(
            AccountProfile(
                tier="trial",
                code="1-of-1",
                max_accounts=1,
                max_requests_per_minute=100,
                api_budget=100
            ),
        ),
    )

    with (
        habbing.openHab(name="alias-limit-ephemeral-1", temp=True, transferable=False) as (_, ephemeral1),
        habbing.openHab(name="alias-limit-account-1", temp=True) as (_, account1),
        habbing.openHab(name="alias-limit-ephemeral-2", temp=True, transferable=False) as (_, ephemeral2),
    ):
        register_aid(contract, "/onboarding", ephemeral1)
        register_aid(contract, "/account", account1)

        # Start and complete a session 
        _, _, start_reply = start_session(contract, ephemeral1, account_aid=account1.pre)
        create_account(contract, ephemeral1, start_reply, account_aid=account1.pre)
        _, _, _ = complete_session(
            contract,
            ephemeral1,
            session_id=start_reply.ked["a"]["session_id"],
            account_aid=account1.pre,
        )

        register_aid(contract, "/onboarding", ephemeral2)

        # Attempt to start a session with a different ephemeral but the same alias
        # which should be rejected because the alias already has the max onboarded accounts
        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral2, route="/onboarding/session/start", payload=start_payload(account_aid="AID_SECOND", account_alias="alpha")),
        )

    assert response.status_code == 429
    assert response.json["title"] == "Account alias limit exceeded"
    assert "configured limit for tier 'trial' is 1" in response.json["description"]


def test_session_start_rejects_alias_when_existing_account_is_pending(contract_factory):
    """Verify onboarding rejects a new session when the alias already has an account pending onboarding."""
    contract = contract_factory(
        bootstrap_accounts_per_ip=100,
        bootstrap_aids_per_ip=100,
        bootstrap_account_options=("1-of-1",),
        account_profiles=(
            AccountProfile(
                tier="trial",
                code="1-of-1",
                max_accounts=1,
                max_requests_per_minute=100,
                api_budget=100
            ),
        ),
    )

    with (
        habbing.openHab(name="alias-pending-ephemeral-1", temp=True, transferable=False) as (_, ephemeral1),
        habbing.openHab(name="alias-pending-account-1", temp=True) as (_, account1),
        habbing.openHab(name="alias-pending-ephemeral-2", temp=True, transferable=False) as (_, ephemeral2),
    ):
        # Don't complete the session to leave the account in pending onboarding status
        register_aid(contract, "/onboarding", ephemeral1)
        register_aid(contract, "/account", account1)

        _, _, start_reply = start_session(
            contract,
            ephemeral1,
            account_aid=account1.pre,
            account_alias="alpha",
        )
        
        create_account(contract, ephemeral1, start_reply, account_aid=account1.pre)

        # Register and start a session with a different ephemeral but the same alias
        register_aid(contract, "/onboarding", ephemeral2)
        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral2,
                route="/onboarding/session/start",
                payload=start_payload(account_aid="AID_SECOND", account_alias="alpha"),
            ),
        )
    # Assert the second session is rejected due to the alias already having an account pending onboarding
    assert response.status_code == 429
    assert response.json["title"] == "Account alias limit exceeded"
    assert "configured limit for tier 'trial' is 1" in response.json["description"]


def test_session_start_counts_uncleaned_closed_sessions_toward_alias_limits(contract_factory):
    """Alias limits should include stale sessions whose resources are still being reclaimed."""
    contract = contract_factory(
        bootstrap_accounts_per_ip=100,
        bootstrap_aids_per_ip=100,
        bootstrap_account_options=("1-of-1",),
        account_profiles=(
            AccountProfile(
                tier="trial",
                code="1-of-1",
                max_accounts=1,
                max_requests_per_minute=100,
                api_budget=100,
            ),
        ),
    )

    with (
        habbing.openHab(name="alias-cleanup-debt-1", temp=True, transferable=False) as (_, first),
        habbing.openHab(name="alias-cleanup-debt-2", temp=True, transferable=False) as (_, second),
    ):
        # Register 2 AIDs
        register_aid(contract, "/onboarding", first)
        register_aid(contract, "/onboarding", second)

        # Start a session with the 1st AID
        _, _, first_reply = start_session(
            contract,
            first,
            account_aid="AID_ALIAS_FIRST",
            account_alias="alpha",
        )
        
        # 1st AID's session becomes stale
        stale = contract.ctx.store.getSession(first_reply.ked["a"]["session_id"])
        stale.expires_at = "2000-01-01T00:00:00+00:00"
        contract.ctx.store.saveSession(stale)
        contract.ctx.exchanger.expirer.markSessionExpired(stale, now="2000-01-01T00:00:00+00:00")

        # Send another session start request with the 2nd AID 
        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                second,
                route="/onboarding/session/start",
                payload=start_payload(account_aid="AID_ALIAS_SECOND", account_alias="alpha"),
            ),
        )
    # Assert request was denied because of limit reached
    assert response.status_code == 429
    assert response.json["title"] == "Account alias limit exceeded"


def test_account_create_rejects_expired_permanent_account(contract_factory):
    """Verify that onboarding rejects account creation when the account is expired."""
    contract = contract_factory(
        bootstrap_accounts_per_ip=100,
        bootstrap_aids_per_ip=100,
    )

    with (
        habbing.openHab(name="expired-account-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name="expired-account", temp=True) as (_, account),
    ):
        # Onboard the account and then manually set it to the desired status
        register_aid(contract, "/onboarding", ephemeral)
        register_aid(contract, "/account", account)

        _, _, start_reply = start_session(contract, ephemeral, account_aid=account.pre)
        create_account(contract, ephemeral, start_reply, account_aid=account.pre)

        record = contract.ctx.store.getAccount(account.pre)
        assert record is not None

        # Set the account status to expired
        record.status = ACCOUNT_STATE_EXPIRED
        contract.ctx.store.saveAccount(record)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/account/create",
                payload=account_create_payload(start_reply, account.pre),
            ),
        )
    assert response.status_code == 409
    assert response.json["title"] == "Account not available"
    assert "expired" in response.json["description"]


def test_cleanup_expired_sessions_cleans_up_stale_staging_allocations(contract_factory, monkeypatch):
    """Ensure expired staging sessions are cleaned by the sweeper cleanup phase."""
    contract = contract_factory(
        bootstrap_accounts_per_ip=100,
        bootstrap_aids_per_ip=100,
    )

    session = contract.ctx.store.createSession(
        ephemeral_aid="E-STALE",
        account_aid="AID_STALE",
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
    session.expires_at = "2000-01-01T00:00:00+00:00"
    contract.ctx.store.saveSession(session)

    cleaned: list[tuple] = []

    def fake_teardown_do(*, session: Any, account=None, tymth, tock: float = 0.0):
        yield tock
        cleaned.append((session.session_id, account))

    monkeypatch.setattr(contract.ctx.exchanger.provisioner, "teardownSessionResourcesDo", fake_teardown_do)

    expired = sweep_do(
        contract.ctx.exchanger.expirer,
        now="2026-01-01T00:00:00+00:00",
        batch_size=1,
    )
    cleaned_sessions = sweep_do(
        contract.ctx.exchanger.expirer,
        now="2026-01-01T00:00:00+00:00",
        batch_size=1,
    )

    expired_session = contract.ctx.store.getSession(session.session_id)
    assert expired_session is not None
    assert expired_session.state == SESSION_STATE_EXPIRED
    assert expired["sessions_expired"] == 1
    assert cleaned_sessions["sessions_cleaned"] == 1
    assert expired_session.resources_cleaned_at == "2026-01-01T00:00:00+00:00"
    assert cleaned == [(session.session_id, None)]


def test_account_create_rejects_wrong_onboarding_principal(contract):
    with (
        habbing.openHab(name="create-mismatch-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name="create-mismatch-account", temp=True) as (_, account),
        habbing.openHab(name="create-mismatch-other", temp=True, transferable=False) as (_, other),
    ):
        register_aid(contract, "/onboarding", ephemeral)
        register_aid(contract, "/onboarding", other)
        _, _, start_reply = start_session(contract, ephemeral, account_aid=account.pre)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                other,
                route="/onboarding/account/create",
                payload=account_create_payload(start_reply, account.pre),
            ),
        )

        assert response.status_code == 401
        assert response.json["title"] == "Wrong onboarding principal"
        session = contract.ctx.store.getSession(start_reply.ked["a"]["session_id"])
        assert session.state == SESSION_STATE_WITNESS_POOL_ALLOCATED


def test_account_create_marks_sessionFailed_when_resources_are_incomplete(contract):
    with (
        habbing.openHab(name="resources-incomplete-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name="resources-incomplete-account", temp=True) as (_, account),
    ):
        session = contract.ctx.store.createSession(
            ephemeral_aid=ephemeral.pre,
            account_aid=account.pre,
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
        register_aid(contract, "/onboarding", ephemeral)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/account/create",
                payload={"session_id": session.session_id, "account_aid": account.pre},
            ),
        )

        assert response.status_code == 409
        assert response.json["title"] == "Resources missing"
        saved = contract.ctx.store.getSession(session.session_id)
        assert saved.state == SESSION_STATE_STARTED
        assert saved.failure_reason == ""


def test_account_create_rejects_account_bound_to_other_session(contract):
    with (
        habbing.openHab(name="existing-account-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name="existing-account", temp=True) as (_, account),
    ):
        register_aid(contract, "/onboarding", ephemeral)
        _, _, start_reply = start_session(contract, ephemeral, account_aid=account.pre)

        existing = contract.ctx.store.buildAccount(
            account_aid=account.pre,
            account_alias="existing",
            witness_profile_code="1-of-1",
            witness_count=1,
            toad=1,
            watcher_required=True,
            region_id="test-region",
            region_name="Test Region",
            session_id="sess_other",
            witness_eids=["W1"],
            watcher_eid="WA1",
        )
        contract.ctx.store.saveAccount(existing)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/account/create",
                payload=account_create_payload(start_reply, account.pre),
            ),
        )

        assert response.status_code == 409
        assert response.json["title"] == "Account already exists"


@pytest.mark.parametrize(
    ("state", "expected_status", "expected_title"),
    [
        ("expired", 410, "Session expired"),
        ("failed", 409, "Session failed"),
        (SESSION_STATE_CANCELLED, 409, "Session cancelled"),
        (SESSION_STATE_COMPLETED, 409, "Session completed"),
    ],
)
def test_account_create_rejects_closed_sessions(contract, state, expected_status, expected_title):
    with (
        habbing.openHab(name=f"closed-create-{state}-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name=f"closed-create-{state}-account", temp=True) as (_, account),
    ):
        session = contract.ctx.store.createSession(
            ephemeral_aid=ephemeral.pre,
            account_aid=account.pre,
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
        session.state = state
        if state == "failed":
            session.failure_reason = "downstream failed"
        contract.ctx.store.saveSession(session)
        register_aid(contract, "/onboarding", ephemeral)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/account/create",
                payload={"session_id": session.session_id, "account_aid": account.pre},
            ),
        )

        assert response.status_code == expected_status
        assert response.json["title"] == expected_title


def test_complete_rejects_before_account_exists(contract):
    with (
        habbing.openHab(name="complete-before-account-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name="complete-before-account", temp=True) as (_, account),
    ):
        register_aid(contract, "/onboarding", ephemeral)
        _, _, start_reply = start_session(contract, ephemeral, account_aid=account.pre)
        session_id = start_reply.ked["a"]["session_id"]

        session = contract.ctx.store.getSession(session_id)
        session.account_aid = account.pre
        session.state = SESSION_STATE_ACCOUNT_CREATED
        contract.ctx.store.saveSession(session)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/complete",
                payload={"session_id": session_id, "account_aid": account.pre},
            ),
        )

        assert response.status_code == 404
        assert response.json["title"] == "Account not found"


def test_complete_rejects_missing_watcher_and_wrong_principal(pending_account_bundle):
    contract = pending_account_bundle["contract"]
    session_id = pending_account_bundle["session_id"]
    account = pending_account_bundle["account"]
    ephemeral = pending_account_bundle["ephemeral"]
    with habbing.openHab(name="complete-wrong-other", temp=True, transferable=False) as (_, other):
        register_aid(contract, "/onboarding", other)

        session = contract.ctx.store.getSession(session_id)
        account_record = contract.ctx.store.getAccount(account.pre)
        session.watcher_eid = ""
        account_record.watcher_eid = ""
        contract.ctx.store.saveSession(session)
        contract.ctx.store.saveAccount(account_record)

        wrong_principal = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                other,
                route="/onboarding/complete",
                payload={"session_id": session_id, "account_aid": account.pre},
            ),
        )
        assert wrong_principal.status_code == 401
        assert wrong_principal.json["title"] == "Wrong onboarding principal"

        missing_watcher = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/complete",
                payload={"session_id": session_id, "account_aid": account.pre},
            ),
        )
        assert missing_watcher.status_code == 409
        assert missing_watcher.json["title"] == "Watcher missing"

        session = contract.ctx.store.getSession(session_id)
        account_record = contract.ctx.store.getAccount(account.pre)
        assert session.state == SESSION_STATE_FAILED
        assert len(session.witness_eids) == 1
        assert session.watcher_eid == ""
        assert account_record.status == ACCOUNT_STATE_FAILED
        assert len(account_record.witness_eids) == 1
        assert account_record.watcher_eid == ""
        assert total_witness_delete_calls(contract.ctx) == []
        assert contract.ctx.store.countResources("witness") == 1
        assert contract.ctx.store.countResources("watcher") == 1

        sweep_do(
            contract.ctx.exchanger.expirer,
            now="2099-01-01T00:00:00+00:00",
            batch_size=1,
        )
        assert sorted(total_witness_delete_calls(contract.ctx)) == sorted(pending_account_bundle["witness_ids"])
        assert contract.ctx.store.countResources("witness") == 0
        assert contract.ctx.store.countResources("watcher") == 0


def test_cancel_marks_session_cancelled_is_idempotent_and_fails_pending_account(contract):
    with (
        habbing.openHab(name="cancel-idempotent-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name="cancel-idempotent-account", temp=True) as (_, account),
    ):
        register_aid(contract, "/onboarding", ephemeral)
        _, _, start_reply = start_session(contract, ephemeral, account_aid=account.pre)
        session_id = start_reply.ked["a"]["session_id"]
        create_account(contract, ephemeral, start_reply, account_aid=account.pre)

        first = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/cancel",
                payload={"session_id": session_id},
            ),
        )
        _, first_reply = assert_reply_frame(contract, first, route="/onboarding/cancel")
        assert first_reply.ked["a"]["state"] == SESSION_STATE_CANCELLED

        second = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/cancel",
                payload={"session_id": session_id},
            ),
        )
        _, second_reply = assert_reply_frame(contract, second, route="/onboarding/cancel")
        assert second_reply.ked["a"]["state"] == SESSION_STATE_CANCELLED
        session = contract.ctx.store.getSession(session_id)
        account_record = contract.ctx.store.getAccount(account.pre)
        assert session.state == SESSION_STATE_CANCELLED
        assert session.resources_cleaned_at == ""
        assert len(session.witness_eids) == 1
        assert session.watcher_eid == "WAT_1"
        assert account_record.status == ACCOUNT_STATE_FAILED
        assert account_record.resources_cleaned_at == ""
        assert len(account_record.witness_eids) == 1
        assert account_record.watcher_eid == "WAT_1"
        assert contract.ctx.store.countResources("witness") == 1
        assert contract.ctx.store.countResources("watcher") == 1
        assert total_witness_delete_calls(contract.ctx) == []
        assert contract.ctx.watcher_boot.delete_calls == []

        sweep_do(
            contract.ctx.exchanger.expirer,
            now="2099-01-01T00:00:00+00:00",
            batch_size=1,
        )
        cleaned_session = contract.ctx.store.getSession(session_id)
        cleaned_account = contract.ctx.store.getAccount(account.pre)
        assert cleaned_session.resources_cleaned_at == "2099-01-01T00:00:00+00:00"
        assert cleaned_account.resources_cleaned_at == "2099-01-01T00:00:00+00:00"
        assert contract.ctx.store.countResources("witness") == 0
        assert contract.ctx.store.countResources("watcher") == 0
        assert len(total_witness_delete_calls(contract.ctx)) == 1
        assert contract.ctx.watcher_boot.delete_calls == ["WAT_1"]


def test_cancel_does_not_call_remote_teardown(contract_factory):
    contract = contract_factory()

    with (
        habbing.openHab(name="cancel-fail-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name="cancel-fail-account", temp=True) as (_, account),
    ):
        register_aid(contract, "/onboarding", ephemeral)
        _, _, start_reply = start_session(contract, ephemeral, account_aid=account.pre)
        session_id = start_reply.ked["a"]["session_id"]
        witness_id = start_reply.ked["a"]["witnesses"][0]["eid"]
        witness_record = contract.ctx.store.getResource("witness", witness_id)
        contract.ctx.witness_boots[witness_record.backend_id].delete_error = boot_error(
            503,
            "simulated witness delete failure",
        )
        create_account(contract, ephemeral, start_reply, account_aid=account.pre)

        response = post_cesr(
            contract,
            "/onboarding",
            build_exn(
                ephemeral,
                route="/onboarding/cancel",
                payload={"session_id": session_id},
            ),
        )

    assert response.status_code == 200
    session = contract.ctx.store.getSession(session_id)
    account_record = contract.ctx.store.getAccount(account.pre)
    assert session.state == SESSION_STATE_CANCELLED
    assert session.resources_cleaned_at == ""
    assert len(session.witness_eids) == 1
    assert session.watcher_eid == "WAT_1"
    assert account_record.status == ACCOUNT_STATE_FAILED
    assert account_record.resources_cleaned_at == ""
    assert len(account_record.witness_eids) == 1
    assert account_record.watcher_eid == "WAT_1"
    assert contract.ctx.store.countResources("witness") == 1
    assert contract.ctx.store.countResources("watcher") == 1
    assert contract.ctx.watcher_boot.delete_calls == []
    assert total_witness_delete_calls(contract.ctx) == []


def test_cancel_rejects_wrong_principal_and_completed_session(contract):
    with (
        habbing.openHab(name="cancel-wrong-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name="cancel-wrong-account", temp=True) as (_, account),
        habbing.openHab(name="cancel-wrong-other", temp=True, transferable=False) as (_, other),
    ):
        register_aid(contract, "/onboarding", ephemeral)
        register_aid(contract, "/onboarding", other)
        _, _, start_reply = start_session(contract, ephemeral, account_aid=account.pre)
        session_id = start_reply.ked["a"]["session_id"]

        wrong = post_cesr(
            contract,
            "/onboarding",
            build_exn(other, route="/onboarding/cancel", payload={"session_id": session_id}),
        )
        assert wrong.status_code == 401
        assert wrong.json["title"] == "Wrong onboarding principal"

        create_account(contract, ephemeral, start_reply, account_aid=account.pre)
        complete_session(contract, ephemeral, session_id=session_id, account_aid=account.pre)

        completed = post_cesr(
            contract,
            "/onboarding",
            build_exn(ephemeral, route="/onboarding/cancel", payload={"session_id": session_id}),
        )
        assert completed.status_code == 409
        assert completed.json["title"] == "Session completed"
