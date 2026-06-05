from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import falcon
from keri import help
from keri.kering import Vrsn_1_0
from keri.peer.exchanging import Exchanger

from kfboot.basing import (
    ACCOUNT_STATE_FAILED,
    ACCOUNT_STATE_ONBOARDED,
    ACCOUNT_STATE_PENDING_ONBOARDING,
    ACCOUNT_STATE_EXPIRED,
    BOOT_OPERATION_ACCOUNT_DELETE,
    BOOT_OPERATION_FAILED,
    BOOT_OPERATION_PENDING,
    BOOT_OPERATION_RESOURCE_DELETE,
    BOOT_OPERATION_RUNNING,
    BOOT_OPERATION_SESSION_PROVISION,
    BOOT_OPERATION_WATCHER_STATUS_QUERY,
    AccountRecord,
    SESSION_STATE_ACCOUNT_CREATED,
    SESSION_STATE_CANCELLED,
    SESSION_STATE_COMPLETED,
    SESSION_STATE_EXPIRED,
    SESSION_STATE_FAILED,
    TERMINAL_SESSION_STATES,
    SessionRecord,
)
from kfboot.store import (
    accountFailed,
    nowIso,
    resourcesToApi,
)
from kfboot.limiting import Limiter
from kfboot.admitting import Admitter
from kfboot.provisioning import Provisioner
from kfboot.expiring import Expirer
from kfboot.utils import extractExnPayload, optionalStr, requiredStr

logger = help.ogler.getLogger(__name__)


@dataclass
class BootContext:
    config: Any
    store: Any
    witness_boots: Any
    watcher_boot: Any
    host_hab: Any
    habery: Any


class RouteHandlerError(RuntimeError):
    """Raised when route handler code fails unexpectedly."""


class RouteHandler:
    resource: str = ""

    def __init__(self, exchanger: "BootExchanger"):
        self.exchanger = exchanger

    def verify(self, serder, **kwa) -> bool:
        try:
            return self.verifyEvent(serder, **kwa)
        except falcon.HTTPError:
            raise
        except Exception as exc:
            logger.exception("Unhandled verifier error for route %s", self.resource)
            raise RouteHandlerError(f"Unhandled verifier error for {self.resource}") from exc

    def verifyEvent(self, serder, **kwa) -> bool:
        return True

    def handle(self, serder, **kwa):
        try:
            return self.handleEvent(serder, **kwa)
        except falcon.HTTPError:
            raise
        except Exception as exc:
            # Prevents internal error to be swallowed and turned into a misleading 401 Request rejected response
            # Unexpected handler failures now surfaces as a 500 Route handler failed
            logger.exception("Unhandled handler error for route %s", self.resource)
            raise RouteHandlerError(f"Unhandled handler error for {self.resource}") from exc

    def handleEvent(self, serder, **kwa):
        raise NotImplementedError


class SessionStartHandler(RouteHandler):
    resource = "/onboarding/session/start"

    def handleEvent(self, serder, **kwa):
        sender = serder.pre
        payload = extractExnPayload(serder)
        option = self.exchanger.accountOption(payload.get("chosen_profile_code", ""))
        account_aid = requiredStr(payload, "account_aid")
        alias = optionalStr(payload, "account_alias")
        region_id = optionalStr(payload, "region_id") or self.exchanger.ctx.config.region_id
        watcher_required = bool(
            payload.get("watcher_required", self.exchanger.ctx.config.bootstrap_watcher_required)
        )
        profile = self.exchanger.ctx.config.account_profile(option["code"])
        logger.info(
            f"Session start requested \n"
            f"Sender AID: {sender} \n"
            f"Account AID: {account_aid} \n"
            f"Account Alias: {alias} \n"
            f"Profile code: {option['code']}\n"
            f"Account tier: {getattr(profile, 'tier', '')}\n"
            f"Client IP: {self.exchanger.client_ip}\n"
        )
        if profile is None:
            logger.error(
                f"Profile not found for session start request for account AID {account_aid} from sender {sender}"
            )
            raise falcon.HTTPInternalServerError(
                title="Account profile missing",
                description="The selected witness profile has no configured account profile.",
            )
        if self.exchanger.ctx.config.bootstrap_watcher_required and not watcher_required:
            logger.warning(
                f"Session start rejected due to missing required watcher for account AID {account_aid}"
                f" from sender {sender}"
            )
            raise falcon.HTTPBadRequest(
                title="Watcher required",
                description="This boot service requires one hosted watcher per onboarded account.",
            )

        account = self.exchanger.ctx.store.getAccount(account_aid)
        if account is not None and account.status == ACCOUNT_STATE_ONBOARDED:
            logger.warning(
                f"Session start rejected because account {account_aid} is already onboarded"
            )
            raise falcon.HTTPConflict(
                title="Account already onboarded",
                description="The permanent account AID already completed onboarding.",
            )

        session = None
        existing = None
        newly_expired_session_ids: set[str] = set()
        session_for_account = self.exchanger.ctx.store.findSessionForAccount(account_aid)

        # First check if the account exists, is not in a terminal state BUT is expired
        # If so, mark it as expired to enter the cleanup process
        # This prevents session who are due for expiry from 'reviving' 
        if (
            session_for_account is not None
            and session_for_account.state not in TERMINAL_SESSION_STATES
            and self.exchanger.sessionPastDue(session_for_account)
        ):
            self.exchanger.expirer.markSessionExpired(session_for_account)
            newly_expired_session_ids.add(session_for_account.session_id)
            session_for_account = None

        if session_for_account is not None and session_for_account.state not in TERMINAL_SESSION_STATES:
            if session_for_account.ephemeral_aid != sender:
                logger.warning(
                    f"Session start rejected due to active session with different onboarding principal"
                    f" for account AID {account_aid} from sender {sender}"
                )
                raise falcon.HTTPConflict(
                    title="Account session already active",
                    description="A different onboarding principal already owns the active session for this account AID.",
                )
            session = session_for_account
        # Session is bound by an ephemeral sender 
        else:
            # Retrieve the existing session for that ephemeral
            existing = self.exchanger.ctx.store.findSessionForEphemeral(sender)
            
            # Check if it exists, is not in a terminal state, and is not expired
            if (
                existing is not None
                and existing.state not in TERMINAL_SESSION_STATES
                and self.exchanger.sessionPastDue(existing)
            ):
                # If so, mark it as expired for cleanup process
                self.exchanger.expirer.markSessionExpired(existing)
                newly_expired_session_ids.add(existing.session_id)
                existing = None
            
            # Check if it was expired earlier
            elif (
                existing is not None
                and existing.session_id in newly_expired_session_ids
            ):
                existing = None
            elif (
                existing is not None
                and existing.state in {
                    SESSION_STATE_EXPIRED,
                    SESSION_STATE_FAILED,
                    SESSION_STATE_CANCELLED,
                }
                and existing.resources_cleaned_at
            ):
                # Once a closed session has finished cleanup, it should no longer
                # shadow fresh starts for the same ephemeral principal.
                existing = None
        if existing is not None:
            session = existing

        if session is None:
            # Check if there is an outstanding session for that sender waiting for cleanup
            blocking = self.exchanger.findCleanupBlockingSession(
                sender=sender,
                account_aid=account_aid,
            )
            if blocking is not None:
                logger.warning(
                    f"Session start rejected because cleanup is still pending for closed session "
                    f"{blocking.session_id} on account AID {account_aid}"
                )
                raise falcon.HTTPConflict(
                    title="Session cleanup pending",
                    description=(
                        "The previous onboarding session is closed but its hosted resources are still "
                        "being reclaimed. Retry after cleanup completes."
                    ),
                )

        if session is not None:
            session = self.exchanger.admitter.reconcileExistingStartSession(
                session=session,
                account_aid=account_aid,
                account_alias=alias,
                option=option,
                region_id=region_id,
                watcher_required=watcher_required,
            )
            logger.info(
                f"Session start reconciled with existing session for account AID {account_aid}"
                f" from sender {sender}"
            )
        else:
            self.exchanger.admitter.enforceSessionStartAdmission(
                sender=sender,
                account_aid=account_aid,
                account_alias=alias,
                profile=self.exchanger.ctx.config.account_profile(option["code"]),
            )
            session = self.exchanger.ctx.store.createSession(
                ephemeral_aid=sender,
                account_aid=account_aid,
                account_alias=alias,
                chosen_profile_code=option["code"],
                client_ip=self.exchanger.client_ip,
                region_id=region_id,
                region_name=self.exchanger.ctx.config.region_name,
                watcher_required=watcher_required,
                witness_count=option["witness_count"],
                toad=option["toad"],
                account_tier=profile.tier,
            )
            logger.info(
                f"Session created for account {account_aid}",
            )

        self.exchanger.ensureSessionProvisionOperation(
            session=session,
            requester=sender,
            route=self.resource,
        )
        self.exchanger.expirer.refreshSessionLease(session)
        self.exchanger.replySession(self.resource, receiver=sender, session=session)


class SessionStatusHandler(RouteHandler):
    resource = "/onboarding/session/status"

    def handleEvent(self, serder, **kwa):
        sender = serder.pre
        session = self.exchanger.requireSession(requiredStr(extractExnPayload(serder), "session_id"))
        self.exchanger.requireOnboardingPrincipal(sender=sender, session=session)
        self.exchanger.requireOpenSession(session)
        self.exchanger.expirer.refreshSessionLease(session)
        logger.info(
            f"Session status requested for session {session.session_id}"
            f" from sender {sender}"
        )
        self.exchanger.replySession(self.resource, receiver=sender, session=session)


class AccountCreateHandler(RouteHandler):
    resource = "/onboarding/account/create"

    def handleEvent(self, serder, **kwa):
        sender = serder.pre
        payload = extractExnPayload(serder)
        session = self.exchanger.requireSession(requiredStr(payload, "session_id"))
        self.exchanger.requireOpenSession(session)
        self.exchanger.requireEphemeralPrincipal(sender=sender, session=session)

        account_aid = requiredStr(payload, "account_aid")
        logger.info(
            f"Account creation requested for account {account_aid} in session {session.session_id}",
        )

        if session.account_aid and session.account_aid != account_aid:
            logger.warning(
                f"Account creation rejected due to session already bound to account AID {session.account_aid}"
            )
            raise falcon.HTTPConflict(
                title="Session already bound",
                description="This onboarding session is already bound to a different account AID.",
            )

        self.exchanger.requireSessionProvisioned(session)

        if not session.witness_eids or (session.watcher_required and not session.watcher_eid):
            logger.error(
                f"Hosted resources missing during account creation for session {session.session_id}"
            )
            raise falcon.HTTPConflict(
                title="Resources missing",
                description="Witness or watcher allocation is incomplete for this session.",
            )

        account = self.exchanger.ctx.store.getAccount(account_aid)
        
        # Check account state before attempting to create or update account records
        if account is not None and account.status == ACCOUNT_STATE_EXPIRED:
            logger.warning(
                f"Account creation rejected due to account {account_aid} being expired",
            )
            raise falcon.HTTPConflict(
                title="Account not available",
                description="The permanent account AID is currently expired and cannot be reused for onboarding.",
            )
        if account is not None and account.session_id not in {"", session.session_id}:
            logger.warning(
                f"Account creation rejected due to account {account_aid} already bound to different session {account.session_id}"
            )
            raise falcon.HTTPConflict(
                title="Account already exists",
                description="The permanent account AID is already bound to a different onboarding session.",
            )

        if session.state in {SESSION_STATE_ACCOUNT_CREATED, SESSION_STATE_COMPLETED} and session.account_aid == account_aid:
            logger.info(
                f"Account creation request reconciled with existing account"
                f" for account {account_aid} in session {session.session_id}",
            )
            self.exchanger.replyAccount(self.resource, receiver=sender, session=session)
            return

        try:
            if account is None:
                account = self.exchanger.ctx.store.buildAccount(
                    account_aid=account_aid,
                    account_alias=optionalStr(payload, "account_alias") or session.account_alias,
                    witness_profile_code=session.chosen_profile_code,
                    witness_count=session.witness_count,
                    toad=session.toad,
                    watcher_required=session.watcher_required,
                    region_id=session.region_id,
                    region_name=session.region_name,
                    session_id=session.session_id,
                    witness_eids=list(session.witness_eids),
                    watcher_eid=session.watcher_eid,
                    tier=session.account_tier,
                    onboarded=False,
                )
            else:
                account.account_alias = optionalStr(payload, "account_alias") or account.account_alias
                account.status = ACCOUNT_STATE_PENDING_ONBOARDING
                account.witness_eids = list(session.witness_eids)
                account.watcher_eid = session.watcher_eid
                account.session_id = session.session_id
                account.onboarded_at = ""
                account.expired_at = ""
                account.resources_cleaned_at = ""
                account.expires_at = ""

            session.account_aid = account_aid
            session.state = SESSION_STATE_ACCOUNT_CREATED
            session.updated_at = nowIso()

            self.exchanger.ctx.store.saveAccount(account)
            self.exchanger.ctx.store.bindResourcesToAccount(session=session, account_aid=account_aid)
            self.exchanger.expirer.refreshSessionLease(session)
            logger.info(
                f"Account {account.status} for account AID {account_aid}",
            )
        except Exception as exc:
            logger.exception(
                f"Account creation failed for account AID {account_aid}",
            )
            self.exchanger.expirer.failSession(
                session=session,
                reason=str(exc),
                account=account,
                teardown=True,
            )
            raise

        self.exchanger.replyAccount(self.resource, receiver=sender, session=session)


class CompleteHandler(RouteHandler):
    resource = "/onboarding/complete"

    def handleEvent(self, serder, **kwa):
        sender = serder.pre
        payload = extractExnPayload(serder)
        session = self.exchanger.requireSession(requiredStr(payload, "session_id"))
        self.exchanger.requireOpenSession(session, allow_completed=True)
        self.exchanger.requireEphemeralPrincipal(sender=sender, session=session)

        account_aid = requiredStr(payload, "account_aid")
        logger.info(
            f"Onboarding completed for account AID {account_aid}",
        )
        if session.account_aid and session.account_aid != account_aid:
            logger.warning(
                "Onboarding rejected due to session already bound to a different account AID",
            )
            raise falcon.HTTPConflict(
                title="Session already bound",
                description="This onboarding session is already bound to a different account AID.",
            )

        account = self.exchanger.ctx.store.getAccount(account_aid)
        if account is None:
            logger.warning(
                f"Onboarding rejected due to account record not found for account AID {account_aid}"
            )
            raise falcon.HTTPNotFound(
                title="Account not found",
                description="No account record exists for the requested permanent account AID.",
            )

        if session.state == SESSION_STATE_COMPLETED and account.status == ACCOUNT_STATE_ONBOARDED:
            # Refresh the account lease on the account because it is active
            self.exchanger.expirer.refreshAccountLease(account)
            logger.info(
                f"Onboarding request reconciled with existing completed session and onboarded account {account_aid}"
            )
            self.exchanger.replyAccount(self.resource, receiver=sender, session=session)
            return

        if session.watcher_required and not session.watcher_eid:
            logger.error(
                f"Onboarding rejected due to missing hosted watcher for account AID {account_aid}"
            )
            self.exchanger.expirer.failSession(
                session=session,
                reason="Hosted watcher is required before onboarding can complete.",
                account=account,
                teardown=True,
            )
            raise falcon.HTTPConflict(
                title="Watcher missing",
                description="This boot service requires one hosted watcher before onboarding completes.",
            )

        session.state = SESSION_STATE_COMPLETED
        session.updated_at = nowIso()
        account.status = ACCOUNT_STATE_ONBOARDED
        account.onboarded_at = nowIso()
        self.exchanger.ctx.store.saveSession(session)
        self.exchanger.ctx.store.saveAccount(account)
        # Refresh the account lease to the onboarded time 
        self.exchanger.expirer.refreshAccountLease(account, now=account.onboarded_at)
        logger.info(
            f"Onboarding completed for account AID {account_aid}"
        )
        self.exchanger.replyAccount(self.resource, receiver=sender, session=session)


class CancelHandler(RouteHandler):
    resource = "/onboarding/cancel"

    def handleEvent(self, serder, **kwa):
        sender = serder.pre
        payload = extractExnPayload(serder)
        session = self.exchanger.requireSession(requiredStr(payload, "session_id"))
        self.exchanger.requireEphemeralPrincipal(sender=sender, session=session)
        logger.info(
            f"Session cancellation requested for session {session.session_id}"
        )
        if session.state == SESSION_STATE_COMPLETED:
            logger.warning(
                "Session cancellation rejected because session is already completed"
            )
            raise falcon.HTTPConflict(
                title="Session completed",
                description="A completed onboarding session cannot be cancelled.",
            )
        if session.state != SESSION_STATE_CANCELLED:
            account = self.exchanger.ctx.store.getAccount(session.account_aid) if session.account_aid else None
            session.state = SESSION_STATE_CANCELLED
            session.updated_at = nowIso()
            self.exchanger.ctx.store.saveSession(session)
            logger.info(
                f"Session cancelled for session {session.session_id}"
            )

            failed = accountFailed(account)
            if failed is not None:
                self.exchanger.ctx.store.saveAccount(failed)
                logger.info(
                    f"Account failed due to session cancellation for account AID {account.account_aid}"
                )

        self.exchanger.replySession(self.resource, receiver=sender, session=session)


class AccountWitnessesHandler(RouteHandler):
    resource = "/account/witnesses"

    def verifyEvent(self, serder, **kwa) -> bool:
        sender = serder.pre
        payload = extractExnPayload(serder)
        self.exchanger.requireOnboardedAccount(sender, payload)
        self.exchanger.limiter.precheckAccountQuotas(serder)
        return True

    def handleEvent(self, serder, **kwa):
        sender = serder.pre
        account = self.exchanger.requireOnboardedAccount(sender, extractExnPayload(serder))
        rows = resourcesToApi(
            self.exchanger.ctx.store.listResourcesForAccount(kind="witness", account_aid=sender)
        )
        account_delete = self.exchanger.activeAccountDeleteOperation(sender)
        self.exchanger.expirer.refreshAccountLease(account)
        self.exchanger.limiter.recordSuccessfulAccountQuotaUse(serder)
        logger.info(
            f"Query response for witnesses for account AID {sender}: {rows}"
        )
        payload = {"account_aid": sender, "witnesses": rows}
        if account_delete is not None:
            payload["account_delete_operation"] = self.exchanger.ctx.store.bootOperationPayload(account_delete)
        self.exchanger.queueReply(self.resource, sender, payload)


class AccountWatchersHandler(RouteHandler):
    resource = "/account/watchers"

    def verifyEvent(self, serder, **kwa) -> bool:
        sender = serder.pre
        payload = extractExnPayload(serder)
        self.exchanger.requireOnboardedAccount(sender, payload)
        self.exchanger.limiter.precheckAccountQuotas(serder)
        return True

    def handleEvent(self, serder, **kwa):
        sender = serder.pre
        account = self.exchanger.requireOnboardedAccount(sender, extractExnPayload(serder))
        rows = resourcesToApi(
            self.exchanger.ctx.store.listResourcesForAccount(kind="watcher", account_aid=sender)
        )
        account_delete = self.exchanger.activeAccountDeleteOperation(sender)
        self.exchanger.expirer.refreshAccountLease(account)
        self.exchanger.limiter.recordSuccessfulAccountQuotaUse(serder)
        logger.info(
            f"Query response for watchers for account AID {sender}: {rows}"
        )
        payload = {"account_aid": sender, "watchers": rows}
        if account_delete is not None:
            payload["account_delete_operation"] = self.exchanger.ctx.store.bootOperationPayload(account_delete)
        self.exchanger.queueReply(self.resource, sender, payload)


class AccountWatcherStatusHandler(RouteHandler):
    resource = "/account/watchers/status"

    def verifyEvent(self, serder, **kwa) -> bool:
        sender = serder.pre
        payload = extractExnPayload(serder)
        self.exchanger.requireOnboardedAccount(sender, payload)
        self.exchanger.limiter.precheckAccountQuotas(serder)
        return True

    def handleEvent(self, serder, **kwa):
        sender = serder.pre
        payload = extractExnPayload(serder)
        account = self.exchanger.requireOnboardedAccount(sender, payload)
        self.exchanger.requireNoAccountDeletePending(sender)

        watcher_id = optionalStr(payload, "watcher_eid") or requiredStr(payload, "watcher_id")
        logger.info(
            f"Query status for watcher {watcher_id} from {sender}"
        )
        record = self.exchanger.ctx.store.getResource("watcher", watcher_id)
        if record is None or record.principal != sender:
            logger.warning(
                f"Query for watcher status failed because watcher {watcher_id} was not found"
            )
            raise falcon.HTTPNotFound(title="Watcher not found")

        watcher = resourcesToApi([record])[0]
        delete_operation = self.exchanger.ctx.store.findActiveBootOperation(
            kind=BOOT_OPERATION_RESOURCE_DELETE,
            subject=f"watcher:{watcher_id}",
            requester=sender,
        )
        if delete_operation is not None:
            self.exchanger.expirer.refreshAccountLease(account)
            self.exchanger.limiter.recordSuccessfulAccountQuotaUse(serder)
            self.exchanger.queueReply(
                self.resource,
                sender,
                {
                    "account_aid": sender,
                    "watcher": watcher,
                    "watcher_id": watcher_id,
                    "operation": self.exchanger.ctx.store.bootOperationPayload(delete_operation),
                },
            )
            return

        operation = self.exchanger.ctx.store.ensureBootOperation(
            kind=BOOT_OPERATION_WATCHER_STATUS_QUERY,
            subject=f"watcher:{watcher_id}",
            requester=sender,
            route=self.resource,
            payload={"account_aid": sender, "watcher_id": watcher_id},
            due_at=nowIso(),
        )
        self.exchanger.expirer.refreshAccountLease(account)
        self.exchanger.limiter.recordSuccessfulAccountQuotaUse(serder)
        self.exchanger.queueReply(
            self.resource,
            sender,
            {
                "account_aid": sender,
                "watcher": watcher,
                "watcher_id": watcher_id,
                "operation": self.exchanger.ctx.store.bootOperationPayload(operation),
            },
        )


class AccountWitnessDeleteHandler(RouteHandler):
    resource = "/account/witnesses/delete"

    def verifyEvent(self, serder, **kwa) -> bool:
        sender = serder.pre
        payload = extractExnPayload(serder)
        self.exchanger.requireOnboardedAccount(sender, payload)
        self.exchanger.limiter.precheckAccountQuotas(serder)
        return True

    def handleEvent(self, serder, **kwa):
        sender = serder.pre
        payload = extractExnPayload(serder)
        account = self.exchanger.requireOnboardedAccount(sender, payload)
        self.exchanger.requireNoAccountDeletePending(sender)
        witness_id = optionalStr(payload, "witness_eid") or requiredStr(payload, "witness_id")
        logger.info(
            f"Account witness delete requested for witness {witness_id} from {sender}"
        )
        record = self.exchanger.ctx.store.getResource("witness", witness_id)
        if record is None or record.principal != sender:
            logger.warning(
                f"Account witness delete request failed because witness {witness_id} was not found"
            )
            raise falcon.HTTPNotFound(title="Witness not found")

        operation = self.exchanger.ctx.store.ensureBootOperation(
            kind=BOOT_OPERATION_RESOURCE_DELETE,
            subject=f"witness:{witness_id}",
            requester=sender,
            route=self.resource,
            payload={
                "account_aid": sender,
                "resource_kind": "witness",
                "resource_id": witness_id,
                "session_id": record.session_id,
            },
            due_at=nowIso(),
        )
        self.exchanger.expirer.refreshAccountLease(account)
        self.exchanger.limiter.recordSuccessfulAccountQuotaUse(serder)
        self.exchanger.queueReply(
            self.resource,
            sender,
            {
                "account_aid": sender,
                "witness_id": witness_id,
                "deleted": False,
                "operation": self.exchanger.ctx.store.bootOperationPayload(operation),
            },
        )


class AccountWatcherDeleteHandler(RouteHandler):
    resource = "/account/watchers/delete"

    def verifyEvent(self, serder, **kwa) -> bool:
        sender = serder.pre
        payload = extractExnPayload(serder)
        self.exchanger.requireOnboardedAccount(sender, payload)
        self.exchanger.limiter.precheckAccountQuotas(serder)
        return True

    def handleEvent(self, serder, **kwa):
        sender = serder.pre
        payload = extractExnPayload(serder)
        account = self.exchanger.requireOnboardedAccount(sender, payload)
        self.exchanger.requireNoAccountDeletePending(sender)
        watcher_id = optionalStr(payload, "watcher_eid") or requiredStr(payload, "watcher_id")
        logger.info(
            f"Account watcher delete requested for watcher {watcher_id} from {sender}"
        )
        record = self.exchanger.ctx.store.getResource("watcher", watcher_id)
        if record is None or record.principal != sender:
            logger.warning(
                f"Account watcher delete request failed because watcher {watcher_id} was not found"
            )
            raise falcon.HTTPNotFound(title="Watcher not found")

        operation = self.exchanger.ctx.store.ensureBootOperation(
            kind=BOOT_OPERATION_RESOURCE_DELETE,
            subject=f"watcher:{watcher_id}",
            requester=sender,
            route=self.resource,
            payload={
                "account_aid": sender,
                "resource_kind": "watcher",
                "resource_id": watcher_id,
                "session_id": record.session_id,
            },
            due_at=nowIso(),
        )
        self.exchanger.expirer.refreshAccountLease(account)
        self.exchanger.limiter.recordSuccessfulAccountQuotaUse(serder)
        self.exchanger.queueReply(
            self.resource,
            sender,
            {
                "account_aid": sender,
                "watcher_id": watcher_id,
                "deleted": False,
                "operation": self.exchanger.ctx.store.bootOperationPayload(operation),
            },
        )


class AccountDeleteHandler(RouteHandler):
    resource = "/account/delete"

    def verifyEvent(self, serder, **kwa) -> bool:
        sender = serder.pre
        payload = extractExnPayload(serder)
        self.exchanger.requireDeletableAccount(sender, payload)
        self.exchanger.limiter.enforceAccountDeleteQuota(
            sender=sender,
            client_ip=self.exchanger.client_ip,
        )
        return True

    def handleEvent(self, serder, **kwa):
        sender = serder.pre
        payload = extractExnPayload(serder)
        account_aid, account = self.exchanger.requireDeletableAccount(sender, payload)
        logger.info(
            f"Account delete requested for account AID {account_aid}"
        )

        if account is None:
            operations = self.exchanger.ctx.store.listBootOperations(
                kind=BOOT_OPERATION_ACCOUNT_DELETE,
                subject=f"account:{sender}",
                requester=sender,
            )
            latest = operations[0] if operations else None
            payload = {"account_aid": sender, "deleted": True}
            if latest is not None:
                payload["operation"] = self.exchanger.ctx.store.bootOperationPayload(latest)
            self.exchanger.queueReply(self.resource, sender, payload)
            return

        operation = self.exchanger.ctx.store.ensureBootOperation(
            kind=BOOT_OPERATION_ACCOUNT_DELETE,
            subject=f"account:{sender}",
            requester=sender,
            route=self.resource,
            payload={"account_aid": sender},
            due_at=nowIso(),
        )
        self.exchanger.queueReply(
            self.resource,
            sender,
            {
                "account_aid": sender,
                "deleted": False,
                "operation": self.exchanger.ctx.store.bootOperationPayload(operation),
            },
        )


class OperationStatusHandler(RouteHandler):
    resource = "/operations/status"

    def handleEvent(self, serder, **kwa):
        sender = serder.pre
        payload = extractExnPayload(serder)
        operation_id = requiredStr(payload, "operation_id")
        operation = self.exchanger.ctx.store.getBootOperation(operation_id)
        if operation is None:
            logger.warning(
                f"Operation status request failed because operation {operation_id} was not found"
            )
            raise falcon.HTTPNotFound(
                title="Operation not found",
                description=f"No boot operation exists for '{operation_id}'.",
            )

        operation_payload = operation.payload if isinstance(operation.payload, dict) else {}
        allowed = bool(sender and sender == operation.requester)
        if not allowed and operation.kind == BOOT_OPERATION_SESSION_PROVISION:
            session_id = str(operation_payload.get("session_id") or operation.subject)
            session = self.exchanger.ctx.store.getSession(session_id)
            allowed = session is not None and sender in {session.ephemeral_aid, session.account_aid}
        elif not allowed and operation.kind == BOOT_OPERATION_WATCHER_STATUS_QUERY:
            watcher_id = str(operation_payload.get("watcher_id") or "")
            resource = self.exchanger.ctx.store.getResource("watcher", watcher_id)
            allowed = resource is not None and sender in {resource.principal, resource.cid}
        elif not allowed and operation.kind == BOOT_OPERATION_RESOURCE_DELETE:
            resource_kind = str(operation_payload.get("resource_kind") or "")
            resource_id = str(operation_payload.get("resource_id") or "")
            resource = self.exchanger.ctx.store.getResource(resource_kind, resource_id)
            allowed = resource is not None and sender in {resource.principal, resource.cid}
        elif not allowed and operation.kind == BOOT_OPERATION_ACCOUNT_DELETE:
            allowed = sender == str(operation_payload.get("account_aid") or "")
        elif not allowed:
            allowed = operation.subject == sender

        if not allowed:
            logger.warning(
                f"Operation status request rejected for sender {sender} on operation {operation.operation_id}"
            )
            raise falcon.HTTPUnauthorized(
                title="Wrong operation principal",
                description="The authenticated sender is not allowed to read this operation.",
            )

        self.exchanger.queueReply(
            self.resource,
            sender,
            {"operation": self.exchanger.ctx.store.bootOperationPayload(operation)},
        )


class BootExchanger(Exchanger):
    def __init__(self, ctx: BootContext):
        super().__init__(hby=ctx.habery, handlers=[])
        self.ctx = ctx
        self.host_hab = ctx.host_hab
        self.reply_streams: list[bytes] = []
        self.client_ip = ""
        self.last_error: falcon.HTTPError | None = None
        self.limiter = Limiter(ctx)
        self.admitter = Admitter(ctx, self)
        self.provisioner = Provisioner(ctx, self)
        self.expirer = Expirer(ctx, self.provisioner)

        handlers = (
            SessionStartHandler(self),
            SessionStatusHandler(self),
            AccountCreateHandler(self),
            CompleteHandler(self),
            CancelHandler(self),
            AccountWitnessesHandler(self),
            AccountWatchersHandler(self),
            AccountWatcherStatusHandler(self),
            OperationStatusHandler(self),
            AccountDeleteHandler(self),
            AccountWitnessDeleteHandler(self),
            AccountWatcherDeleteHandler(self),
        )
        for handler in handlers:
            self.addHandler(handler)
        logger.info(
            f"Exchanger initialized \n"
            f"Handlers: {[type(h).__name__ for h in handlers]}\n"
            f"Witness Count: {len(ctx.witness_boots)}\n"
            f"Watcher Boot URL: {getattr(ctx.watcher_boot, "base_url", "")}",
        )

    def clearReplies(self) -> None:
        self.reply_streams.clear()
        self.last_error = None

    def setClientIp(self, client_ip: Any) -> None:
        if isinstance(client_ip, tuple):
            client_ip = client_ip[0] if client_ip else ""
        self.client_ip = str(client_ip or "").strip()

    

    def takeReply(self) -> bytes | None:
        if not self.reply_streams:
            return None
        return self.reply_streams.pop(0)

    def accountOption(self, code: str) -> dict[str, Any]:
        option = self.ctx.config.account_option(code or "")
        if option is None:
            logger.warning(
                f"Account option is unsupported for code '{code or ''}'"
            )
            raise falcon.HTTPBadRequest(
                title="Unsupported witness profile",
                description=f"Unknown account profile '{code or ''}'.",
            )
        return option

    def requireSession(self, session_id: str) -> SessionRecord:
        session = self.ctx.store.getSession(session_id)
        if session is None:
            logger.warning(
                f"Session not found for session ID {session_id}"
            )
            raise falcon.HTTPNotFound(
                title="Session not found",
                description=f"No onboarding session exists for '{session_id}'.",
            )
        return session

    def requireOnboardingPrincipal(self, *, sender: str, session: SessionRecord) -> None:
        if sender in {session.ephemeral_aid, session.account_aid}:
            return
        logger.warning(
            f"Session principal mismatch between sender {sender} and session principals {session.ephemeral_aid}, {session.account_aid}"
        )
        raise falcon.HTTPUnauthorized(
            title="Wrong principal",
            description="The authenticated sender does not match the onboarding session principal.",
        )

    def requireEphemeralPrincipal(self, *, sender: str, session: SessionRecord) -> None:
        if sender and sender == session.ephemeral_aid:
            return
        logger.warning(
            f"Session ephemeral principal mismatch for session {session.session_id}"
            f" between sender {sender} and session ephemeral principal {session.ephemeral_aid}"
        )
        raise falcon.HTTPUnauthorized(
            title="Wrong onboarding principal",
            description="The authenticated sender must be the session's hidden onboarding AID.",
        )

    def requireAccountPrincipal(self, *, sender: str, session: SessionRecord) -> None:
        if sender and sender == session.account_aid:
            return
        logger.warning(
            "Session account principal mismatch, the authenticated sender does not match the session's account AID"
        )
        raise falcon.HTTPUnauthorized(
            title="Wrong account principal",
            description="The authenticated sender must be the permanent account AID.",
        )

    def requireOpenSession(self, session: SessionRecord, *, allow_completed: bool = False) -> None:
        """
        Ensure the session is in an open, non-terminal lifecycle state.

        Responsibilities:
        - Reject sessions that have reached a terminal state
        (FAILED, CANCELLED, EXPIRED, COMPLETED unless explicitly allowed).
        - Reject sessions that are missing required lifecycle fields.
        - Provide a consistent guardrail for all onboarding routes that require
        an active session.

        Lifecycle rules enforced:
        - If session.state is in TERMINAL_SESSION_STATES = reject.
        - If session.state == COMPLETED and allow_completed is False = reject.
        - Otherwise, the session is considered open and usable.

        Raises:
        - falcon.HTTPConflict if the session is closed or completed when not allowed.
        - falcon.HTTPUnauthorized if the session is missing or invalid.
        """

        # Check if a session is not in a terminal state but its expiration date is due, expire it now then rejects
        if session.state not in TERMINAL_SESSION_STATES and self.sessionPastDue(session):
            self.expirer.markSessionExpired(session)
            logger.warning(f"Session {session.session_id} expired")
            raise falcon.HTTPGone(title="Session expired")
        if session.state == SESSION_STATE_EXPIRED:
            logger.warning(f"Session {session.session_id} expired")
            raise falcon.HTTPGone(title="Session expired")
        if session.state == SESSION_STATE_FAILED:
            logger.warning(f"Session {session.session_id} failed")
            raise falcon.HTTPConflict(
                title="Session failed",
                description=session.failure_reason or "The onboarding session is in a failed state.",
            )
        if session.state == SESSION_STATE_CANCELLED:
            logger.warning(f"Session {session.session_id} cancelled")
            raise falcon.HTTPConflict(title="Session cancelled")
        if session.state == SESSION_STATE_COMPLETED and not allow_completed:
            logger.warning(f"Session {session.session_id} completed")
            raise falcon.HTTPConflict(title="Session completed")

    def requireOnboardedAccount(self, sender: str, payload: dict[str, Any]) -> AccountRecord:
        """
        Ensure the authenticated sender corresponds to a valid, non‑expired,
        fully onboarded account.

        Responsibilities:
        - Enforce that the authenticated sender is the principal for the
        requested account (account_aid must match sender when provided).
        - Retrieve the account record for the sender and validate that it exists.
        - Reject accounts that are expired or past due, performing a dynamic
        expiration transition when necessary.
        - Reject accounts that have not completed onboarding.
        - Provide a consistent guardrail for all routes that require an
        onboarded account principal.

        Lifecycle rules enforced:
        - If account_aid is provided and does not match sender => reject (401).
        - If no account exists for sender => reject (404).
        - If account.status == EXPIRED => reject (409).
        - If account is past due (TTL exceeded), mark it expired and reject (409).
        - If account.status != ONBOARDED => reject (409).

        Returns:
        - The validated AccountRecord for the authenticated sender.

        Raises:
        - falcon.HTTPUnauthorized for principal mismatch.
        - falcon.HTTPNotFound if the account does not exist.
        - falcon.HTTPConflict for expired, past‑due, or non‑onboarded accounts.
        """
        account_aid = optionalStr(payload, "account_aid")
        if account_aid and account_aid != sender:
            logger.warning(
                f"Account principal mismatch, authenticated sender {sender} does not match"
                f" the requested account AID {account_aid}"
            )
            raise falcon.HTTPUnauthorized(
                title="Account principal mismatch",
                description="The authenticated sender must match account_aid.",
            )
        account = self.ctx.store.getAccount(sender)
        if account is None:
            logger.warning(
                f"Account not found for authenticated sender {sender}"
            )
            raise falcon.HTTPNotFound(
                title="Account not found",
                description="No account exists for the authenticated sender.",
            )
        if account.status == ACCOUNT_STATE_EXPIRED:
            logger.warning(f"Account {account.account_aid} has expired and cannot access account routes")
            raise falcon.HTTPConflict(
                title="Account expired",
                description="This account has expired and must be renewed or deleted before accessing account routes.",
            )
        if self.accountPastDue(account):
            self.expirer.markAccountExpired(account)
            logger.warning(
                f"Account {account.account_aid} is past due and cannot access account routes"
            )
            raise falcon.HTTPConflict(
                title="Account expired",
                description="This account has expired and must be renewed or deleted before accessing account routes.",
            )
        if account.status != ACCOUNT_STATE_ONBOARDED:
            logger.warning(f"Account {account.account_aid} is not onboarded and cannot access approved account routes")
            raise falcon.HTTPConflict(
                title="Account not onboarded",
                description="Approved-account routes require an onboarded account principal.",
            )
        return account

    def sessionPastDue(self, session: SessionRecord) -> bool:
        """Check if a session's expiration is due"""

        # Get expiration date, return if None
        if not session.expires_at:
            return False

        try:
            expires_at = datetime.fromisoformat(session.expires_at)
        except ValueError:
            logger.warning(
                f"Session {session.session_id} has invalid expires_at format: {session.expires_at}",
            )
            # Fail closed so corrupted lifecycle metadata cannot keep a session open.
            return True

        return expires_at <= datetime.fromisoformat(nowIso())

    def ensureSessionProvisionOperation(
        self,
        *,
        session: SessionRecord,
        requester: str,
        route: str,
    ):
        latest = self._latestSessionProvisionOperation(session)
        if session.witness_eids and (not session.watcher_required or session.watcher_eid):
            return latest

        return self.ctx.store.ensureBootOperation(
            kind=BOOT_OPERATION_SESSION_PROVISION,
            subject=session.session_id,
            requester=requester,
            route=route,
            payload={
                "session_id": session.session_id,
                "account_aid": session.account_aid,
                "witness_count": session.witness_count,
                "watcher_required": session.watcher_required,
            },
            due_at=nowIso(),
        )

    def requireSessionProvisioned(self, session: SessionRecord) -> None:
        operation = self._latestSessionProvisionOperation(session)
        if operation is not None and operation.state in {
            BOOT_OPERATION_PENDING,
            BOOT_OPERATION_RUNNING,
        }:
            logger.warning(
                f"Account creation rejected because session {session.session_id} provisioning is {operation.state}"
            )
            raise falcon.HTTPConflict(
                title="Session provisioning pending",
                description="Hosted resource provisioning is still in progress for this session.",
            )

        if operation is not None and operation.state == BOOT_OPERATION_FAILED:
            logger.warning(
                f"Account creation rejected because session {session.session_id} provisioning failed"
            )
            raise falcon.HTTPConflict(
                title="Session provisioning failed",
                description=operation.last_error or "Hosted resource provisioning failed for this session.",
            )

    def activeAccountDeleteOperation(self, account_aid: str):
        return self.ctx.store.findActiveBootOperation(
            kind=BOOT_OPERATION_ACCOUNT_DELETE,
            subject=f"account:{account_aid}",
            requester=account_aid,
        )

    def requireNoAccountDeletePending(self, account_aid: str) -> None:
        operation = self.activeAccountDeleteOperation(account_aid)
        if operation is None:
            return
        logger.warning(
            f"Account route rejected because account {account_aid} deletion is {operation.state}"
        )
        raise falcon.HTTPConflict(
            title="Account deletion pending",
            description="Account deletion is already pending for this account.",
        )

    def requireDeletableAccount(self, sender: str, payload: dict[str, Any]) -> tuple[str, AccountRecord | None]:
        """Checks the identity of the sender and if the account is in a valid state for deletion"""
        account_aid = optionalStr(payload, "account_aid") 
        if account_aid and account_aid != sender:
            logger.warning(
                f"Account delete request rejected, authenticated sender {sender} does not match requested account AID {account_aid}"
            )
            raise falcon.HTTPUnauthorized(
                title="Account principal mismatch",
                description="The authenticated sender must match account_aid.",
            )

        account = self.ctx.store.getAccount(sender)

        # Accounts that are onboarded, expired and failed are valid for deletion
        if account is not None and account.status not in {
            ACCOUNT_STATE_ONBOARDED,
            ACCOUNT_STATE_EXPIRED,
            ACCOUNT_STATE_FAILED,
        }:
            logger.warning(
                f"Account delete request rejected due to account {account_aid} being in invalid state: {account.status}"
            )
            raise falcon.HTTPConflict(
                title="Account not onboarded",
                description="Approved-account routes require an onboarded account principal.",
            )

        return account_aid, account

    def findCleanupBlockingSession(self, *, sender: str, account_aid: str) -> SessionRecord | None:
        """Return a closed session that still owns cleanup debt for this start request.

        We check the latest account-bound and ephemeral-bound sessions because those are
        the records most likely to be revived by repeated start attempts. A closed session
        without `resources_cleaned_at` still represents live hosted resources that should
        be reclaimed before issuing a fresh allocation.
        """
        # Create a seen set
        seen: set[str] = set()
        
        # Iterate through session for that account aid/ephemeral
        for candidate in (
            self.ctx.store.findSessionForAccount(account_aid),
            self.ctx.store.findSessionForEphemeral(sender),
        ):
            if candidate is None or candidate.session_id in seen:
                continue
            seen.add(candidate.session_id)

            # Check if it is in a terminal state
            if candidate.state not in {
                SESSION_STATE_EXPIRED,
                SESSION_STATE_FAILED,
                SESSION_STATE_CANCELLED,
            }:
                continue

            # Check if it was cleaned
            if candidate.resources_cleaned_at:
                continue
            return candidate
        return None

    def accountPastDue(self, account: AccountRecord) -> bool:
        """Checks expiration of an account"""
        # Return if expire date is not set
        if not account.expires_at:
            return False

        try:
            expires_at = datetime.fromisoformat(account.expires_at)
        except ValueError:
            logger.warning(
                f"Account {account.account_aid} has invalid expires_at format: {account.expires_at}",
            )
            # Fail closed so corrupted lifecycle metadata cannot keep an account active.
            return True

        return expires_at <= datetime.fromisoformat(nowIso())

    def replySession(self, route: str, *, receiver: str, session: SessionRecord) -> None:
        payload = self.sessionPayload(session)
        self.queueReply(route, receiver, payload)

    def replyAccount(self, route: str, *, receiver: str, session: SessionRecord) -> None:
        payload = self.sessionPayload(session)
        if session.account_aid:
            account = self.ctx.store.getAccount(session.account_aid)
            if account is not None:
                payload["account"] = self.ctx.store.accountPayload(account)
        self.queueReply(route, receiver, payload)

    def sessionPayload(self, session: SessionRecord) -> dict[str, Any]:
        witnesses = resourcesToApi(
            self.ctx.store.getResources("witness", session.witness_eids),
            include_boot_url=True,
        )
        watcher = None
        if session.watcher_eid:
            rows = resourcesToApi(
                self.ctx.store.getResources("watcher", [session.watcher_eid]),
                include_boot_url=True,
            )
            watcher = rows[0] if rows else None

        payload = self.ctx.store.sessionPayload(session)
        payload["session"] = dict(payload)
        payload["witnesses"] = witnesses
        payload["watcher"] = watcher
        payload["witness_count"] = session.witness_count or len(witnesses)
        payload["toad"] = session.toad
        payload["region_id"] = session.region_id
        payload["region_name"] = session.region_name
        if session.account_aid:
            payload["account_aid"] = session.account_aid
        operation = self._latestSessionProvisionOperation(session)
        if operation is not None:
            payload["session_provision_operation"] = self.ctx.store.bootOperationPayload(operation)
        return payload

    def _latestSessionProvisionOperation(self, session: SessionRecord):
        operations = self.ctx.store.listBootOperations(
            kind=BOOT_OPERATION_SESSION_PROVISION,
            subject=session.session_id,
        )
        return operations[0] if operations else None

    def queueReply(self, route: str, receiver: str, payload: dict[str, Any]) -> None:
        stream = bytearray(self.host_hab.replay())
        stream.extend(
            self.host_hab.exchange(
                route=route,
                attributes=payload,
                receiver=receiver or "",
                gvrsn=Vrsn_1_0,
            )
        )
        self.reply_streams.append(bytes(stream))
        logger.debug(
            f"Reply queued for route {route} to receiver {receiver}",
        )

    def processEvent(self, serder, tsgs=None, cigars=None, ptds=None, essrs=None, **kwa):
        try:
            route = str(serder.ked.get("r", "") or "")
            # Enforce per-IP onboarding throttles before route-specific verification.
            self.limiter.enforceOnboardingRequestQuota(route=route, client_ip=self.client_ip)
            return super().processEvent(serder, tsgs=tsgs, cigars=cigars, ptds=ptds, essrs=essrs, **kwa)
        except falcon.HTTPError as exc:
            self.last_error = exc
            logger.warning(
                f"Exchange event processing failed with HTTP error: {exc}"
            )
            return None
        except RouteHandlerError as exc:
            self.last_error = falcon.HTTPInternalServerError(
                title="Route handler failed",
                description="The boot service could not process this route.",
            )
            logger.exception("Exchange route handler failed: %s", exc)
            return None
