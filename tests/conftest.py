from __future__ import annotations

from contextlib import ExitStack
from itertools import count

import pytest
from falcon import testing
from keri.app import habbing

from kfboot.app import create_app

from .support import (
    FakeWatcherBoot,
    complete_session,
    create_account,
    make_config,
    make_witness_boots,
    register_aid,
    run_boot_operations,
    start_session,
)


@pytest.fixture
def contract_factory(tmp_path):
    contexts = []
    counter = count()

    def factory(*, witness_boots=None, witness_boot_overrides=None, watcher_boot=None, **config_overrides):
        index = next(counter)
        config = make_config(tmp_path, index=index, **config_overrides)
        app, ctx = create_app(config=config, temp=True)

        witness_boots = witness_boots or make_witness_boots(
            config.witness_backends,
            overrides=witness_boot_overrides,
        )
        watcher_boot = watcher_boot or FakeWatcherBoot()
        ctx.witness_boots = witness_boots
        ctx.watcher_boot = watcher_boot
        ctx.exchanger.ctx.witness_boots = witness_boots
        ctx.exchanger.ctx.watcher_boot = watcher_boot
        ctx.exchanger.provisioner.configureCleanupBootClients(
            witness_boots=witness_boots,
            watcher_boot=watcher_boot,
        )

        client = testing.TestClient(app)
        client.ctx = ctx
        contexts.append(ctx)
        return client

    yield factory

    for ctx in reversed(contexts):
        ctx.close(clear=True)


@pytest.fixture
def contract(contract_factory):
    return contract_factory()


@pytest.fixture
def pending_account_bundle(contract):
    with ExitStack() as stack:
        _, ephemeral = stack.enter_context(habbing.openHab(name="pending-ephemeral", temp=True, transferable=False))
        _, account = stack.enter_context(habbing.openHab(name="pending-account", temp=True))

        register_aid(contract, "/onboarding", ephemeral)
        _, _, start_reply = start_session(contract, ephemeral, account_aid=account.pre)
        session_id = start_reply.ked["a"]["session_id"]
        run_boot_operations(contract)
        session = contract.ctx.store.getSession(session_id)

        _, _, create_reply = create_account(contract, ephemeral, start_reply, account_aid=account.pre)
        register_aid(contract, "/account", account)

        yield {
            "contract": contract,
            "ephemeral": ephemeral,
            "account": account,
            "session_id": session_id,
            "witness_ids": list(session.witness_eids),
            "watcher_id": session.watcher_eid,
            "start_reply": start_reply,
            "create_reply": create_reply,
        }


@pytest.fixture
def onboarded_bundle(pending_account_bundle):
    contract = pending_account_bundle["contract"]
    ephemeral = pending_account_bundle["ephemeral"]
    account = pending_account_bundle["account"]
    session_id = pending_account_bundle["session_id"]

    _, _, complete_reply = complete_session(
        contract,
        ephemeral,
        session_id=session_id,
        account_aid=account.pre,
    )

    return {
        **pending_account_bundle,
        "complete_reply": complete_reply,
    }
