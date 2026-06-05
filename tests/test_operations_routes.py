from __future__ import annotations

from keri.app import habbing

from kfboot.basing import (
    BOOT_OPERATION_ACCOUNT_DELETE,
    BOOT_OPERATION_SESSION_PROVISION,
)

from .support import assert_reply_frame, build_exn, post_cesr, register_aid, start_session


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
