"""Account API orchestration of the standalone provider, without a hosted queue."""
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from keri.app import habbing
from keri.core import Diger, Number, exchange
from keri.core.eventing import messagize
from keri.help.helping import toIso8601
from keri.kering import Kinds, Vrsn_2_0

from kfboot.basing import ACCOUNT_STATE_ONBOARDED, AccountRecord
from kfboot.boot_client import BootError
from kfboot.mailboxing import BINDING_CLOCK_SKEW_SECONDS, BINDING_MAX_AGE_SECONDS
from kfboot.operating import BootOperationDoer, BootOperationProcessor

from .support import assert_reply_frame, build_exn, drain_do, post_cesr, register_aid


@dataclass
class MailboxProvider:
    eid: str
    allocations: list = field(default_factory=list)
    deletions: list = field(default_factory=list)
    registered: set = field(default_factory=set)
    delete_error: Exception | None = None

    def allocateMailboxDo(self, aid, *, tymth, tock=0.0):
        self.allocations.append(aid)
        yield tock
        self.registered.add(aid)
        return {'cid': aid, 'eid': self.eid, 'url': 'http://127.0.0.1:6732',
                'oobis': [f'http://127.0.0.1:6732/oobi/{self.eid}/controller'],
                'name': 'kf-mailbox', 'status': 'allocated'}

    def deleteMailboxDo(self, aid, *, tymth, tock=0.0):
        self.deletions.append(aid)
        yield tock
        if self.delete_error:
            raise self.delete_error
        self.registered.discard(aid)


def sign_binding(contract, account, recipient, *, bound_account=None, receiver=None, stamp=None):
    binding = exchange(sender=recipient.pre, receiver=receiver or contract.ctx.host_hab.pre,
                       route='/mailbox/bind', attributes={'account_aid': bound_account or account.pre,
                       'recipient_aid': recipient.pre}, stamp=stamp, version=Vrsn_2_0, kind=Kinds.json)
    return recipient.endorse(binding, last=False, framed=True, gvrsn=Vrsn_2_0).decode()


def request_mailbox(contract, account, recipient, binding=None):
    request = build_exn(account, route='/account/mailboxes/create', payload={
        'account_aid': account.pre, 'recipient_aid': recipient.pre,
        'binding': sign_binding(contract, account, recipient) if binding is None else binding})
    return post_cesr(contract, '/account', request)


def create(contract, account, recipient, binding=None):
    response = request_mailbox(contract, account, recipient, binding)
    _, reply = assert_reply_frame(contract, response, route='/account/mailboxes/create')
    return reply.ked['a']


def onboard(contract, hab):
    register_aid(contract, '/account', hab)
    contract.ctx.store.saveAccount(AccountRecord(account_aid=hab.pre, status=ACCOUNT_STATE_ONBOARDED))


def process(bundle):
    ctx = bundle.contract.ctx
    runner = BootOperationDoer(store=ctx.store, witness_boots=ctx.witness_boots,
                              watcher_boot=ctx.watcher_boot,
                              processor=BootOperationProcessor(ctx.exchanger.provisioner, bundle.provider))
    return drain_do(runner.processDueDo(tymth=lambda: 0.0))


@pytest.fixture
def contract(contract_factory, request):
    return contract_factory(**getattr(request, 'param', {}))


@pytest.fixture
def mailbox_bundle(contract):
    with habbing.openHab(name='mail-account', temp=True) as (_, account), \
            habbing.openHab(name='mail-recipient', temp=True) as (_, recipient), \
            habbing.openHab(name='mail-provider', temp=True, transferable=False) as (_, operator):
        onboard(contract, account)
        register_aid(contract, '/account', recipient)
        provider = MailboxProvider(operator.pre)
        yield SimpleNamespace(contract=contract, account=account, recipient=recipient, provider=provider)


def test_allocation_uses_deferred_provider_and_recipient_identity(mailbox_bundle):
    b = mailbox_bundle
    reply = create(b.contract, b.account, b.recipient)
    assert reply['mailbox']['status'] == 'pending'
    assert b.provider.allocations == []
    # Another ordinary account request remains available before remote completion.
    listed = post_cesr(b.contract, '/account', build_exn(b.account, route='/account/mailboxes', payload={}))
    _, listing = assert_reply_frame(b.contract, listed, route='/account/mailboxes')
    assert listing.ked['a']['mailboxes'][0]['recipient_aid'] == b.recipient.pre
    process(b)
    operation = b.contract.ctx.store.getBootOperation(reply['operation']['operation_id'])
    assert operation.state == 'succeeded'
    allocated = operation.result['mailbox']
    assert allocated['eid'] == b.provider.eid != b.contract.ctx.host_hab.pre
    assert allocated['recipient_aid'] == b.recipient.pre != b.account.pre
    assert allocated['oobis'] == [f'http://127.0.0.1:6732/oobi/{b.provider.eid}/controller']
    assert b.provider.allocations == [b.recipient.pre]


def test_allocation_retry_recovers_one_provider_registration(mailbox_bundle):
    b = mailbox_bundle
    first = create(b.contract, b.account, b.recipient)
    retried = create(b.contract, b.account, b.recipient)
    assert first['mailbox']['mailbox_id'] == retried['mailbox']['mailbox_id']
    assert first['operation']['operation_id'] == retried['operation']['operation_id']
    process(b)
    recovered = create(b.contract, b.account, b.recipient)
    assert recovered['mailbox']['status'] == 'active'
    assert recovered['mailbox']['mailbox_id'] == first['mailbox']['mailbox_id']
    assert 'operation' not in recovered
    assert b.provider.allocations == [b.recipient.pre]


def test_provider_cleanup_retains_metadata_until_remote_removal(mailbox_bundle):
    b = mailbox_bundle
    create(b.contract, b.account, b.recipient)
    process(b)
    provisioner = b.contract.ctx.exchanger.provisioner
    b.provider.delete_error = BootError('provider unavailable', status_code=503)
    with pytest.raises(BootError):
        drain_do(provisioner.deleteAccountMailboxesDo(
            b.account.pre, mailbox_boot=b.provider, tymth=lambda: 0.0))
    assert b.contract.ctx.store.mailboxes.get(b.recipient.pre).status == 'deleting'
    with pytest.raises(RuntimeError):
        b.contract.ctx.store.deleteAccount(b.account.pre)
    b.provider.delete_error = BootError('already removed', status_code=404)
    drain_do(provisioner.deleteAccountMailboxesDo(
        b.account.pre, mailbox_boot=b.provider, tymth=lambda: 0.0))
    assert b.contract.ctx.store.mailboxes.get(b.recipient.pre).status == 'deleted'
    b.contract.ctx.store.deleteAccount(b.account.pre)
    assert b.contract.ctx.store.getAccount(b.account.pre) is None


def test_cleanup_cancels_pending_allocation_before_remote_delete(mailbox_bundle):
    b = mailbox_bundle
    reply = create(b.contract, b.account, b.recipient)
    drain_do(b.contract.ctx.exchanger.provisioner.deleteAccountMailboxesDo(
        b.account.pre, mailbox_boot=b.provider, tymth=lambda: 0.0))
    process(b)
    assert b.contract.ctx.store.getBootOperation(reply['operation']['operation_id']).state == 'failed'
    assert b.provider.allocations == []
    assert b.provider.deletions == [b.recipient.pre]
    assert b.contract.ctx.store.mailboxes.get(b.recipient.pre).status == 'deleted'


@pytest.mark.parametrize('case, title', [
    ('account', 'Recipient binding does not match this allocation'),
    ('receiver', 'Recipient binding does not match this allocation'),
    ('expired', 'Recipient binding has expired'),
    ('future', 'Recipient binding has expired'),
    ('skewed', None),
])
def test_allocation_requires_current_recipient_consent(mailbox_bundle, case, title):
    b = mailbox_bundle
    now = datetime.now(UTC)
    binding = sign_binding(b.contract, b.account, b.recipient, **{
        'account': {'bound_account': b.recipient.pre},
        'receiver': {'receiver': b.account.pre},
        'expired': {'stamp': toIso8601(now - timedelta(seconds=BINDING_MAX_AGE_SECONDS + 5))},
        'future': {'stamp': toIso8601(now + timedelta(seconds=BINDING_CLOCK_SKEW_SECONDS + 5))},
        'skewed': {'stamp': toIso8601(now + timedelta(seconds=BINDING_CLOCK_SKEW_SECONDS - 5))},
    }[case])
    response = request_mailbox(b.contract, b.account, b.recipient, binding)
    if title is None:
        assert_reply_frame(b.contract, response, route='/account/mailboxes/create')
        assert b.contract.ctx.store.mailboxes.get(b.recipient.pre).status == 'pending'
    else:
        assert response.status_code == 400
        assert response.json['title'] == title
        assert b.contract.ctx.store.mailboxes.get(b.recipient.pre) is None


def test_deleted_allocation_requires_new_recipient_consent(mailbox_bundle):
    b = mailbox_bundle
    binding = sign_binding(b.contract, b.account, b.recipient)
    mailbox = create(b.contract, b.account, b.recipient, binding)['mailbox']
    process(b)
    deleted = post_cesr(b.contract, '/account', build_exn(b.account, route='/account/mailboxes/delete', payload={
        'recipient_aid': b.recipient.pre, 'mailbox_id': mailbox['mailbox_id']}))
    _, reply = assert_reply_frame(b.contract, deleted, route='/account/mailboxes/delete')
    assert reply.ked['a']['mailbox']['status'] == 'deleting'
    process(b)
    assert b.provider.deletions == [b.recipient.pre]
    assert b.contract.ctx.store.mailboxes.get(b.recipient.pre).status == 'deleted'

    replayed = request_mailbox(b.contract, b.account, b.recipient, binding)
    assert replayed.status_code == 409
    assert replayed.json['title'] == 'A new recipient binding is required'
    assert create(b.contract, b.account, b.recipient)['mailbox']['mailbox_id'] != mailbox['mailbox_id']


@pytest.mark.parametrize('contract', [{'mailbox_limit_per_account': 1}], indirect=True)
def test_allocation_enforces_recipient_ownership_and_account_limit(mailbox_bundle):
    b = mailbox_bundle
    ctx = b.contract.ctx
    create(b.contract, b.account, b.recipient)
    with habbing.openHab(name='mail-other-account', temp=True) as (_, other), \
            habbing.openHab(name='mail-other-recipient', temp=True) as (_, extra):
        onboard(b.contract, other)
        register_aid(b.contract, '/account', extra)
        taken = request_mailbox(b.contract, other, b.recipient)
        assert taken.status_code == 409
        assert taken.json['title'] == 'Recipient already has a mailbox'
        limited = request_mailbox(b.contract, b.account, extra)
        assert limited.status_code == 409
        assert limited.json['title'] == 'Mailbox allocation limit reached'
    assert [row.recipient_aid for row in ctx.store.mailboxes.list(b.account.pre)] == [b.recipient.pre]
    assert ctx.store.mailboxes.list(other.pre) == []


def test_overlapping_cleanup_cannot_delete_replacement_registration(mailbox_bundle):
    b = mailbox_bundle
    create(b.contract, b.account, b.recipient)
    process(b)
    old = b.contract.ctx.store.mailboxes.get(b.recipient.pre)
    provisioner = b.contract.ctx.exchanger.provisioner
    first = provisioner.deleteMailboxDo(old, mailbox_boot=b.provider, tymth=lambda: 0.0)
    second = provisioner.deleteMailboxDo(old, mailbox_boot=b.provider, tymth=lambda: 0.0)
    next(first)
    next(second)
    drain_do(first)
    replacement = create(b.contract, b.account, b.recipient)
    assert replacement['mailbox']['mailbox_id'] != old.mailbox_id
    process(b)
    drain_do(second)
    assert b.recipient.pre in b.provider.registered
    assert b.contract.ctx.store.mailboxes.get(b.recipient.pre).status == 'active'


@pytest.mark.parametrize('case', ['empty', 'malformed', 'truncated', 'second-frame', 'trailing'])
def test_allocation_requires_one_complete_binding(mailbox_bundle, case):
    b = mailbox_bundle
    binding = sign_binding(b.contract, b.account, b.recipient)
    invalid = {'empty': '', 'malformed': 'not CESR', 'truncated': binding[:-1],
               'second-frame': binding + binding, 'trailing': binding + 'garbage'}[case]
    response = request_mailbox(b.contract, b.account, b.recipient, invalid)
    assert response.status_code == 400
    assert b.contract.ctx.store.mailboxes.get(b.recipient.pre) is None


def test_allocation_rejects_consent_signed_before_recipient_rotation(mailbox_bundle):
    b = mailbox_bundle
    previous = sign_binding(b.contract, b.account, b.recipient)
    b.recipient.rotate()
    assert post_cesr(b.contract, '/account', b.recipient.msgOwnEvent(sn=b.recipient.kever.sn)).status_code == 204
    denied = request_mailbox(b.contract, b.account, b.recipient, previous)
    assert denied.status_code == 401
    assert b.contract.ctx.store.mailboxes.get(b.recipient.pre) is None
    assert create(b.contract, b.account, b.recipient)['mailbox']['status'] == 'pending'


def test_allocation_requires_recipient_signing_threshold(mailbox_bundle):
    b = mailbox_bundle
    with habbing.openHby(name='mail-multikey', temp=True) as hby:
        recipient = hby.makeHab(name='recipient', icount=2, isith='2', ncount=2, nsith='2')
        register_aid(b.contract, '/account', recipient)
        binding = exchange(sender=recipient.pre, receiver=b.contract.ctx.host_hab.pre,
                           route='/mailbox/bind', attributes={'account_aid': b.account.pre,
                           'recipient_aid': recipient.pre}, version=Vrsn_2_0, kind=Kinds.json)
        kever = recipient.kever
        partial = messagize(serder=binding, tsgs=[(kever.prefixer, Number(sn=kever.lastEst.s),
                            Diger(qb64=kever.lastEst.d), recipient.sign(ser=binding.raw)[:1])],
                            framed=True, gvrsn=Vrsn_2_0).decode()
        denied = request_mailbox(b.contract, b.account, recipient, partial)
        assert denied.status_code == 401
        assert b.contract.ctx.store.mailboxes.get(recipient.pre) is None
        complete = recipient.endorse(binding, last=False, framed=True, gvrsn=Vrsn_2_0).decode()
        assert create(b.contract, b.account, recipient, complete)['mailbox']['status'] == 'pending'


def test_account_cleanup_waits_for_running_mailbox_allocation(mailbox_bundle):
    b = mailbox_bundle
    ctx = b.contract.ctx
    reply = create(b.contract, b.account, b.recipient)
    runner = BootOperationDoer(store=ctx.store, witness_boots=ctx.witness_boots,
                              watcher_boot=ctx.watcher_boot,
                              processor=BootOperationProcessor(ctx.exchanger.provisioner, b.provider))
    allocation = runner.processDueDo(tymth=lambda: 0.0)
    cleanup = ctx.exchanger.provisioner.deleteAccountMailboxesDo(
        b.account.pre, mailbox_boot=b.provider, tymth=lambda: 0.0)
    try:
        next(allocation)  # Provider request has started and is awaiting its result.
        assert b.provider.allocations == [b.recipient.pre]
        assert ctx.store.getBootOperation(reply['operation']['operation_id']).state == 'running'
        next(cleanup)
        assert ctx.store.mailboxes.get(b.recipient.pre).status == 'deleting'
        assert b.provider.deletions == []
        drain_do(allocation)
        assert b.recipient.pre in b.provider.registered
        assert ctx.store.mailboxes.get(b.recipient.pre).status == 'deleting'
        next(cleanup)  # Remote removal is now in flight; retain the cleanup reference.
        assert ctx.store.mailboxes.get(b.recipient.pre).status == 'deleting'
        with pytest.raises(RuntimeError):
            ctx.store.deleteAccount(b.account.pre)
        drain_do(cleanup)
        assert b.recipient.pre not in b.provider.registered
        assert b.provider.deletions == [b.recipient.pre]
        assert ctx.store.mailboxes.get(b.recipient.pre).status == 'deleted'
        ctx.store.deleteAccount(b.account.pre)
        assert ctx.store.getAccount(b.account.pre) is None
    finally:
        allocation.close()
        cleanup.close()
