# expiring.py

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta

from keri import help

from kfboot.basing import (
    ACCOUNT_STATE_EXPIRED,
    ACCOUNT_STATE_FAILED,
    ACCOUNT_STATE_ONBOARDED,
    CLEANUP_TASK_ACCOUNT_CLEANUP,
    CLEANUP_TASK_ACCOUNT_DELETE,
    CLEANUP_TASK_ACCOUNT_EXPIRE,
    CLEANUP_TASK_SESSION_CLEANUP,
    CLEANUP_TASK_SESSION_DELETE,
    CLEANUP_TASK_SESSION_EXPIRE,
    SESSION_STATE_CANCELLED,
    SESSION_STATE_COMPLETED,
    SESSION_STATE_EXPIRED,
    SESSION_STATE_FAILED,
    TERMINAL_SESSION_STATES,
    CleanupTaskRecord,
    SessionRecord,
)
from kfboot.boot_client import BootError
from kfboot.store import accountFailed, nowIso, sessionFailed

logger = help.ogler.getLogger(__name__)


class Expirer:
    """
    Coordinate all session and account expiration, cleanup, and deletion
    workflows using the durable cleanup‑task queue.

    Purpose:
    - Act as the lifecycle controller for sessions and accounts whose TTLs,
      cleanup requirements, or retention windows have elapsed.
    - Provide one durable-queue execution path for all expiration-related
      state transitions.
    - Drive the durable cleanup‑task queue by claiming, processing, rescheduling,
      and completing tasks in due‑time order.

    Core responsibilities:
    - Expire sessions and accounts when their stored expiry timestamps are due.
    - Tear down hosted resources for expired sessions and accounts.
    - Delete expired accounts after cleanup and retention windows.
    - Apply exponential‑backoff retry scheduling for cleanup failures.
    - Quarantine cleanup tasks that no longer seem to resolve.
    - Provide helper APIs for session failure, TTL refresh, and dynamic
      expiration transitions.

    Cleanup‑task lifecycle enforced:
    - `session_expire` => mark session expired.
    - `session_cleanup` => teardown session resources => complete.
    - `session_delete` => delete closed session after retention => complete.

    - `account_expire` => mark account expired.
    - `account_cleanup` => teardown closed-account resources => complete.
    - `account_delete` => delete closed, cleaned account => complete.

    High‑level workflow:
    - sweepDo(): drain due tasks until batch/time budget is exhausted.
    - _claimDueTask(): mark one due task in progress.
    - _processClaimedTaskDo(): dispatch to the appropriate handler.
    - _process*(): perform state transitions, teardown, or deletion.
    - _rescheduleTask(): defer failed tasks with exponential backoff.
    - _completeTask(): remove finished tasks from the durable queue.
    """
    # Just for readability
    SESSION_EXPIRE_TASK = CLEANUP_TASK_SESSION_EXPIRE
    SESSION_CLEANUP_TASK = CLEANUP_TASK_SESSION_CLEANUP
    SESSION_DELETE_TASK = CLEANUP_TASK_SESSION_DELETE
    ACCOUNT_EXPIRE_TASK = CLEANUP_TASK_ACCOUNT_EXPIRE
    ACCOUNT_CLEANUP_TASK = CLEANUP_TASK_ACCOUNT_CLEANUP
    ACCOUNT_DELETE_TASK = CLEANUP_TASK_ACCOUNT_DELETE

    def __init__(self, ctx, provisioner):
        """
        Initialize the Expirer, the coordinator responsible for all session and
        account expiration, cleanup, and deletion workflows.

        Responsibilities:
        - Store references to the application context and the Provisioner used
        for tearing down hosted resources.

        Attributes:
        - ctx: The BootContext providing configuration, store access, and
        environment dependencies.
        - provisioner: The Provisioner responsible for resource teardown during
        session/account cleanup.
        """
        self.ctx = ctx
        self.provisioner = provisioner

    def sweepDo(
        self,
        *,
        batch_size: int | None = None,
        time_budget_seconds: float | None = None,
        now: str | None = None,
        tymth,
        tock: float = 0.0,
    ):
        """Process due cleanup tasks cooperatively under the HIO runtime."""

        # Get batch size limit if provided, if None fallback to config
        limit = batch_size if batch_size is not None else self.ctx.config.cleanup_batch_size

        # Get budget limit if provided, if None fallback to config
        budget = (
            time_budget_seconds
            if time_budget_seconds is not None
            else self.ctx.config.cleanup_time_budget_seconds
        )

        # Create result object for logging work done
        results = {
            "sessions_expired": 0,
            "sessions_cleaned": 0,
            "sessions_deleted": 0,
            "accounts_expired": 0,
            "accounts_cleaned": 0,
            "accounts_deleted": 0,
        }

        # Return if limit or budget is 0
        if limit <= 0 or budget <= 0:
            return results

        # Set up timing and task counter
        started_at = time.monotonic()
        claimed = 0
        while claimed < limit:
            # Stop after this cooperative pass spends its configured time budget.
            if time.monotonic() - started_at >= budget:
                break

            # Get current time to set task start time
            current = now or nowIso()

            # Claim a due task
            task = self._claimDueTask(now=current)
            if task is None:
                # If no due tasks are found, break
                break

            # Increment task claimed number
            claimed += 1

            # Process task cooperatively
            category, _value = yield from self._processClaimedTaskDo(
                task,
                now=current,
                tymth=tymth,
                tock=tock,
            )
            if category is not None:
                # Increment the work done based on the category
                results[category] += 1

            # Yield to the root HIO scheduler between claimed tasks.
            yield tock

        return results

    def markSessionExpired(self, session: SessionRecord, *, now: str | None = None) -> SessionRecord:
        """Persist a session transition into the expired state."""

        # Determine the timestamp to use for the expiration event if provided else use current time
        current = now or nowIso()

        # Set the session as expired
        session.state = SESSION_STATE_EXPIRED
        session.updated_at = current

        # Set expiration date if none
        if not session.expired_at:
            session.expired_at = current

        # Save the record to DB
        self.ctx.store.saveSession(session)

        # Return the updated session
        return session

    def markAccountExpired(self, account, *, now: str | None = None):
        """Persist an account transition into the expired state"""

        # Use time provided if not use current time
        current = now or nowIso()

        # Set the account as expired
        account.status = ACCOUNT_STATE_EXPIRED

        # Update the account expired_at to current if none
        if not account.expired_at:
            account.expired_at = current

        # Save account to DB
        self.ctx.store.saveAccount(account)

        # Return updated account
        return account

    def failSession(
        self,
        *,
        session: SessionRecord,
        reason: str,
        account=None,
        teardown: bool = False,
    ) -> None:
        """Mark a session failed and optionally tear down any staged resources"""
        sessionFailed(session, reason)
        self.ctx.store.saveSession(session)
        logger.warning(
            f"Session {session.session_id} failed: {reason}",
        )

        if account is None:
            failed = None
        elif account.status == ACCOUNT_STATE_ONBOARDED:
            failed = None
        else:
            failed = accountFailed(account)

        if failed is not None:
            self.ctx.store.saveAccount(failed)
            logger.warning(
                f"Account failed due to session failure for account AID {failed.account_aid}",
            )

        if teardown:
            logger.info(
                f"Session resource teardown initiated due to session failure for session {session.session_id}"
            )
            try:
                self.provisioner.teardownSessionResources(session=session, account=account)
            except BootError as exc:
                session.failure_reason = f"{reason} Cleanup failed: {exc}"
                session.updated_at = nowIso()
                self.ctx.store.saveSession(session)
                logger.warning(
                    f"Session resource teardown failed for {session.session_id}: {exc}",
                )
            # Teardown succeeds
            else:
                cleaned_at = nowIso()
                session.resources_cleaned_at = cleaned_at
                session.updated_at = cleaned_at
                self.ctx.store.saveSession(session)

                if failed is not None:
                    failed.resources_cleaned_at = cleaned_at
                    failed.session_id = ""
                    self.ctx.store.saveAccount(failed)

    def refreshSessionLease(self, session: SessionRecord) -> None:
        """Extend the TTL for a still-active session"""
        self.ctx.store.refreshSessionLease(session)
        logger.debug(f"Session lease refreshed for session {session.session_id}")

    def refreshAccountLease(self, account, *, now: str | None = None) -> None:
        """Extend the idle TTL for a still-active onboarded account."""
        self.ctx.store.refreshAccountLease(account, now=now)
        logger.debug(f"Account lease refreshed for account {account.account_aid}")

    def recoverClaimedCleanupTasks(self, *, now: str | None = None) -> int:
        """Requeue any previously claimed tasks so a restarted runner can resume work.

        In the supported deployment model there is a single runner, so any task
        that still carries claim metadata at startup is assumed to be orphaned by
        a prior crash or unclean shutdown and is made immediately visible again.
        """
        recovered = self.ctx.store.requeueClaimedCleanupTasks(now=now or nowIso())
        if recovered:
            logger.info(
                f"Recovered {recovered} claimed cleanup task(s) during runner startup"
            )
        return recovered

    def _claimDueTask(
        self,
        *,
        now: str,
        kind: str | None = None,
    ) -> CleanupTaskRecord | None:
        """Mark one due task as in progress in the durable queue."""
        return self.ctx.store.claimDueCleanupTask(
            now=now,
            kind=kind,
        )

    def _processClaimedTaskDo(
        self,
        task: CleanupTaskRecord,
        *,
        now: str,
        tymth,
        tock: float = 0.0,
    ):
        """
        Process a single claimed cleanup task cooperatively.

        - Session tasks: mark expired, clean resources, delete after retention.
        - Account tasks: mark expired, clean resources, delete after retention.
        """
        try:
            if task.kind == self.SESSION_EXPIRE_TASK:
                return self._processSessionExpire(task, now=now)
            if task.kind == self.SESSION_CLEANUP_TASK:
                return (
                    yield from self._processSessionCleanupDo(
                        task,
                        now=now,
                        tymth=tymth,
                        tock=tock,
                    )
                )
            if task.kind == self.SESSION_DELETE_TASK:
                return self._processSessionDelete(task, now=now)
            if task.kind == self.ACCOUNT_EXPIRE_TASK:
                return self._processAccountExpire(task, now=now)
            if task.kind == self.ACCOUNT_CLEANUP_TASK:
                return (
                    yield from self._processAccountCleanupDo(
                        task,
                        now=now,
                        tymth=tymth,
                        tock=tock,
                    )
                )
            if task.kind == self.ACCOUNT_DELETE_TASK:
                return (
                    yield from self._processAccountDeleteDo(
                        task,
                        now=now,
                        tymth=tymth,
                        tock=tock,
                    )
                )
        except Exception as exc:
            logger.exception(
                "Unexpected cleanup task failure: kind=%s subject=%s",
                task.kind,
                task.subject,
            )
            # Set the task failure and block it immediately since it cannot be handled
            self._handleTaskFailure(
                task,
                exc,
                now=now,
                operation=task.kind,
                immediate_block=True,
            )
            return None, None

        # Unknown tasks are invalid, mark as complete for removal.
        logger.warning(f"Unknown cleanup task kind '{task.kind}' for subject {task.subject}")
        self._completeTask(task.kind, task.subject)
        return None, None

    def _processSessionExpire(
        self,
        task: CleanupTaskRecord,
        *,
        now: str,
    ) -> tuple[str | None, object | None]:
        """Set a session as expired when its stored expiry time is due"""

        # Retrieve session
        session = self.ctx.store.getSession(task.subject)

        # If session is not found, it is an orphaned task, mark as complete for removal
        if session is None:
            self._completeTask(task.kind, task.subject)
            return None, None

        # If session is in terminal states, mark as complete for removal
        if session.state in TERMINAL_SESSION_STATES:
            self._completeTask(task.kind, task.subject)
            return None, None

        # If session does not have an expiration date, task is invalid so mark as complete for removal
        if not session.expires_at:
            self._completeTask(task.kind, task.subject)
            return None, None

        try:
            # Validate expiration date
            expires_at = datetime.fromisoformat(session.expires_at)
        except ValueError:
            logger.warning(
                f"Session {session.session_id} has invalid expires_at format: {session.expires_at}",
            )
            # Invalid expiry metadata should fail closed instead of silently escaping
            # the lifecycle. Expire the session immediately so cleanup can reclaim
            # its resources on the next phase.
            self.markSessionExpired(session, now=now)
            return "sessions_expired", session

        # If session is not due yet, reschedule task with the updated time
        if expires_at > datetime.fromisoformat(now):
            self._rescheduleTask(
                task.kind,
                task.subject,
                due_at=session.expires_at,
                now=now,
                reset_attempts=True,
            )
            return None, None

        # Mark the session as expired
        self.markSessionExpired(session, now=now)
        logger.info(
            f"Session expired for session {session.session_id}",
        )
        return "sessions_expired", session

    def _processSessionCleanupDo(
        self,
        task: CleanupTaskRecord,
        *,
        now: str,
        tymth,
        tock: float = 0.0,
    ):
        """Tear down hosted resources for one expired session cooperatively."""

        # Retrieve session
        session = self.ctx.store.getSession(task.subject)

        # If session is None, task is invalid, mark as complete for removal
        if session is None:
            self._completeTask(task.kind, task.subject)
            return None, None

        # If session is not expired, failed or cancelled session cleanup task is invalid
        if session.state not in {
            SESSION_STATE_EXPIRED,
            SESSION_STATE_FAILED,
            SESSION_STATE_CANCELLED,
        }:
            self._completeTask(task.kind, task.subject)
            return None, None

        # If session resources have already been cleaned, re-sync to the delete phase
        if session.resources_cleaned_at:
            self.ctx.store.saveSession(session)
            self._completeTask(task.kind, task.subject)
            return None, None

        # Retrieve the account for that session
        account = self.ctx.store.getAccount(session.account_aid) if session.account_aid else None

        try:
            # Teardown session resources cooperatively
            yield from self.provisioner.teardownSessionResourcesDo(
                session=session,
                account=account,
                tymth=tymth,
                tock=tock,
            )
        except BootError as exc:
            # If error, log and save it
            session.failure_reason = f"Cleanup failed after expiry: {exc}"
            session.updated_at = now
            self.ctx.store.saveSession(session)
            self._handleTaskFailure(
                task,
                exc,
                now=now,
                operation="session cleanup",
            )
            # Retry or block both mean cleanup is still pending, so do not report this
            # task as successfully cleaned in sweep results.
            return None, None

        # Update session record with the resources cleaned up time
        session.resources_cleaned_at = now
        session.updated_at = now
        self.ctx.store.saveSession(session)

        if account is None:
            failed = None
        elif account.status == ACCOUNT_STATE_ONBOARDED:
            failed = None
        else:
            failed = accountFailed(account)

        if failed is not None:
            # If account exists in failed state, mark as resources cleaned
            failed.resources_cleaned_at = now
            failed.session_id = ""
            self.ctx.store.saveAccount(failed)
            logger.info(
                f"Account failed due to session expiry for account {account.account_aid}",
            )

        # Mark task as complete for removal
        self._completeTask(task.kind, task.subject)
        logger.info(
            f"Session resources cleaned after expiry for session {session.session_id}",
        )
        return "sessions_cleaned", session.session_id

    def _processSessionDelete(
        self,
        task: CleanupTaskRecord,
        *,
        now: str,
    ) -> tuple[str | None, object | None]:
        """Delete one closed session after its retention window elapses."""

        # Retrieve session for that task/subject pair
        session = self.ctx.store.getSession(task.subject)

        # If session is not found, task is invalid 
        if session is None:
            self._completeTask(task.kind, task.subject)
            return None, None

        # If session is not expired, failed, cancelled or completed, session delete task is invalid
        if session.state not in {
            SESSION_STATE_EXPIRED,
            SESSION_STATE_FAILED,
            SESSION_STATE_CANCELLED,
            SESSION_STATE_COMPLETED,
        }:
            self._completeTask(task.kind, task.subject)
            return None, None

        # If session is not expired, failed, or cancelled, and resources haven't been cleaned yet,
        # session delete task is invalid
        if (
            session.state in {
                SESSION_STATE_EXPIRED,
                SESSION_STATE_FAILED,
                SESSION_STATE_CANCELLED,
            }
            and not session.resources_cleaned_at
        ):
            self.ctx.store.saveSession(session)
            self._completeTask(task.kind, task.subject)
            return None, None

        # Retrieve delete due time, if it has not elapsed yet, reschedule to the new time
        delete_due_at = self.ctx.store.sessionDeleteDueAt(session)
        if delete_due_at > now:
            self._rescheduleTask(
                task.kind,
                task.subject,
                due_at=delete_due_at,
                now=now,
            )
            return None, None

        # Session is valid for deletion, proceed to deletion
        self.ctx.store.deleteSession(session.session_id)
        logger.info(f"Closed session deleted for session {session.session_id}")
        return "sessions_deleted", session.session_id

    def _processAccountExpire(
        self,
        task: CleanupTaskRecord,
        *,
        now: str,
    ) -> tuple[str | None, object | None]:
        """Expire one onboarded account if its expiry time is due"""

        # Retrieve account for that task
        account = self.ctx.store.getAccount(task.subject)

        # If account is not found, task is invalid mark as complete for removal
        if account is None:
            self._completeTask(task.kind, task.subject)
            return None, None

        # If account is not onboarded or does not have an expiry date, task is invalid
        if account.status != ACCOUNT_STATE_ONBOARDED or not account.expires_at:
            self._completeTask(task.kind, task.subject)
            return None, None

        try:
            # Validate expiration date
            expires_at = datetime.fromisoformat(account.expires_at)
        except ValueError:
            logger.warning(
                f"Account {account.account_aid} has invalid expires_at format: {account.expires_at}",
            )
            # Invalid expiry metadata should fail closed instead of silently escaping
            # account cleanup. Mark the account expired so teardown can proceed.
            self.markAccountExpired(account, now=now)
            return "accounts_expired", account.account_aid

        # If account is not yet due, reschedule task with the new date
        if expires_at > datetime.fromisoformat(now):
            self._rescheduleTask(
                task.kind,
                task.subject,
                due_at=account.expires_at,
                now=now,
                reset_attempts=True,
            )
            return None, None

        # Mark the account as expired
        self.markAccountExpired(account, now=now)
        logger.info(
            f"Account expired at {account.expires_at} for account AID {account.account_aid}",
        )
        return "accounts_expired", account.account_aid

    def _processAccountCleanupDo(
        self,
        task: CleanupTaskRecord,
        *,
        now: str,
        tymth,
        tock: float = 0.0,
    ):
        """Tear down hosted resources for one expired account cooperatively."""

        account = self._prepareAccountCleanupTask(task)
        if account is None:
            return None, None

        try:
            # Teardown Account resources cooperatively
            yield from self.provisioner.teardownAccountResourcesDo(
                account_aid=account.account_aid,
                account=account,
                tymth=tymth,
                tock=tock,
            )
        except BootError as exc:
            self._handleTaskFailure(
                task,
                exc,
                now=now,
                operation="account cleanup",
            )
            # The account is still waiting on teardown, so retrying or blocking
            # should not count as a completed cleanup in sweep counters.
            return None, None

        return self._finishAccountCleanupTask(task, account, now=now)

    def _prepareAccountCleanupTask(self, task: CleanupTaskRecord):
        account = self.ctx.store.getAccount(task.subject)
        if account is None:
            self._completeTask(task.kind, task.subject)
            return None

        if account.status not in {
            ACCOUNT_STATE_EXPIRED,
            ACCOUNT_STATE_FAILED,
        }:
            self._completeTask(task.kind, task.subject)
            return None

        if account.resources_cleaned_at:
            # Re-save so store task syncing can advance this account to the delete phase.
            self.ctx.store.saveAccount(account)
            self._completeTask(task.kind, task.subject)
            return None

        return account

    def _finishAccountCleanupTask(
        self,
        task: CleanupTaskRecord,
        account,
        *,
        now: str,
    ) -> tuple[str, str]:
        account.resources_cleaned_at = now
        self.ctx.store.saveAccount(account)
        self._completeTask(task.kind, task.subject)
        logger.info(
            f"Expired account resources cleaned for account AID {account.account_aid}",
        )
        return "accounts_cleaned", account.account_aid

    def _processAccountDeleteDo(
        self,
        task: CleanupTaskRecord,
        *,
        now: str,
        tymth,
        tock: float = 0.0,
    ):
        """Delete one expired account cooperatively after retention elapses."""

        account = self._prepareAccountDeleteTask(task, now=now)
        if account is None:
            return None, None

        try:
            # Delete account cooperatively
            yield from self.provisioner.deleteAccountDo(
                account_aid=account.account_aid,
                account=account,
                tymth=tymth,
                tock=tock,
            )
        except BootError as exc:
            self._handleTaskFailure(
                task,
                exc,
                now=now,
                operation="account delete",
            )
            return None, None

        return self._finishAccountDeleteTask(task, account)

    def _prepareAccountDeleteTask(self, task: CleanupTaskRecord, *, now: str):
        account = self.ctx.store.getAccount(task.subject)
        if account is None:
            self._completeTask(task.kind, task.subject)
            return None

        if account.status not in {
            ACCOUNT_STATE_EXPIRED,
            ACCOUNT_STATE_FAILED,
        }:
            self._completeTask(task.kind, task.subject)
            return None

        if not account.resources_cleaned_at:
            # Cleanup must finish before account deletion can be considered valid.
            self.ctx.store.saveAccount(account)
            self._completeTask(task.kind, task.subject)
            return None

        delete_due_at = self.ctx.store.accountDeleteDueAt(account)
        if delete_due_at > now:
            # Retention has not elapsed yet; put the delete task back at its due time.
            self._rescheduleTask(
                task.kind,
                task.subject,
                due_at=delete_due_at,
                now=now,
            )
            return None

        return account

    def _handleTaskFailure(
        self,
        task: CleanupTaskRecord,
        exc: Exception,
        *,
        now: str,
        operation: str,
        immediate_block: bool = False,
    ) -> str:
        """Either retry or quarantine a failed cleanup task.

        Temporary downstream failures should keep using exponential backoff, but
        poisoned tasks need a durable blocked state so the cleaner stops retrying
        work that clearly needs operator intervention.
        """

        error_text = str(exc)
        first_failed_at = task.first_failed_at or now

        # First determine if the task should be blocked
        if self._shouldBlockTask(
            task,
            exc,
            now=now,
            first_failed_at=first_failed_at,
            immediate_block=immediate_block,
        ):
            # If it should be blocked, set the reason and save it in DB
            blocked_reason = self._blockedReason(
                task,
                exc,
                now=now,
                operation=operation,
                first_failed_at=first_failed_at,
                immediate_block=immediate_block,
            )
            self.ctx.store.blockCleanupTask(
                task.kind,
                task.subject,
                now=now,
                blocked_reason=blocked_reason,
                last_error=error_text,
                first_failed_at=first_failed_at,
            )
            logger.warning(
                "Cleanup task blocked: kind=%s subject=%s reason=%s",
                task.kind,
                task.subject,
                blocked_reason,
            )
            return "blocked"

        # Reschedule the task with failure metadata
        retry_time = self._nextRetryAt(task, now=now)
        self.ctx.store.rescheduleFailedCleanupTask(
            task.kind,
            task.subject,
            due_at=retry_time,
            now=now,
            last_error=error_text,
            first_failed_at=first_failed_at,
        )
        logger.warning(
            "Cleanup task rescheduled after failure: kind=%s subject=%s retry_at=%s error=%s",
            task.kind,
            task.subject,
            retry_time,
            error_text,
        )
        return "rescheduled"

    def _shouldBlockTask(
        self,
        task: CleanupTaskRecord,
        exc: Exception,
        *,
        now: str,
        first_failed_at: str,
        immediate_block: bool = False,
    ) -> bool:
        """Return True when a failed cleanup task should be quarantined."""
        attempt_limit = self.ctx.config.cleanup_block_after_attempts
        failure_age_limit = self.ctx.config.cleanup_block_after_failure_age_seconds

        # Check for the immediate_block flag 
        if immediate_block:
            return True

        if isinstance(exc, BootError) and not self._bootErrorIsRetryable(exc):
            return True

        # Check for config thresholds 
        if task.attempt_count >= attempt_limit:
            return True
        return (
            self._failureAgeSeconds(first_failed_at=first_failed_at, now=now)
            >= failure_age_limit
        )

    def _bootErrorIsRetryable(self, exc: BootError) -> bool:
        """Classify which downstream HTTP failures are worth retrying."""

        # Return true if it is not an HTTP error
        if exc.status_code is None:
            return True
        
        # Retry on server errors 
        if exc.status_code >= 500:
            return True
        
        # Retry on client errors like rate limits or timeouts
        # This might be unecessary but those fits retryable errors
        return exc.status_code in {408, 425, 429}

    def _failureAgeSeconds(self, *, first_failed_at: str, now: str) -> float:
        """Measure how long this task has been failing"""

        try:
            failed_at = datetime.fromisoformat(first_failed_at)
            current = datetime.fromisoformat(now)
        except ValueError:
            return float("inf")
        return max((current - failed_at).total_seconds(), 0.0)

    def _blockedReason(
        self,
        task: CleanupTaskRecord,
        exc: Exception,
        *,
        now: str,
        operation: str,
        first_failed_at: str,
        immediate_block: bool = False,
    ) -> str:
        """Build a reason for task quarantine:
        1 - Immediate block flag was set
        2 - The error is non-retryable based on its type or status code
        3 - The task has exceeded the configured retry attempt limit
        4 - The task has exceeded the configured failure time limit
        """
        attempt_limit = self.ctx.config.cleanup_block_after_attempts
        failure_age_limit = self.ctx.config.cleanup_block_after_failure_age_seconds

        if immediate_block:
            return (
                f"Unexpected cleanup error during {operation}: "
                f"{type(exc).__name__}: {exc}"
            )
        if isinstance(exc, BootError) and not self._bootErrorIsRetryable(exc):
            code = exc.status_code if exc.status_code is not None else "unknown"
            return (
                f"Non-retryable cleanup failure during {operation} "
                f"(status={code}): {exc}"
            )
        if task.attempt_count >= attempt_limit:
            return (
                f"Cleanup retry limit reached during {operation}: "
                f"{task.attempt_count} attempts >= "
                f"{attempt_limit} allowed"
            )
        age_seconds = self._failureAgeSeconds(first_failed_at=first_failed_at, now=now)
        return (
            f"Cleanup failure age limit reached during {operation}: "
            f"{age_seconds:.0f}s >= "
            f"{failure_age_limit:.0f}s"
        )

    def _finishAccountDeleteTask(
        self,
        task: CleanupTaskRecord,
        account,
    ) -> tuple[str, str]:
        self._completeTask(task.kind, task.subject)
        logger.info(
            f"Expired account deleted for account AID {account.account_aid}",
        )
        return "accounts_deleted", account.account_aid

    def _nextRetryAt(self, task: CleanupTaskRecord, *, now: str) -> str:
        """Compute the next retry time using exponential backoff and optional jitter"""
        base_delay = max(self.ctx.config.cleanup_failure_backoff_seconds, 0.0)
        max_delay = max(self.ctx.config.cleanup_failure_backoff_max_seconds, base_delay)
        attempt_number = max(task.attempt_count, 1)
        delay = base_delay * (2 ** (attempt_number - 1)) if base_delay > 0 else 0.0
        delay = min(delay, max_delay)

        jitter = max(self.ctx.config.cleanup_failure_jitter_seconds, 0.0)
        if jitter > 0:
            delay += random.uniform(0.0, jitter)

        return (datetime.fromisoformat(now) + timedelta(seconds=delay)).isoformat()

    def _rescheduleTask(
        self,
        kind: str,
        subject: str,
        *,
        due_at: str,
        now: str,
        last_error: str | None = None,
        reset_attempts: bool = False,
    ) -> None:
        """Saves a new due time for a task after deferral or failure in the DB"""
        self.ctx.store.scheduleCleanupTask(
            kind,
            subject,
            due_at=due_at,
            now=now,
            last_error=last_error,
            reset_attempts=reset_attempts,
        )

    def _completeTask(self, kind: str, subject: str) -> None:
        """Remove a task from the durable queue once it has finished"""
        self.ctx.store.completeCleanupTask(kind, subject)
