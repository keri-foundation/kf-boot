# admitting.py
from keri import help
import falcon
from typing import Any 

from kfboot.basing import (
    SESSION_STATE_CANCELLED,
    SESSION_STATE_EXPIRED,
    SESSION_STATE_FAILED,
    SessionRecord,
)

logger = help.ogler.getLogger(__name__)


class Admitter:
    """
    Admission control for onboarding flows.

    Responsibilities:
    - Enforce IP-based onboarding quotas
    - Prevent duplicate or conflicting session starts
    - Validate that onboarding can proceed for the given request
    """

    def __init__(self, ctx, exchanger):
        self.ctx = ctx
        self.exchanger = exchanger

    def enforceSessionStartAdmission(
        self,
        *,
        sender: str,
        account_aid: str,
    ) -> None:
        client_ip = (self.exchanger.client_ip or "").strip()
        if not client_ip:
            return

        # Count both active sessions and closed sessions that still have cleanup debt.
        # Otherwise a user can let sessions expire and immediately rotate into fresh
        # onboarding slots while the old witness/watcher allocations are still live.
        admission_sessions = self.ctx.store.listAdmissionSessionsForIp(client_ip)
        active_accounts = {record.account_aid for record in admission_sessions if record.account_aid}
        active_ephemerals = {record.ephemeral_aid for record in admission_sessions if record.ephemeral_aid}

        account_limit = self.ctx.config.bootstrap_accounts_per_ip
        if account_limit > 0 and account_aid and account_aid not in active_accounts and len(active_accounts) >= account_limit:
            logger.warning(
                f"Account creation rejected due to per-IP onboarding account limit exceeded for client IP {client_ip}."
                f" Current limit is {account_limit} active onboarding accounts, and there are currently {len(active_accounts)} active accounts"
            )
            raise falcon.HTTPTooManyRequests(
                title="Per-IP onboarding account limit exceeded",
                description=(
                    f"Client IP {client_ip} already has {len(active_accounts)} active onboarding "
                    f"account session(s); the configured limit is {account_limit}."
                ),
            )

        aid_limit = self.ctx.config.bootstrap_aids_per_ip
        if aid_limit > 0 and sender and sender not in active_ephemerals and len(active_ephemerals) >= aid_limit:
            logger.warning(
                f"AID creation rejected due to per-IP onboarding principal limit exceeded for client IP {client_ip}."
                f" Current limit is {aid_limit} active onboarding principals, and there are currently {len(active_ephemerals)} active ephemeral AIDs"
            )
            raise falcon.HTTPTooManyRequests(
                title="Per-IP onboarding principal limit exceeded",
                description=(
                    f"Client IP {client_ip} already has {len(active_ephemerals)} active onboarding "
                    f"principal(s); the configured limit is {aid_limit}."
                ),
            )

    def reconcileExistingStartSession(
        self,
        *,
        session: SessionRecord,
        account_aid: str,
        account_alias: str,
        option: dict[str, Any],
        region_id: str,
        watcher_required: bool,
    ) -> SessionRecord:
        if session.state == SESSION_STATE_FAILED:
            logger.warning(f"Session start rejected: {session.failure_reason} {session.session_id}")
            raise falcon.HTTPConflict(
                title="Session failed",
                description=session.failure_reason or "Blind retry would duplicate hosted resources.",
            )
        if session.state in {SESSION_STATE_CANCELLED, SESSION_STATE_EXPIRED}:
            logger.warning(f"Session {session.session_id} was closed because no longer active")
            raise falcon.HTTPConflict(
                title="Session closed",
                description="The onboarding session is no longer active.",
            )
        if session.account_aid and session.account_aid != account_aid:
            logger.warning(
                f"Account AID mismatch for session {session.session_id}: has account AID {session.account_aid}"
                f" but request specified account AID {account_aid}",
            )
            raise falcon.HTTPConflict(
                title="Session parameter mismatch",
                description="The existing onboarding session was started with a different permanent account AID.",
            )
        if account_alias and session.account_alias and session.account_alias != account_alias:
            logger.warning(
                f"Account alias mismatch for session {session.session_id}: account alias {session.account_alias}"
                f" but request specified account alias {account_alias}"
            )
            raise falcon.HTTPConflict(
                title="Session parameter mismatch",
                description="The existing onboarding session was started with a different account alias.",
            )
        if session.chosen_profile_code and session.chosen_profile_code != option["code"]:
            logger.warning(
                f"Witness profile mismatch for session {session.session_id}: witness profile code {session.chosen_profile_code}"
                f" but request specified witness profile code {option['code']}",
            )
            raise falcon.HTTPConflict(
                title="Session parameter mismatch",
                description="The existing onboarding session uses a different witness profile.",
            )
        if session.region_id and session.region_id != region_id:
            logger.warning(
                f"Region mismatch for session {session.session_id}: session region {session.region_id} but request specified region {region_id}",
            )
            raise falcon.HTTPConflict(
                title="Session parameter mismatch",
                description="The existing onboarding session uses a different region.",
            )
        if session.watcher_required != watcher_required:
            logger.warning(
                f"Watcher requirement mismatch for session {session.session_id}: session watcher requirement {session.watcher_required}"
                f" but request specified watcher requirement {watcher_required}"
            )
            raise falcon.HTTPConflict(
                title="Session parameter mismatch",
                description="The existing onboarding session uses a different watcher requirement.",
            )
        return session
