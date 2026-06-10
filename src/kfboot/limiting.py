from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import falcon
from keri import help

from kfboot.basing import QuotaRecord
from kfboot.config import ACCOUNT_ROUTES, ONBOARDING_QUOTA_ROUTES, ONBOARDING_ROUTES
from kfboot.utils import extractExnPayload, optionalStr
from kfboot.store import nowIso

logger = help.ogler.getLogger(__name__)

ACCOUNT_REQUEST_SCOPE = "account_request"
ONBOARDING_REQUEST_SCOPE = "onboarding_request_ip"
ACCOUNT_DELETE_REQUEST_SCOPE = "account_delete_request"
ACCOUNT_DELETE_IP_REQUEST_SCOPE = "account_delete_request_ip"


class Limiter:
    def __init__(self, ctx):
        self.ctx = ctx

    def enforceOnboardingRequestQuota(self, *, route: str, client_ip: str) -> None:
        """Throttle onboarding business requests by client IP."""

        # Polling operation status is not onboarding business work.
        if route not in ONBOARDING_QUOTA_ROUTES:
            return

        # Get client IP
        client_ip = (client_ip or "").strip()
        if not client_ip:
            return

        # Get the requests limit from the config
        limit = self.ctx.config.bootstrap_api_requests_per_minute
        if limit <= 0:
            return

        # Get current time
        now = datetime.fromisoformat(nowIso())

        window = self._quotaRecord(ONBOARDING_REQUEST_SCOPE, client_ip, now=now)

        # Check for window reset 
        self._resetWindowIfElapsed(window, now=now)

        # If user exceeds the limit
        if window.count >= limit:
            self.ctx.store.saveQuota(window)
            retry_after = self._windowRetryAfterSeconds(window, now=now)

            logger.warning(
                f"Onboarding request per-IP rate limit exceeded for client IP {client_ip}."
                f" Limit is {limit} request(s) per minute."
            )
            raise falcon.HTTPTooManyRequests(
                title="Onboarding request rate limit exceeded",
                description=(
                    f"Client IP {client_ip} exceeded {limit} onboarding request(s) per minute. "
                    f"Retry after {retry_after} second(s)."
                ),
                retry_after=retry_after,
            )

        window.count += 1
        self.ctx.store.saveQuota(window)

    def enforceAccountQuotas(self, serder) -> None:
        """Apply account quota enforcement and record successful usage.

        This wrapper is convenient for direct callers and tests, but live route
        handlers should use the split precheck/record methods so the request
        that consumes the final API-budget slot can still complete before the
        account is marked past due.
        """

        self.precheckAccountQuotas(serder)
        self.recordSuccessfulAccountQuotaUse(serder)

    def precheckAccountQuotas(self, serder) -> None:
        """Apply only the pre-handler quota checks for onboarding/account requests."""

        # Apply quotas only to onboarding and account routes
        route = str(serder.ked.get("r", "") or "")
        if route not in ONBOARDING_ROUTES and route not in ACCOUNT_ROUTES:
            return

        # Check account context for the request
        payload = extractExnPayload(serder)
        account_aid, profile = self._accountContextForRoute(serder, payload)
        # TODO - we may want to enforce some limits even without account context
        if not account_aid or profile is None:
            logger.debug(
                f"No account context found for route {route}"
            )
            return

        # Enforce request rate and KEL budget for the account
        self._enforceAccountRequestRate(account_aid, profile)
        self._enforceAccountApiBudgetLimit(account_aid, profile)

    def recordSuccessfulAccountQuotaUse(self, serder) -> None:
        """Record API-budget usage after a business handler succeeds.

        Budget usage is recorded after the handler so the request that just
        reached the budget still succeeds. Otherwise verifyEvent() could mark
        the account expired and a second requireOnboardedAccount() inside the
        handler would reject the very same request mid-flight.
        """

        route = str(serder.ked.get("r", "") or "")
        if route not in ONBOARDING_ROUTES and route not in ACCOUNT_ROUTES:
            return

        payload = extractExnPayload(serder)
        account_aid, profile = self._accountContextForRoute(serder, payload)
        if not account_aid or profile is None:
            logger.debug(
                f"No account context found for route {route}"
            )
            return

        self._recordAccountApiBudgetUse(account_aid, profile)

    def enforceAccountDeleteQuota(self, *, sender: str, client_ip: str) -> None:
        """Throttle account deletion with the shared bootstrap API limit, but on delete-specific buckets."""

        # Get the limit from the config
        limit = self.ctx.config.bootstrap_api_requests_per_minute
        if limit <= 0:
            return

        # Get windows IP based and account based window
        now = datetime.fromisoformat(nowIso())
        windows: list[tuple[QuotaRecord, str, str]] = []

        normalized_sender = (sender or "").strip()
        if normalized_sender:
            windows.append(
                (
                    self._quotaRecord(ACCOUNT_DELETE_REQUEST_SCOPE, normalized_sender, now=now),
                    "sender AID",
                    normalized_sender,
                )
            )

        normalized_ip = (client_ip or "").strip()
        if normalized_ip:
            windows.append(
                (
                    self._quotaRecord(ACCOUNT_DELETE_IP_REQUEST_SCOPE, normalized_ip, now=now),
                    "client IP",
                    normalized_ip,
                )
            )

        # Return if no windows 
        if not windows:
            return

        # Use helper function to reset window if time window elapses
        for window, _, _ in windows:
            self._resetWindowIfElapsed(window, now=now)

        for window, subject_kind, subject in windows:
            # Check for limit
            if window.count >= limit:   
                self.ctx.store.saveQuota(window)
                logger.warning(
                    f"Account delete rate limit exceeded for {subject_kind} {subject}."
                    f" Limit is {limit} delete request(s) per minute."
                )
                raise falcon.HTTPTooManyRequests(
                    title="Account delete rate limit exceeded",
                    description=(
                        f"{subject_kind.capitalize()} {subject} exceeded {limit} account delete request(s) "
                        "in the rolling minute window. Retry later."
                    ),
                )

        for window, _, _ in windows:
            # Increase count and save it
            window.count += 1
            self.ctx.store.saveQuota(window)

    def _enforceAccountRequestRate(self, account_aid: str, profile: Any) -> None:
        """Enforce per-account request limits on onboarding and account routes."""
        
        # Get the current time and the request window for this account
        now = datetime.fromisoformat(nowIso())
        window = self._quotaRecord(ACCOUNT_REQUEST_SCOPE, account_aid, now=now)
        
        # Reset the window if more than 60 seconds have elapsed since the start
        self._resetWindowIfElapsed(window, now=now)

        # Check if the request count exceeds the profile limit for the window
        if window.count >= profile.max_requests_per_minute > 0:
            self.ctx.store.saveQuota(window)
            logger.warning(
                f"Account request per minute rate limit exceeded."
                f" User is limited to {profile.max_requests_per_minute} requests per minute under tier '{profile.tier}'"
            )
            raise falcon.HTTPTooManyRequests(
                title="Account request rate limit exceeded",
                description=(
                    f"Account {account_aid} exceeded {profile.max_requests_per_minute} requests in the rolling minute window. "
                    "Retry later or request a higher staging tier."
                ),
            )

        # If not exceeded, increment the count and log if approaching soft limits
        window.count += 1
        self.ctx.store.saveQuota(window)
        ratio = window.count / max(profile.max_requests_per_minute, 1)
        if ratio >= 0.95:
            logger.warning(f"Approaching request rate limit for account {account_aid}, current rate: 95%")
        elif ratio >= 0.85:
            logger.info(f"Approaching request rate limit for account {account_aid}, current rate: 85%")

    def _enforceAccountApiBudgetLimit(self, account_aid: str, profile: Any) -> None:
        """Reject requests when the account has already exhausted its API budget."""
        
        if profile.api_budget <= 0:
            return

        account = self.ctx.store.getAccount(account_aid)
        if account is None:
            logger.info(
                "Account does not exist yet, skipping enforcing KEL budget"
            )
            return

        count = account.api_used
         
        if count >= profile.api_budget:
            logger.warning(
                f"Account API budget exceeded for account {account_aid} under tier '{profile.tier}'",
            )
            raise falcon.HTTPTooManyRequests(
                title="Account key event budget exceeded",
                description=(
                    f"Account {account_aid} exceeded {profile.api_budget} API requests. "
                    "Request quota has been exhausted for this account tier."
                ),
            )

    def _recordAccountApiBudgetUse(self, account_aid: str, profile: Any) -> None:
        """Persist API-budget usage after a request has succeeded."""

        if profile.api_budget <= 0:
            return

        account = self.ctx.store.getAccount(account_aid)
        if account is None:
            logger.info(
                "Account does not exist yet, skipping API budget accounting"
            )
            return

        count = account.api_used
        if count >= profile.api_budget:
            logger.warning(
                f"Skipping API budget increment for account {account_aid} because the budget is already exhausted"
            )
            return

        count += 1
        account.api_used = count

        # Make sure an account gets marked for expiry as soon as it reaches its limit even if there is no expiry time
        if count >= profile.api_budget:
            account.expires_at = nowIso()
            logger.info(
                f"API budget limit reached for account {account_aid}. "
                "The account is now marked for expiry."
            )
        self.ctx.store.saveAccount(account)
        ratio = count / max(profile.api_budget, 1)
        if ratio >= 0.95:
            logger.warning(f"Approaching API budget limit for account {account_aid}, current rate: 95%")
        elif ratio >= 0.85:
            logger.info(f"Approaching API budget limit for account {account_aid}, current rate: 85%")

    def _accountContextForRoute(self, serder, payload: dict[str, Any]) -> tuple[str, Any]:
        """Resolve the account AID and tier profile for the current request."""
        route = str(serder.ked.get("r", "") or "")
        sender = serder.pre

        # For onboarding start, return the account AID and profile based on session context
        if route == "/onboarding/session/start":
            account_aid = optionalStr(payload, "account_aid")
            profile = self.ctx.config.account_profile(payload.get("chosen_profile_code", ""))
            return account_aid, profile

        # For other onboarding routes, resolve the account AID and profile from the session context
        session_id = optionalStr(payload, "session_id")
        if session_id:
            session = self.ctx.store.getSession(session_id)
            if session is not None:
                profile = self.ctx.config.account_profile(session.chosen_profile_code)
                account_aid = session.account_aid or optionalStr(payload, "account_aid")
                return account_aid, profile

        # For account routes, resolve the account AID and profile based on the authenticated sender
        if route in ACCOUNT_ROUTES:
            # Account routes are authenticated by the account AID sender.
            account_aid = sender
            account = self.ctx.store.getAccount(account_aid)
            profile = self.ctx.config.account_profile(account.witness_profile_code) if account is not None else None
            return account_aid, profile

        return "", None

    def _quotaRecord(self, scope: str, subject: str, *, now: datetime) -> QuotaRecord:
        """Get quota record from the store or create one if none"""
        record = self.ctx.store.getQuota(scope, subject)
        if record is not None:
            return record
        return QuotaRecord(
            scope=scope,
            subject=subject,
            window_start=now.isoformat(),
            count=0,
            blocked_until="",
        )

    def _resetWindowIfElapsed(self, window: QuotaRecord, *, now: datetime) -> None:
        window_start = _parseDt(window.window_start, default=now)
        elapsed = (now - window_start).total_seconds()
        if elapsed >= 60:
            window.window_start = now.isoformat()
            window.count = 0

    def _windowRetryAfterSeconds(self, window: QuotaRecord, *, now: datetime) -> int:
        window_start = _parseDt(window.window_start, default=now)
        # Use math ceiling to return the rounded up second
        retry_after = math.ceil(max(60 - (now - window_start).total_seconds(), 0))
        return max(retry_after, 1)


def _parseDt(value: str, *, default: datetime) -> datetime:
    """Parse the datetime value but returns a default if None"""
    if not value:
        return default
    return datetime.fromisoformat(value)
