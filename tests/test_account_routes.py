from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import falcon
import pytest
from keri.app import habbing

from kfboot.basing import (
    ACCOUNT_STATE_EXPIRED,
    ACCOUNT_STATE_ONBOARDED,
    ACCOUNT_STATE_PENDING_ONBOARDING,
    BOOT_OPERATION_ACCOUNT_DELETE,
    BOOT_OPERATION_FAILED,
    BOOT_OPERATION_PENDING,
    BOOT_OPERATION_RESOURCE_DELETE,
    BOOT_OPERATION_SUCCEEDED,
    BOOT_OPERATION_WATCHER_STATUS_QUERY,
)
from kfboot.boot_client import BootError
from kfboot.config import AccountProfile
from kfboot.store import Store
from kfboot.limiting import Limiter

from .support import (
    assert_reply_frame,
    build_exn,
    freeze_boot_time,
    complete_session,
    create_account,
    make_config,
    post_cesr,
    register_aid,
    run_boot_operations,
    start_session,
    sweep_do,
    total_witness_delete_calls,
)


def test_approved_account_routes_return_resources_update_status_and_delete_records(onboarded_bundle):
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    witness_id = onboarded_bundle["witness_ids"][0]
    watcher_id = onboarded_bundle["watcher_id"]
    witness_record = contract.ctx.store.getResource("witness", witness_id)

    witnesses = post_cesr(
        contract,
        "/account",
        build_exn(account, route="/account/witnesses", payload={"account_aid": account.pre}),
    )
    _, witnesses_reply = assert_reply_frame(contract, witnesses, route="/account/witnesses")
    assert [row["eid"] for row in witnesses_reply.ked["a"]["witnesses"]] == [witness_id]
    assert witnesses_reply.ked["a"]["witnesses"][0]["witness_url"] == witness_record.url
    assert "boot_url" not in witnesses_reply.ked["a"]["witnesses"][0]

    watchers = post_cesr(
        contract,
        "/account",
        build_exn(account, route="/account/watchers", payload={"account_aid": account.pre}),
    )
    _, watchers_reply = assert_reply_frame(contract, watchers, route="/account/watchers")
    assert [row["eid"] for row in watchers_reply.ked["a"]["watchers"]] == [watcher_id]
    assert "boot_url" not in watchers_reply.ked["a"]["watchers"][0]

    watcherStatus = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/watchers/status",
            payload={"account_aid": account.pre, "watcher_eid": watcher_id},
        ),
    )
    _, status_reply = assert_reply_frame(contract, watcherStatus, route="/account/watchers/status")
    operation_id = status_reply.ked["a"]["operation"]["operation_id"]
    assert status_reply.ked["a"]["operation"]["kind"] == BOOT_OPERATION_WATCHER_STATUS_QUERY
    assert status_reply.ked["a"]["operation"]["state"] == BOOT_OPERATION_PENDING
    assert status_reply.ked["a"]["watcher"]["status"] == "created"
    assert contract.ctx.store.getResource("watcher", watcher_id).status == "created"
    assert contract.ctx.watcher_boot.status_calls == []

    run_boot_operations(contract)
    operation = contract.ctx.store.getBootOperation(operation_id)
    assert operation.state == BOOT_OPERATION_SUCCEEDED
    assert operation.result["eid"] == watcher_id
    assert operation.result["summary"]["responsive_witnesses"] == 1
    assert contract.ctx.store.getResource("watcher", watcher_id).status == "connected"
    assert contract.ctx.watcher_boot.status_calls == [watcher_id]

    operation_status = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/operations/status",
            payload={"operation_id": operation_id},
        ),
    )
    _, operation_status_reply = assert_reply_frame(contract, operation_status, route="/operations/status")
    assert operation_status_reply.ked["a"]["operation"]["result"] == operation.result

    witness_delete = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/witnesses/delete",
            payload={"account_aid": account.pre, "witness_eid": witness_id},
        ),
    )
    _, witness_delete_reply = assert_reply_frame(
        contract,
        witness_delete,
        route="/account/witnesses/delete",
    )
    assert witness_delete_reply.ked["a"]["account_aid"] == account.pre
    assert witness_delete_reply.ked["a"]["witness_id"] == witness_id
    assert witness_delete_reply.ked["a"]["deleted"] is False
    assert witness_delete_reply.ked["a"]["operation"]["kind"] == BOOT_OPERATION_RESOURCE_DELETE
    assert witness_delete_reply.ked["a"]["operation"]["state"] == BOOT_OPERATION_PENDING
    assert contract.ctx.store.getResource("witness", witness_id) is not None
    assert contract.ctx.store.getAccount(account.pre).witness_eids == [witness_id]

    run_boot_operations(contract)
    witness_operation = contract.ctx.store.getBootOperation(
        witness_delete_reply.ked["a"]["operation"]["operation_id"]
    )
    assert witness_operation.state == BOOT_OPERATION_SUCCEEDED
    assert witness_operation.result == {"kind": "witness", "eid": witness_id, "deleted": True}
    assert contract.ctx.store.getResource("witness", witness_id) is None
    assert contract.ctx.store.getAccount(account.pre).witness_eids == []

    watcher_delete = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/watchers/delete",
            payload={"account_aid": account.pre, "watcher_eid": watcher_id},
        ),
    )
    _, watcher_delete_reply = assert_reply_frame(contract, watcher_delete, route="/account/watchers/delete")
    assert watcher_delete_reply.ked["a"]["account_aid"] == account.pre
    assert watcher_delete_reply.ked["a"]["watcher_id"] == watcher_id
    assert watcher_delete_reply.ked["a"]["deleted"] is False
    assert watcher_delete_reply.ked["a"]["operation"]["kind"] == BOOT_OPERATION_RESOURCE_DELETE
    assert watcher_delete_reply.ked["a"]["operation"]["state"] == BOOT_OPERATION_PENDING
    assert contract.ctx.store.getResource("watcher", watcher_id) is not None
    assert contract.ctx.store.getAccount(account.pre).watcher_eid == watcher_id

    run_boot_operations(contract)
    watcher_operation = contract.ctx.store.getBootOperation(
        watcher_delete_reply.ked["a"]["operation"]["operation_id"]
    )
    assert watcher_operation.state == BOOT_OPERATION_SUCCEEDED
    assert watcher_operation.result == {"kind": "watcher", "eid": watcher_id, "deleted": True}
    assert contract.ctx.store.getResource("watcher", watcher_id) is None
    assert contract.ctx.store.getAccount(account.pre).watcher_eid == ""
    assert total_witness_delete_calls(contract.ctx) == [witness_id]
    for backend_id, boot in contract.ctx.witness_boots.items():
        expected = [witness_id] if backend_id == witness_record.backend_id else []
        assert boot.delete_calls == expected
    assert contract.ctx.watcher_boot.delete_calls == [watcher_id]


def test_account_delete_route_removes_account_state_and_is_idempotent(onboarded_bundle):
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    session_id = onboarded_bundle["session_id"]
    witness_ids = onboarded_bundle["witness_ids"]
    watcher_id = onboarded_bundle["watcher_id"]
    contract.ctx.store.addBinding(account.pre, "cid-to-delete")

    first = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/delete",
            payload={"account_aid": account.pre},
        ),
    )
    _, first_reply = assert_reply_frame(contract, first, route="/account/delete")
    assert first_reply.ked["a"]["account_aid"] == account.pre
    assert first_reply.ked["a"]["deleted"] is False
    assert first_reply.ked["a"]["operation"]["kind"] == BOOT_OPERATION_ACCOUNT_DELETE
    assert first_reply.ked["a"]["operation"]["state"] == BOOT_OPERATION_PENDING
    assert contract.ctx.store.baser.bindings.get(keys=(account.pre, "cid-to-delete")) is not None
    assert contract.ctx.store.getAccount(account.pre) is not None
    assert contract.ctx.store.getSession(session_id) is not None

    second = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/delete",
            payload={"account_aid": account.pre},
        ),
    )
    _, second_reply = assert_reply_frame(contract, second, route="/account/delete")
    assert second_reply.ked["a"]["account_aid"] == account.pre
    assert second_reply.ked["a"]["deleted"] is False
    assert second_reply.ked["a"]["operation"]["operation_id"] == first_reply.ked["a"]["operation"]["operation_id"]

    run_boot_operations(contract)
    operation = contract.ctx.store.getBootOperation(first_reply.ked["a"]["operation"]["operation_id"])
    assert operation.state == BOOT_OPERATION_SUCCEEDED
    assert operation.result["account_aid"] == account.pre
    assert operation.result["deleted"] is True
    assert contract.ctx.store.baser.bindings.get(keys=(account.pre, "cid-to-delete")) is None
    assert contract.ctx.store.getAccount(account.pre) is None
    assert contract.ctx.store.getSession(session_id) is None
    assert contract.ctx.store.getResource("watcher", watcher_id) is None
    for witness_id in witness_ids:
        assert contract.ctx.store.getResource("witness", witness_id) is None
    assert total_witness_delete_calls(contract.ctx) == witness_ids
    assert contract.ctx.watcher_boot.delete_calls == [watcher_id]

    operation_status = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/operations/status",
            payload={"operation_id": operation.operation_id},
        ),
    )
    _, operation_status_reply = assert_reply_frame(contract, operation_status, route="/operations/status")
    assert operation_status_reply.ked["a"]["operation"]["operation_id"] == operation.operation_id
    assert operation_status_reply.ked["a"]["operation"]["state"] == BOOT_OPERATION_SUCCEEDED


def test_resource_delete_route_reuses_active_operation_before_worker_runs(onboarded_bundle):
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    witness_id = onboarded_bundle["witness_ids"][0]

    first = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/witnesses/delete",
            payload={"account_aid": account.pre, "witness_id": witness_id},
        ),
    )
    second = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/witnesses/delete",
            payload={"account_aid": account.pre, "witness_id": witness_id},
        ),
    )

    _, first_reply = assert_reply_frame(contract, first, route="/account/witnesses/delete")
    _, second_reply = assert_reply_frame(contract, second, route="/account/witnesses/delete")
    assert first_reply.ked["a"]["operation"]["operation_id"] == second_reply.ked["a"]["operation"]["operation_id"]
    assert contract.ctx.store.getResource("witness", witness_id) is not None
    assert total_witness_delete_calls(contract.ctx) == []

    run_boot_operations(contract)
    operation = contract.ctx.store.getBootOperation(first_reply.ked["a"]["operation"]["operation_id"])
    assert operation.state == BOOT_OPERATION_SUCCEEDED
    assert contract.ctx.store.getResource("witness", witness_id) is None
    assert total_witness_delete_calls(contract.ctx) == [witness_id]


def test_watcher_status_reuses_active_delete_operation_before_worker_runs(onboarded_bundle):
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    watcher_id = onboarded_bundle["watcher_id"]

    delete = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/watchers/delete",
            payload={"account_aid": account.pre, "watcher_id": watcher_id},
        ),
    )
    status = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/watchers/status",
            payload={"account_aid": account.pre, "watcher_id": watcher_id},
        ),
    )

    _, delete_reply = assert_reply_frame(contract, delete, route="/account/watchers/delete")
    _, status_reply = assert_reply_frame(contract, status, route="/account/watchers/status")
    assert status_reply.ked["a"]["operation"]["operation_id"] == delete_reply.ked["a"]["operation"]["operation_id"]
    assert status_reply.ked["a"]["operation"]["kind"] == BOOT_OPERATION_RESOURCE_DELETE
    assert contract.ctx.watcher_boot.status_calls == []
    assert (
        contract.ctx.store.findActiveBootOperation(
            kind=BOOT_OPERATION_WATCHER_STATUS_QUERY,
            subject=f"watcher:{watcher_id}",
            requester=account.pre,
        )
        is None
    )


def test_resource_delete_operation_tolerates_remote_404(onboarded_bundle):
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    witness_id = onboarded_bundle["witness_ids"][0]
    witness_record = contract.ctx.store.getResource("witness", witness_id)
    witness_boot = contract.ctx.witness_boots[witness_record.backend_id]
    witness_boot.delete_error = BootError("simulated witness not found", status_code=404)

    response = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/witnesses/delete",
            payload={"account_aid": account.pre, "witness_id": witness_id},
        ),
    )

    _, reply = assert_reply_frame(contract, response, route="/account/witnesses/delete")
    run_boot_operations(contract)
    operation = contract.ctx.store.getBootOperation(reply.ked["a"]["operation"]["operation_id"])
    assert operation.state == BOOT_OPERATION_SUCCEEDED
    assert contract.ctx.store.getResource("witness", witness_id) is None
    assert contract.ctx.store.getAccount(account.pre).witness_eids == []
    assert witness_boot.delete_calls == [witness_id]


def test_resource_delete_operation_keeps_local_state_on_remote_failure(onboarded_bundle):
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    witness_id = onboarded_bundle["witness_ids"][0]
    witness_record = contract.ctx.store.getResource("witness", witness_id)
    witness_boot = contract.ctx.witness_boots[witness_record.backend_id]
    witness_boot.delete_error = BootError("simulated witness delete failure", status_code=503)

    response = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/witnesses/delete",
            payload={"account_aid": account.pre, "witness_id": witness_id},
        ),
    )

    _, reply = assert_reply_frame(contract, response, route="/account/witnesses/delete")
    run_boot_operations(contract)
    operation = contract.ctx.store.getBootOperation(reply.ked["a"]["operation"]["operation_id"])
    assert operation.state == BOOT_OPERATION_PENDING
    assert operation.last_error == "simulated witness delete failure"
    assert contract.ctx.store.getResource("witness", witness_id) is not None
    assert contract.ctx.store.getAccount(account.pre).witness_eids == [witness_id]
    assert witness_boot.delete_calls == [witness_id]


def test_account_delete_failure_keeps_remaining_state_retryable(onboarded_bundle):
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    session_id = onboarded_bundle["session_id"]
    witness_id = onboarded_bundle["witness_ids"][0]
    watcher_id = onboarded_bundle["watcher_id"]
    witness_record = contract.ctx.store.getResource("witness", witness_id)
    witness_boot = contract.ctx.witness_boots[witness_record.backend_id]
    witness_boot.delete_error = BootError("simulated witness delete failure", status_code=503)

    failed = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/delete",
            payload={"account_aid": account.pre},
        ),
    )

    _, failed_reply = assert_reply_frame(contract, failed, route="/account/delete")
    assert failed_reply.ked["a"]["deleted"] is False

    run_boot_operations(contract)
    operation = contract.ctx.store.getBootOperation(failed_reply.ked["a"]["operation"]["operation_id"])
    assert operation.state == BOOT_OPERATION_PENDING
    assert operation.last_error == "simulated witness delete failure"
    account_record = contract.ctx.store.getAccount(account.pre)
    assert account_record is not None
    assert account_record.status == ACCOUNT_STATE_ONBOARDED
    assert account_record.watcher_eid == ""
    assert account_record.witness_eids == [witness_id]
    assert contract.ctx.store.getSession(session_id) is not None
    assert contract.ctx.store.getResource("watcher", watcher_id) is None
    assert contract.ctx.store.getResource("witness", witness_id) is not None
    assert contract.ctx.watcher_boot.delete_calls == [watcher_id]
    assert witness_boot.delete_calls == [witness_id]

    witness_boot.delete_error = None
    contract.ctx.store.rescheduleBootOperation(
        operation.operation_id,
        due_at="2000-01-01T00:00:00+00:00",
    )
    retry = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/delete",
            payload={"account_aid": account.pre},
        ),
    )

    _, retry_reply = assert_reply_frame(contract, retry, route="/account/delete")
    assert retry_reply.ked["a"]["deleted"] is False
    assert retry_reply.ked["a"]["operation"]["operation_id"] == operation.operation_id
    run_boot_operations(contract)
    operation = contract.ctx.store.getBootOperation(operation.operation_id)
    assert operation.state == BOOT_OPERATION_SUCCEEDED
    assert contract.ctx.store.getAccount(account.pre) is None
    assert contract.ctx.store.getSession(session_id) is None
    assert contract.ctx.store.getResource("witness", witness_id) is None
    assert contract.ctx.watcher_boot.delete_calls == [watcher_id]
    assert witness_boot.delete_calls == [witness_id, witness_id]


def test_account_delete_pending_blocks_state_changing_account_routes(onboarded_bundle):
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    witness_id = onboarded_bundle["witness_ids"][0]
    watcher_id = onboarded_bundle["watcher_id"]

    delete = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/delete",
            payload={"account_aid": account.pre},
        ),
    )
    _, delete_reply = assert_reply_frame(contract, delete, route="/account/delete")
    operation_id = delete_reply.ked["a"]["operation"]["operation_id"]

    witnesses = post_cesr(
        contract,
        "/account",
        build_exn(account, route="/account/witnesses", payload={"account_aid": account.pre}),
    )
    _, witnesses_reply = assert_reply_frame(contract, witnesses, route="/account/witnesses")
    assert witnesses_reply.ked["a"]["account_delete_operation"]["operation_id"] == operation_id

    watcher_status = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/watchers/status",
            payload={"account_aid": account.pre, "watcher_id": watcher_id},
        ),
    )
    assert watcher_status.status_code == 409
    assert watcher_status.json["title"] == "Account deletion pending"

    witness_delete = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/witnesses/delete",
            payload={"account_aid": account.pre, "witness_id": witness_id},
        ),
    )
    assert witness_delete.status_code == 409
    assert witness_delete.json["title"] == "Account deletion pending"


@pytest.mark.parametrize(
    ("route", "payload_builder"),
    [
        (
            "/account/witnesses/delete",
            lambda bundle: {"account_aid": bundle["account"].pre, "witness_id": bundle["witness_ids"][0]},
        ),
        (
            "/account/watchers/delete",
            lambda bundle: {"account_aid": bundle["account"].pre, "watcher_id": bundle["watcher_id"]},
        ),
    ],
)
def test_resources_delete_routes_enforce_normal_account_request_quota(
    contract_factory,
    route,
    payload_builder,
):
    """Test that witnesses and watchers delete routes count towards the requests per minute limit"""
    contract = contract_factory(
        bootstrap_accounts_per_ip=100,
        bootstrap_aids_per_ip=100,
        bootstrap_account_options=("1-of-1",),
        account_profiles=(
            AccountProfile(
                tier="trial",
                code="1-of-1",
                max_accounts=100,
                max_requests_per_minute=1,  # Note that the max requests per minute is 1
                api_budget=100,
            ),
        ),
    )

    with (
        habbing.openHab(name=f"quota-delete-ephemeral-{route.split('/')[-1]}", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name=f"quota-delete-account-{route.split('/')[-1]}", temp=True) as (_, account),
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
        
        # Send the 1st request which should be accepted
        accepted = post_cesr(
            contract,
            "/account",
            build_exn(account, route="/account/witnesses", payload={"account_aid": account.pre}),
        )
        assert accepted.status_code == 200
        account_record = contract.ctx.store.getAccount(account.pre)

        # 2nd request gets rejected 
        rejected = post_cesr(
            contract,
            "/account",
            build_exn(
                account,
                route=route,
                payload=payload_builder(
                    {
                        "account": account,
                        "witness_ids": list(account_record.witness_eids),
                        "watcher_id": account_record.watcher_eid,
                    }
                ),
            ),
        )

    assert rejected.status_code == 429
    assert rejected.json["title"] == "Account request rate limit exceeded"


def test_account_delete_route_bypasses_normal_account_request_quota(contract_factory):
    """Test that account delete route is not limited by limit"""
    contract = contract_factory(
        bootstrap_accounts_per_ip=100,
        bootstrap_aids_per_ip=100,
        bootstrap_account_options=("1-of-1",),
        account_profiles=(
            AccountProfile(
                tier="trial",
                code="1-of-1",
                max_accounts=100,
                max_requests_per_minute=1,      # Note the max requests per minute is 1
                api_budget=100,
            ),
        ),
    )

    with (
        habbing.openHab(name="delete-bypass-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name="delete-bypass-account", temp=True) as (_, account),
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

        # 1st requests should get accepted
        accepted = post_cesr(
            contract,
            "/account",
            build_exn(account, route="/account/witnesses", payload={"account_aid": account.pre}),
        )
        assert accepted.status_code == 200

        # 2nd request is rejected
        exhausted = post_cesr(
            contract,
            "/account",
            build_exn(account, route="/account/watchers", payload={"account_aid": account.pre}),
        )
        assert exhausted.status_code == 429
        assert exhausted.json["title"] == "Account request rate limit exceeded"

        # Account deletion request is accepted because it is not limited
        deleted = post_cesr(
            contract,
            "/account",
            build_exn(account, route="/account/delete", payload={"account_aid": account.pre}),
        )

    _, reply = assert_reply_frame(contract, deleted, route="/account/delete")
    assert reply.ked["a"]["deleted"] is False
    assert reply.ked["a"]["operation"]["kind"] == BOOT_OPERATION_ACCOUNT_DELETE
    assert contract.ctx.store.getAccount(account.pre) is not None


def test_account_delete_route_throttle_for_non_account_senders(contract_factory, monkeypatch):
    """Test that account delete request are throttled through IP"""
    clock = freeze_boot_time(monkeypatch, datetime(2026, 1, 1, tzinfo=UTC))
    contract = contract_factory(
        bootstrap_accounts_per_ip=100,
        bootstrap_aids_per_ip=100,
        bootstrap_account_options=("1-of-1",),
        bootstrap_api_requests_per_minute=1,
    )

    with habbing.openHab(name="delete-only-sender", temp=True) as (_, account):
        register_aid(contract, "/account", account)

        # 1st request gets accepted
        first = post_cesr(
            contract,
            "/account",
            build_exn(account, route="/account/delete", payload={"account_aid": account.pre}),
            remote_addr="198.51.100.50",
        )
        _, first_reply = assert_reply_frame(contract, first, route="/account/delete")
        assert first_reply.ked["a"]["deleted"] is True

        # 2nd request hits the throttle
        second = post_cesr(
            contract,
            "/account",
            build_exn(account, route="/account/delete", payload={"account_aid": account.pre}),
            remote_addr="198.51.100.50",
        )

        assert second.status_code == 429
        assert second.json["title"] == "Account delete rate limit exceeded"

        # Move the time past 1 min
        clock.value += timedelta(seconds=61)

        # Request is accepted
        later = post_cesr(
            contract,
            "/account",
            build_exn(account, route="/account/delete", payload={"account_aid": account.pre}),
            remote_addr="198.51.100.50",
        )

    _, later_reply = assert_reply_frame(contract, later, route="/account/delete")
    assert later_reply.ked["a"]["deleted"] is True


@pytest.mark.parametrize(
    ("status_response", "expected_status"),
    [
        ({"summary": {"total_witnesses": 0, "responsive_witnesses": 0}}, "created"),
        ({"summary": {"total_witnesses": 3, "responsive_witnesses": 1}}, "query_pending"),
        (
            {
                "status": "lagging",
                "summary": {"total_witnesses": 3, "responsive_witnesses": 3},
            },
            "lagging",
        ),
    ],
)
def test_account_watcherStatus_derives_non_happy_path_labels(
    onboarded_bundle,
    status_response,
    expected_status,
):
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    watcher_id = onboarded_bundle["watcher_id"]

    contract.ctx.watcher_boot.status_response = {
        "controller_id": account.pre,
        **status_response,
    }

    response = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/watchers/status",
            payload={"account_aid": account.pre, "watcher_id": watcher_id},
        ),
    )

    _, reply = assert_reply_frame(contract, response, route="/account/watchers/status")
    assert reply.ked["a"]["watcher_id"] == watcher_id
    assert reply.ked["a"]["operation"]["state"] == BOOT_OPERATION_PENDING
    assert reply.ked["a"]["watcher"]["status"] == "created"

    run_boot_operations(contract)
    operation = contract.ctx.store.getBootOperation(reply.ked["a"]["operation"]["operation_id"])
    assert operation.state == BOOT_OPERATION_SUCCEEDED
    assert operation.result == {"eid": watcher_id, "controller_id": account.pre, **status_response}
    assert contract.ctx.store.getResource("watcher", watcher_id).status == expected_status


def test_witness_delete_routes_to_the_persisted_backend_id(onboarded_bundle):
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    witness_id = onboarded_bundle["witness_ids"][0]
    record = contract.ctx.store.getResource("witness", witness_id)
    target_backend = next(
        backend
        for backend in reversed(contract.ctx.config.witness_backends)
        if backend.id != record.backend_id
    )
    record.backend_id = target_backend.id
    record.boot_url = target_backend.boot_url
    record.url = target_backend.public_url
    contract.ctx.store.saveResource(record)

    response = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/witnesses/delete",
            payload={"account_aid": account.pre, "witness_eid": witness_id},
        ),
    )

    _, reply = assert_reply_frame(contract, response, route="/account/witnesses/delete")
    assert reply.ked["a"]["witness_id"] == witness_id
    assert reply.ked["a"]["operation"]["state"] == BOOT_OPERATION_PENDING
    run_boot_operations(contract)
    for backend_id, boot in contract.ctx.witness_boots.items():
        expected = [witness_id] if backend_id == target_backend.id else []
        assert boot.delete_calls == expected


def test_witness_delete_routes_legacy_records_by_public_url_when_backend_fields_are_missing(onboarded_bundle):
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    witness_id = onboarded_bundle["witness_ids"][0]
    record = contract.ctx.store.getResource("witness", witness_id)
    expected_backend_id = record.backend_id

    for boot in contract.ctx.witness_boots.values():
        boot.delete_calls.clear()

    record.backend_id = ""
    record.boot_url = ""
    contract.ctx.store.saveResource(record)

    response = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/witnesses/delete",
            payload={"account_aid": account.pre, "witness_eid": witness_id},
        ),
    )

    _, reply = assert_reply_frame(contract, response, route="/account/witnesses/delete")
    assert reply.ked["a"]["witness_id"] == witness_id
    assert reply.ked["a"]["operation"]["state"] == BOOT_OPERATION_PENDING
    run_boot_operations(contract)
    for backend_id, boot in contract.ctx.witness_boots.items():
        expected = [witness_id] if backend_id == expected_backend_id else []
        assert boot.delete_calls == expected


def test_witness_delete_routes_legacy_records_by_public_host_and_port_when_url_is_missing(onboarded_bundle):
    """Tests witness deletes can still resolve legacy records through host and port metadata."""
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    witness_id = onboarded_bundle["witness_ids"][0]
    record = contract.ctx.store.getResource("witness", witness_id)
    expected_backend_id = record.backend_id

    for boot in contract.ctx.witness_boots.values():
        boot.delete_calls.clear()

    record.backend_id = ""
    record.boot_url = ""
    record.url = ""
    contract.ctx.store.saveResource(record)

    response = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/witnesses/delete",
            payload={"account_aid": account.pre, "witness_eid": witness_id},
        ),
    )

    _, reply = assert_reply_frame(contract, response, route="/account/witnesses/delete")
    assert reply.ked["a"]["witness_id"] == witness_id
    assert reply.ked["a"]["operation"]["state"] == BOOT_OPERATION_PENDING
    run_boot_operations(contract)
    for backend_id, boot in contract.ctx.witness_boots.items():
        expected = [witness_id] if backend_id == expected_backend_id else []
        assert boot.delete_calls == expected


def test_account_witnesses_route_tolerates_legacy_resource_rows(onboarded_bundle, monkeypatch):
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    witness_id = onboarded_bundle["witness_ids"][0]
    original_iter = contract.ctx.store.baser.resources.getTopItemIter

    junk_row = SimpleNamespace(
        kind="witness",
        eid="LEGACY_JUNK",
        url="https://legacy-junk.example",
        oobis=["https://legacy-junk.example/oobi/LEGACY_JUNK/controller"],
        status="allocated",
    )
    legacy_row = SimpleNamespace(
        kind="witness",
        eid="LEGACY_MATCH",
        principal=account.pre,
        cid=account.pre,
        name="Legacy Witness",
        identifier_alias="legacy",
        region_id="legacy-region",
        region_name="Legacy Region",
        url="https://legacy.example",
        oobis=["https://legacy.example/oobi/LEGACY_MATCH/controller"],
        status="allocated",
    )

    def fake_iter(*args, **kwargs):
        keys = kwargs.get("keys", args[0] if args else ())
        if keys == ("witness",):
            yield (("witness", "LEGACY_JUNK"), junk_row)
            yield (("witness", "LEGACY_MATCH"), legacy_row)
        yield from original_iter(*args, **kwargs)

    monkeypatch.setattr(contract.ctx.store.baser.resources, "getTopItemIter", fake_iter)

    response = post_cesr(
        contract,
        "/account",
        build_exn(account, route="/account/witnesses", payload={"account_aid": account.pre}),
    )

    _, reply = assert_reply_frame(contract, response, route="/account/witnesses")
    rows = reply.ked["a"]["witnesses"]
    assert {row["eid"] for row in rows} == {witness_id, "LEGACY_MATCH"}
    assert all(row["eid"] != "LEGACY_JUNK" for row in rows)
    legacy_api = next(row for row in rows if row["eid"] == "LEGACY_MATCH")
    assert legacy_api["created_at"] == ""
    assert legacy_api["witness_url"] == "https://legacy.example"


@pytest.mark.parametrize(
    ("route", "payload"),
    [
        ("/account/witnesses", lambda bundle: {"account_aid": bundle["account"].pre}),
        ("/account/watchers", lambda bundle: {"account_aid": bundle["account"].pre}),
        ("/account/delete", lambda bundle: {"account_aid": bundle["account"].pre}),
        (
            "/account/watchers/status",
            lambda bundle: {"account_aid": bundle["account"].pre, "watcher_id": bundle["watcher_id"]},
        ),
        (
            "/account/witnesses/delete",
            lambda bundle: {"account_aid": bundle["account"].pre, "witness_id": bundle["witness_ids"][0]},
        ),
        (
            "/account/watchers/delete",
            lambda bundle: {"account_aid": bundle["account"].pre, "watcher_id": bundle["watcher_id"]},
        ),
    ],
)
def test_approved_account_routes_require_an_onboarded_account(pending_account_bundle, route, payload):
    contract = pending_account_bundle["contract"]
    account = pending_account_bundle["account"]

    response = post_cesr(
        contract,
        "/account",
        build_exn(account, route=route, payload=payload(pending_account_bundle)),
    )

    assert response.status_code == 409
    assert response.json["title"] == "Account not onboarded"
    assert contract.ctx.store.getAccount(account.pre).status == ACCOUNT_STATE_PENDING_ONBOARDING


@pytest.mark.parametrize(
    ("route", "payload"),
    [
        ("/account/witnesses", {"account_aid": "different-account"}),
        ("/account/watchers", {"account_aid": "different-account"}),
        ("/account/delete", {"account_aid": "different-account"}),
        (
            "/account/watchers/status",
            {"account_aid": "different-account", "watcher_id": "ignored"},
        ),
        (
            "/account/witnesses/delete",
            {"account_aid": "different-account", "witness_id": "ignored"},
        ),
        (
            "/account/watchers/delete",
            {"account_aid": "different-account", "watcher_id": "ignored"},
        ),
    ],
)
def test_approved_account_routes_reject_account_principal_mismatch(onboarded_bundle, route, payload):
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]

    response = post_cesr(contract, "/account", build_exn(account, route=route, payload=payload))

    assert response.status_code == 401
    assert response.json["title"] == "Account principal mismatch"


def test_account_resource_routes_return_404_for_missing_resources(onboarded_bundle):
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]

    missing_watcher = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/watchers/status",
            payload={"account_aid": account.pre, "watcher_id": "missing-watcher"},
        ),
    )
    assert missing_watcher.status_code == 404
    assert missing_watcher.json["title"] == "Watcher not found"

    missing_witness_delete = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/witnesses/delete",
            payload={"account_aid": account.pre, "witness_id": "missing-witness"},
        ),
    )
    assert missing_witness_delete.status_code == 404
    assert missing_witness_delete.json["title"] == "Witness not found"

    missing_watcher_delete = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/watchers/delete",
            payload={"account_aid": account.pre, "watcher_id": "missing-watcher"},
        ),
    )
    assert missing_watcher_delete.status_code == 404
    assert missing_watcher_delete.json["title"] == "Watcher not found"


@pytest.mark.parametrize(
    ("status_code", "expected_state"),
    [
        (400, BOOT_OPERATION_FAILED),
        (404, BOOT_OPERATION_FAILED),
        (409, BOOT_OPERATION_FAILED),
        (503, BOOT_OPERATION_PENDING),
    ],
)
def test_account_watcher_status_operation_records_downstream_boot_errors(
    onboarded_bundle,
    status_code,
    expected_state,
):
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    watcher_id = onboarded_bundle["watcher_id"]
    original_status = contract.ctx.store.getResource("watcher", watcher_id).status

    contract.ctx.watcher_boot.status_error = BootError(f"downstream {status_code}", status_code=status_code)

    response = post_cesr(
        contract,
        "/account",
        build_exn(
            account,
            route="/account/watchers/status",
            payload={"account_aid": account.pre, "watcher_id": watcher_id},
        ),
    )

    _, reply = assert_reply_frame(contract, response, route="/account/watchers/status")
    operation_id = reply.ked["a"]["operation"]["operation_id"]
    assert reply.ked["a"]["operation"]["state"] == BOOT_OPERATION_PENDING

    run_boot_operations(contract)
    operation = contract.ctx.store.getBootOperation(operation_id)
    assert operation.state == expected_state
    assert operation.last_error == f"downstream {status_code}"
    if expected_state == BOOT_OPERATION_FAILED:
        assert operation.result == {"status_code": status_code}
        assert operation.due_at == ""
    else:
        assert operation.result == {}
        assert operation.due_at > operation.last_attempt_at
    assert contract.ctx.store.getResource("watcher", watcher_id).status == original_status


def test_account_routes_enforce_persisted_witness_profile_code(contract_factory):
    """Tests account-route quotas use the account's stored witness profile."""
    contract = contract_factory(
        bootstrap_accounts_per_ip=100,
        bootstrap_aids_per_ip=100,
        account_profiles=(
            AccountProfile(
                tier="trial",
                code="1-of-1",
                max_accounts=100,
                max_requests_per_minute=100,
                api_budget=100
            ),
            AccountProfile(
                tier="org",
                code="3-of-4",
                max_accounts=100,
                max_requests_per_minute=1,
                api_budget=100
            ),
        ),
    )

    with (
        habbing.openHab(name="persisted-profile-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name="persisted-profile-account", temp=True) as (_, account),
    ):
        register_aid(contract, "/onboarding", ephemeral)
        register_aid(contract, "/account", account)

        # Complete onboarding with "3-of-4" profile
        _, _, start_reply = start_session(
            contract,
            ephemeral,
            account_aid=account.pre,
            account_alias="org-alpha",
            chosen_profile_code="3-of-4",
        )
        create_account(contract, ephemeral, start_reply, account_aid=account.pre)
        complete_session(
            contract,
            ephemeral,
            session_id=start_reply.ked["a"]["session_id"],
            account_aid=account.pre,
        )

        record = contract.ctx.store.getAccount(account.pre)
        assert record.witness_profile_code == "3-of-4"

        accepted = post_cesr(
            contract,
            "/account",
            build_exn(account, route="/account/witnesses", payload={"account_aid": account.pre}),
        )
        assert accepted.status_code == 200

        # Request gets rejected based on the "3-of-4" profile's max_requests_per_minute limit
        rejected = post_cesr(
            contract,
            "/account",
            build_exn(account, route="/account/witnesses", payload={"account_aid": account.pre}),
        )

    assert rejected.status_code == 429
    assert rejected.json["title"] == "Account request rate limit exceeded"


def test_expire_accounts_transitions_onboarded_account_to_expired(onboarded_bundle):
    """Ensure onboarded accounts are moved to expired status when their expiry date passes."""
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    record = contract.ctx.store.getAccount(account.pre)
    
    # Set the account expiry date to the past to trigger expiration
    record.expires_at = "2000-01-01T00:00:00+00:00"
    contract.ctx.store.saveAccount(record)

    # Manually trigger the expiration process
    sweep = sweep_do(
        contract.ctx.exchanger.expirer,
        now="2026-01-01T00:00:00+00:00",
        batch_size=1,
    )

    updated = contract.ctx.store.getAccount(account.pre)
    assert updated is not None
    assert sweep["accounts_expired"] == 1
    assert updated.status == ACCOUNT_STATE_EXPIRED


def test_account_route_marks_past_due_account_expired_on_ingress(onboarded_bundle):
    """Past-due accounts are marked expired on ingress without doing heavy cleanup there."""
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    session_id = onboarded_bundle["session_id"]
    witness_ids = onboarded_bundle["witness_ids"]
    watcher_id = onboarded_bundle["watcher_id"]

    # Get record for that account and expire it
    record = contract.ctx.store.getAccount(account.pre)
    record.expires_at = "2000-01-01T00:00:00+00:00"

    # Save it to trigger expiration workflow
    contract.ctx.store.saveAccount(record)

    # Send an account request
    response = post_cesr(
        contract,
        "/account",
        build_exn(account, route="/account/witnesses", payload={"account_aid": account.pre}),
    )

    # Retrieve the account record
    updated = contract.ctx.store.getAccount(account.pre)
    assert updated is not None

    # Assert that it was expired and request was rejected
    assert updated.status == ACCOUNT_STATE_EXPIRED
    assert updated.resources_cleaned_at == ""
    assert contract.ctx.store.getSession(session_id) is not None
    assert contract.ctx.store.getResource("watcher", watcher_id) is not None
    for witness_id in witness_ids:
        assert contract.ctx.store.getResource("witness", witness_id) is not None
    assert total_witness_delete_calls(contract.ctx) == []
    assert contract.ctx.watcher_boot.delete_calls == []
    assert response.status_code == 409
    assert response.json["title"] == "Account expired"


def test_account_routes_refresh_idle_account_lease(onboarded_bundle):
    """Test that refreshing an account lease sets it to current time + account TTL even if expires_at is superior"""
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    record = contract.ctx.store.getAccount(account.pre)

    # Expire the account far in the future
    record.expires_at = "2099-01-01T00:00:00+00:00"
    contract.ctx.store.saveAccount(record)

    response = post_cesr(
        contract,
        "/account",
        build_exn(account, route="/account/witnesses", payload={"account_aid": account.pre}),
    )

    assert response.status_code == 200
    updated = contract.ctx.store.getAccount(account.pre)
    assert updated is not None

    # Assert that the expires_at variable changed to be current time + account TTL
    assert updated.expires_at != "2099-01-01T00:00:00+00:00"


def test_cleanup_expired_accounts_retries_with_backoff(onboarded_bundle, monkeypatch):
    """Expired account cleanup should back off after teardown failures instead of retrying every sweep."""
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]

    # Retrieve account and expire it
    record = contract.ctx.store.getAccount(account.pre)
    record.status = ACCOUNT_STATE_EXPIRED
    record.expired_at = "2026-01-01T00:00:00+00:00"
    contract.ctx.store.saveAccount(record)

    # Create an attempts list to track back off behavior
    attempts: list[str] = []

    def fake_teardown_do(*, account_aid: str, account=None, tymth, tock: float = 0.0):
        attempts.append(account_aid)
        yield tock
        raise BootError("simulated teardown failure", status_code=502)

    monkeypatch.setattr(contract.ctx.exchanger.provisioner, "teardownAccountResourcesDo", fake_teardown_do)

    # First sweep: task is due => teardown attempted => failure => task rescheduled with backoff.
    first = sweep_do(
        contract.ctx.exchanger.expirer,
        now="2026-01-01T00:00:00+00:00",
    )

    # Second sweep (30 seconds later): backoff window has NOT elapsed => no retry should occur.
    second = sweep_do(
        contract.ctx.exchanger.expirer,
        now="2026-01-01T00:00:30+00:00",
    )

    # Third sweep (61 seconds later): backoff window HAS elapsed => retry should occur.
    third = sweep_do(
        contract.ctx.exchanger.expirer,
        now="2026-01-01T00:01:01+00:00",
    )

    # Retrieve the account and assert clean up was not successful
    updated = contract.ctx.store.getAccount(account.pre)
    assert updated is not None
    assert updated.status == ACCOUNT_STATE_EXPIRED
    assert updated.resources_cleaned_at == ""

    # Failed cleanups should reschedule without being reported as successfully cleaned.
    assert first["accounts_cleaned"] == 0

    # Second sweep skipped due to backoff
    assert second["accounts_cleaned"] == 0

    # Third sweep retries after backoff, but still does not report success because cleanup failed again.
    assert third["accounts_cleaned"] == 0

    # Assert attempts only contains the 1st and 3rd attempt
    assert attempts == [account.pre, account.pre]


def test_cleanup_expired_accounts_blocks_after_retry_threshold(onboarded_bundle, monkeypatch):
    """Repeated transient failures should eventually stop auto-retrying."""
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]

    contract.ctx.config = contract.ctx.config.__class__(
        **{
            **contract.ctx.config.__dict__,
            "cleanup_block_after_attempts": 2,      # Set the block threshold to 2 attempts
        }
    )
    contract.ctx.exchanger.ctx.config = contract.ctx.config

    # Set account as expired to trigger cleanup
    record = contract.ctx.store.getAccount(account.pre)
    record.status = ACCOUNT_STATE_EXPIRED
    record.expired_at = "2026-01-01T00:00:00+00:00"
    contract.ctx.store.saveAccount(record)

    attempts: list[str] = []

    # Simulate constant teardown failure
    def always_fail_do(*, account_aid: str, account=None, tymth, tock: float = 0.0):
        attempts.append(account_aid)
        yield tock
        raise BootError("simulated teardown failure", status_code=502)

    monkeypatch.setattr(
        contract.ctx.exchanger.provisioner,
        "teardownAccountResourcesDo",
        always_fail_do,
    )

    first = sweep_do(
        contract.ctx.exchanger.expirer,
        now="2026-01-01T00:00:00+00:00",
        batch_size=1,
    )
    second = sweep_do(
        contract.ctx.exchanger.expirer,
        now="2026-01-01T00:01:01+00:00",
        batch_size=1,
    )
    third = sweep_do(
        contract.ctx.exchanger.expirer,
        now="2026-01-01T00:02:02+00:00",
        batch_size=1,
    )

    task = contract.ctx.store.getCleanupTask("account_cleanup", account.pre)
    snapshot = contract.ctx.store.cleanupBacklogSnapshot(now="2026-01-01T00:02:03+00:00")
    
    # Assert the sweeps fail cleanup without counting false success
    assert first["accounts_cleaned"] == 0
    assert second["accounts_cleaned"] == 0
    assert third["accounts_cleaned"] == 0

    # Assert that there was 2 attempts, the 3rd one results in the task being blocked
    assert attempts == [account.pre, account.pre]
    assert task is not None
    assert task.blocked_at == "2026-01-01T00:01:01+00:00"
    assert "Cleanup retry limit reached" in task.blocked_reason
    assert snapshot["blocked_tasks"] == 1


@pytest.mark.parametrize(
    ("status", "title"),
    [
        (ACCOUNT_STATE_EXPIRED, "Account expired"),
    ],
)
def test_account_routes_reject_expired_accounts(onboarded_bundle, status, title):
    """Expired accounts are rejected locally while cleanup waits for the sweeper."""
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    record = contract.ctx.store.getAccount(account.pre)

    # Set status to expired to trigger rejection of account routes
    record.status = status
    contract.ctx.store.saveAccount(record)

    response = post_cesr(
        contract,
        "/account",
        build_exn(account, route="/account/witnesses", payload={"account_aid": account.pre}),
    )

    assert contract.ctx.store.getAccount(account.pre) is not None
    assert response.status_code == 409
    assert response.json["title"] == title

def test_cleanup_expired_accounts_triggers_resource_teardown(contract_factory, monkeypatch):
    """Ensure the cleanup phase tears down resources for already-expired accounts."""
    contract = contract_factory(
        bootstrap_accounts_per_ip=100,
        bootstrap_aids_per_ip=100,
    )

    # Create an onboarded account with resources
    account = contract.ctx.store.buildAccount(
        account_aid="AID_EXPIRED",
        account_alias="beta",
        witness_profile_code="1-of-1",
        witness_count=1,
        toad=1,
        watcher_required=True,
        region_id="test-region",
        region_name="Test Region",
        session_id="SESSION123",
        witness_eids=["WITNESS123"],
        watcher_eid="WATCHER123",
        tier="trial",
        onboarded=True,
    )

    # Set account as expired and save it 
    account.status = ACCOUNT_STATE_EXPIRED
    account.expired_at = "2000-01-01T00:00:00+00:00"
    contract.ctx.store.saveAccount(account)

    cleaned: list[tuple] = []

    def fake_teardown_do(*, account_aid: str, account=None, tymth, tock: float = 0.0):
        yield tock
        # Simulate what teardown_accountResources would do
        account.watcher_eid = ""
        account.witness_eids = []
        account.session_id = ""
        contract.ctx.store.saveAccount(account)
        cleaned.append((account_aid, account))

    monkeypatch.setattr(contract.ctx.exchanger.provisioner, "teardownAccountResourcesDo", fake_teardown_do)

    # Run cleanup logic
    sweep = sweep_do(
        contract.ctx.exchanger.expirer,
        now="2026-01-01T00:00:00+00:00",
        batch_size=1,
    )

    expired = contract.ctx.store.getAccount("AID_EXPIRED")
    assert expired is not None
    assert expired.status == ACCOUNT_STATE_EXPIRED
    assert sweep["accounts_cleaned"] == 1

    # Ensure teardown was invoked exactly once with correct args
    assert cleaned == [("AID_EXPIRED", expired)]

    # Assert that resources were actually cleared
    assert expired.watcher_eid == ""
    assert expired.witness_eids == []
    assert expired.session_id == ""
    assert expired.resources_cleaned_at == "2026-01-01T00:00:00+00:00"


def test_cleanup_sweep_processes_expired_account_in_batches(onboarded_bundle):
    """Tests a bounded sweep should spread expire, cleanup, and delete across multiple passes."""
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    session_id = onboarded_bundle["session_id"]
    witness_ids = onboarded_bundle["witness_ids"]
    watcher_id = onboarded_bundle["watcher_id"]
    record = contract.ctx.store.getAccount(account.pre)
    record.expires_at = "2000-01-01T00:00:00+00:00"
    contract.ctx.store.saveAccount(record)

    # Set batch size to 1 to make sure it only does 1 work
    first = sweep_do(
        contract.ctx.exchanger.expirer,
        batch_size=1,
        now="2026-01-01T00:00:00+00:00",
    )

    # Get the account after the sweep
    after_first = contract.ctx.store.getAccount(account.pre)

    # Assert results of the work done
    assert first == {
        "sessions_expired": 0,
        "sessions_cleaned": 0,
        "sessions_deleted": 0,
        "accounts_expired": 1,
        "accounts_cleaned": 0,
        "accounts_deleted": 0,
    }

    # Assert account status cleanly transitioned to only expired
    assert after_first is not None
    assert after_first.status == ACCOUNT_STATE_EXPIRED

    # Assert resources haven't been cleaned up yet
    assert after_first.resources_cleaned_at == ""
    assert contract.ctx.store.getSession(session_id) is not None
    assert contract.ctx.store.getResource("watcher", watcher_id) is not None
    for witness_id in witness_ids:
        assert contract.ctx.store.getResource("witness", witness_id) is not None
    
    # Run the second sweep with a batch size to only 1
    second = sweep_do(
        contract.ctx.exchanger.expirer,
        batch_size=1,
        now="2026-01-01T00:00:00+00:00",
    )

    # Retrieve the account after the 2nd sweep
    after_second = contract.ctx.store.getAccount(account.pre)

    # Assert results 
    assert second == {
        "sessions_expired": 0,
        "sessions_cleaned": 0,
        "sessions_deleted": 0,
        "accounts_expired": 0,
        "accounts_cleaned": 1,
        "accounts_deleted": 0,
    }

    # Assert account is still present and transitioned from expired to cleaned up with resources torndown
    assert after_second is not None
    assert after_second.resources_cleaned_at
    assert contract.ctx.store.getSession(session_id) is not None
    assert contract.ctx.store.getResource("watcher", watcher_id) is None
    for witness_id in witness_ids:
        assert contract.ctx.store.getResource("witness", witness_id) is None


    # Run a 3rd sweep with batch size of 1
    third = sweep_do(
        contract.ctx.exchanger.expirer,
        batch_size=1,
        now="2026-01-01T00:00:00+00:00",
    )

    # Assert results
    assert third == {
        "sessions_expired": 0,
        "sessions_cleaned": 0,
        "sessions_deleted": 0,
        "accounts_expired": 0,
        "accounts_cleaned": 0,
        "accounts_deleted": 1,
    }

    # Assert account and session got deleted
    assert contract.ctx.store.getAccount(account.pre) is None
    assert contract.ctx.store.getSession(session_id) is None


def test_delete_expired_accounts_removes_account(onboarded_bundle):
    """Test deleteExpiredAccount function is correctly deleting the record of the account"""
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]
    session_id = onboarded_bundle["session_id"]
    witness_ids = onboarded_bundle["witness_ids"]
    watcher_id = onboarded_bundle["watcher_id"]

    # Get the record and set it as expired
    record = contract.ctx.store.getAccount(account.pre)
    record.status = ACCOUNT_STATE_EXPIRED
    record.expired_at = "2026-01-01T00:00:00+00:00"
    record.resources_cleaned_at = "2026-01-01T00:00:00+00:00"

    # Save the changes
    contract.ctx.store.saveAccount(record)

    # Put it a fake cid to assert correct deletion behavior
    contract.ctx.store.addBinding(account.pre, "cid-to-delete")

    # Run the cleanup sweep
    deleted = sweep_do(
        contract.ctx.exchanger.expirer,
        batch_size=1,
        now="2026-01-01T00:00:00+00:00",
    )

    # Assert correct deletion behavior
    assert deleted["accounts_deleted"] == 1
    assert contract.ctx.store.baser.bindings.get(keys=(account.pre, "cid-to-delete")) is None
    assert contract.ctx.store.getAccount(account.pre) is None
    assert contract.ctx.store.getSession(session_id) is None
    assert contract.ctx.store.getResource("watcher", watcher_id) is None
    for witness_id in witness_ids:
        assert contract.ctx.store.getResource("witness", witness_id) is None
    assert total_witness_delete_calls(contract.ctx) == witness_ids
    assert contract.ctx.watcher_boot.delete_calls == [watcher_id]


def test_delete_expired_accounts_ignores_non_expired_accounts(onboarded_bundle):
    """Test that the delete expired account function does not affect non-expired accounts"""
    contract = onboarded_bundle["contract"]
    account = onboarded_bundle["account"]

    deleted = sweep_do(contract.ctx.exchanger.expirer)

    assert deleted["accounts_deleted"] == 0
    assert contract.ctx.store.getAccount(account.pre) is not None


def test_account_request_quota_survives_store_reopen(tmp_path):
    """Test account quotas throttle are persistent even after closing"""
    # Create a config with 2 max requests per minute
    config = make_config(
        tmp_path,
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
    store_path = config.db_path

    # Create an authenticated account-route request.
    serder = SimpleNamespace(
        pre="AID_DURABLE_ACCOUNT",
        ked={
            "r": "/account/witnesses",
            "a": {
                "account_aid": "AID_DURABLE_ACCOUNT",
            },
        },
    )

    first_store = Store(store_path, session_ttl_seconds=config.session_ttl_seconds)
    try:
        account = first_store.buildAccount(
            account_aid="AID_DURABLE_ACCOUNT",
            account_alias="durable",
            witness_profile_code="1-of-1",
            witness_count=1,
            toad=1,
            watcher_required=True,
            region_id="test-region",
            region_name="Test Region",
            session_id="SESSION123",
            witness_eids=[],
            watcher_eid="",
            tier="trial",
            onboarded=True,
        )
        first_store.saveAccount(account)

        limiter = Limiter(SimpleNamespace(config=config, store=first_store))

        # Run the account route 2 times.
        limiter.enforceAccountQuotas(serder)
        limiter.enforceAccountQuotas(serder)
    finally:
        first_store.close()
    
    reopened_store = Store(store_path, session_ttl_seconds=config.session_ttl_seconds)
    try:
        limiter = Limiter(SimpleNamespace(config=config, store=reopened_store))
        with pytest.raises(falcon.HTTPTooManyRequests) as excinfo:
            limiter.enforceAccountQuotas(serder)
        assert excinfo.value.title == "Account request rate limit exceeded"
    finally:
        reopened_store.close()


def test_account_is_set_to_expire_when_budget_fully_used(tmp_path, monkeypatch):
    # Set clock to 2026-01-01T00:00:00+00:00 
    freeze_boot_time(monkeypatch, datetime(2026, 1, 1, tzinfo=UTC))
    config = make_config(
        tmp_path,
        bootstrap_account_options=("1-of-1",),
        account_profiles=(
            AccountProfile(
                tier="trial",
                code="1-of-1",
                max_accounts=100,
                max_requests_per_minute=100,
                api_budget=1,
            ),
        ),
    )
    # Create a request to reach the limit
    serder = SimpleNamespace(
        pre="AID_EXPIRING_USAGE",
        ked={
            "r": "/account/witnesses",
            "a": {
                "account_aid": "AID_EXPIRING_USAGE",
            },
        },
    )

    store = Store(config.db_path, session_ttl_seconds=config.session_ttl_seconds)
    try:
        # Create an account and save it
        account = store.buildAccount(
            account_aid="AID_EXPIRING_USAGE",
            account_alias="expiring",
            witness_profile_code="1-of-1",
            witness_count=1,
            toad=1,
            watcher_required=True,
            region_id="test-region",
            region_name="Test Region",
            session_id="SESSION123",
            witness_eids=[],
            watcher_eid="",
            tier="trial",
            onboarded=True,
        )
        store.saveAccount(account)

        limiter = Limiter(SimpleNamespace(config=config, store=store))
        # Enforce the quotas on that request
        limiter.enforceAccountQuotas(serder)
        # Check the store for the account 
        updated = store.getAccount("AID_EXPIRING_USAGE")
        assert updated is not None
        # Assert API usage changed to 1
        assert updated.api_used == 1
        # Assert the expiration date is immediate
        assert updated.expires_at == "2026-01-01T00:00:00+00:00"
    finally:
        store.close()


def test_last_allowed_account_request_succeeds_before_budget_expiry(contract_factory, monkeypatch):
    """The request that consumes the last API-budget slot should still succeed."""
    freeze_boot_time(monkeypatch, datetime(2026, 1, 1, tzinfo=UTC))
    contract = contract_factory(
        bootstrap_accounts_per_ip=100,
        bootstrap_aids_per_ip=100,
        bootstrap_account_options=("1-of-1",),
        account_profiles=(
            AccountProfile(
                tier="trial",
                code="1-of-1",
                max_accounts=100,
                max_requests_per_minute=100,
                api_budget=1,
            ),
        ),
    )

    with (
        habbing.openHab(name="budget-final-request-ephemeral", temp=True, transferable=False) as (_, ephemeral),
        habbing.openHab(name="budget-final-request-account", temp=True) as (_, account),
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

        accepted = post_cesr(
            contract,
            "/account",
            build_exn(account, route="/account/witnesses", payload={"account_aid": account.pre}),
        )

        updated = contract.ctx.store.getAccount(account.pre)
        assert accepted.status_code == 200
        assert updated is not None
        assert updated.api_used == 1
        assert updated.expires_at == "2026-01-01T00:00:00+00:00"

        rejected = post_cesr(
            contract,
            "/account",
            build_exn(account, route="/account/witnesses", payload={"account_aid": account.pre}),
        )

    assert rejected.status_code == 409
    assert rejected.json["title"] == "Account expired"
