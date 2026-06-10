from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from hio.base import doing
from keri import help

from kfboot.basing import (
    BOOT_OPERATION_ACCOUNT_DELETE,
    BOOT_OPERATION_RESOURCE_DELETE,
    BOOT_OPERATION_SESSION_PROVISION,
    BOOT_OPERATION_WATCHER_STATUS_QUERY,
)
from kfboot.boot_client import BootError
from kfboot.store import nowIso

logger = help.ogler.getLogger(__name__)


@dataclass(frozen=True)
class BootOperationProcessor:
    provisioner: Any

    def processBootOperationDo(self, *, operation, witness_boots, watcher_boot, tymth, tock: float = 0.0):
        if operation.kind == BOOT_OPERATION_SESSION_PROVISION:
            session_id = str(operation.payload.get("session_id") or operation.subject)
            session = self.provisioner.ctx.store.getSession(session_id)
            if session is None:
                raise BootError(f"Session '{session_id}' was not found.", status_code=404)
            return (
                yield from self.provisioner.provisionSessionResourcesDo(
                    session=session,
                    witness_boots=witness_boots,
                    watcher_boot=watcher_boot,
                    operation_id=operation.operation_id,
                    tymth=tymth,
                    tock=tock,
                )
            )

        if operation.kind == BOOT_OPERATION_WATCHER_STATUS_QUERY:
            watcher_id = str(operation.payload.get("watcher_id") or "")
            if not watcher_id:
                raise BootError("Watcher status operation is missing watcher_id.", status_code=400)
            record = self.provisioner.ctx.store.getResource("watcher", watcher_id)
            if record is None:
                raise BootError(f"Watcher '{watcher_id}' was not found.", status_code=404)

            status = yield from watcher_boot.watcherStatusDo(
                watcher_id,
                tymth=tymth,
                tock=tock,
            )
            derived_status = ""
            if isinstance(status, dict):
                direct = status.get("status")
                summary = status.get("summary")
                if isinstance(direct, str) and direct:
                    derived_status = direct
                elif isinstance(summary, dict):
                    total = int(summary.get("total_witnesses") or 0)
                    responsive = int(summary.get("responsive_witnesses") or 0)
                    if responsive >= total > 0:
                        derived_status = "connected"
                    elif total > 0 and responsive <= 0:
                        derived_status = "disconnected"
                    elif total > 0:
                        derived_status = "query_pending"
            if derived_status:
                record.status = derived_status
                self.provisioner.ctx.store.saveResource(record)
            return status

        if operation.kind == BOOT_OPERATION_RESOURCE_DELETE:
            resource_kind = str(operation.payload.get("resource_kind") or "")
            resource_id = str(operation.payload.get("resource_id") or "")
            if resource_kind not in {"witness", "watcher"} or not resource_id:
                raise BootError("Resource delete operation is missing a valid resource subject.", status_code=400)

            account_aid = str(operation.payload.get("account_aid") or "")
            account = self.provisioner.ctx.store.getAccount(account_aid) if account_aid else None
            record = self.provisioner.ctx.store.getResource(resource_kind, resource_id)
            session_id = str(operation.payload.get("session_id") or getattr(record, "session_id", "") or "")
            session = self.provisioner.ctx.store.getSession(session_id) if session_id else None
            yield from self.provisioner.deleteHostedResourceDo(
                kind=resource_kind,
                eid=resource_id,
                session=session,
                account=account,
                tolerate_missing_remote=True,
                witness_boots=witness_boots,
                watcher_boot=watcher_boot,
                tymth=tymth,
                tock=tock,
            )
            return {"kind": resource_kind, "eid": resource_id, "deleted": True}

        if operation.kind == BOOT_OPERATION_ACCOUNT_DELETE:
            account_aid = str(operation.payload.get("account_aid") or "")
            if not account_aid:
                raise BootError("Account delete operation is missing account_aid.", status_code=400)
            account = self.provisioner.ctx.store.getAccount(account_aid)
            session_ids = [
                session.session_id
                for session in self.provisioner.ctx.store.listSessionsForAccount(account_aid)
            ]
            yield from self.provisioner.deleteAccountDo(
                account_aid=account_aid,
                account=account,
                witness_boots=witness_boots,
                watcher_boot=watcher_boot,
                tymth=tymth,
                tock=tock,
            )
            return {
                "account_aid": account_aid,
                "deleted": True,
                "session_ids": session_ids,
            }

        raise BootError(f"Unsupported boot operation kind '{operation.kind}'.", status_code=400)


class BootOperationDoer(doing.DoDoer):
    def __init__(
        self,
        *,
        store,
        witness_boots: dict[str, Any] | None = None,
        watcher_boot=None,
        processor=None,
        clienter=None,
        interval: float = 0.05,
        batch_size: int = 10,
        failure_backoff_seconds: float = 5.0,
        failure_backoff_max_seconds: float = 300.0,
        failure_max_attempts: int = 10,
    ):
        run_tock = 0.05
        self.store = store
        self.witness_boots = witness_boots or {}
        self.watcher_boot = watcher_boot
        self.processor = processor
        self.clienter = clienter
        self.interval = interval
        self.batch_size = batch_size
        self.failure_backoff_seconds = failure_backoff_seconds
        self.failure_backoff_max_seconds = failure_backoff_max_seconds
        self.failure_max_attempts = failure_max_attempts
        self.recovered_claimed_operations = 0
        self.last_processed_count = 0
        self.last_error = ""

        doers = []
        if self.clienter is not None:
            doers.append(self.clienter)
        doers.append(doing.doify(self.operationDo, tock=run_tock))
        super().__init__(doers=doers, tock=run_tock)

    def operationDo(self, tymth, tock=0.0, **kwa):
        """Drive due boot operations from the root HIO runtime."""
        now = nowIso()
        self.recovered_claimed_operations = self.store.requeueClaimedBootOperations(now=now)
        if self.recovered_claimed_operations:
            logger.info(
                f"Recovered {self.recovered_claimed_operations} claimed boot operation(s) during startup"
            )

        next_run_at = 0.0
        yield tock
        while True:
            tyme = tymth()
            if tyme < next_run_at:
                yield tock
                continue

            self.last_processed_count = yield from self.processDueDo(tymth=tymth, tock=tock)
            next_run_at = tyme + self.interval
            yield tock

    def processDueDo(self, *, tymth, tock: float = 0.0):
        processed = 0
        while processed < self.batch_size:
            operation = self.store.claimDueBootOperation(now=nowIso())
            if operation is None:
                break

            processed += 1
            try:
                if self.processor is None:
                    raise BootError("Boot operation processor is not configured.", status_code=503)
                if not hasattr(self.processor, "processBootOperationDo"):
                    raise BootError("Boot operation processor is missing processBootOperationDo.", status_code=503)

                result = yield from self.processor.processBootOperationDo(
                    operation=operation,
                    witness_boots=self.witness_boots,
                    watcher_boot=self.watcher_boot,
                    tymth=tymth,
                    tock=tock,
                )
                if result is None:
                    result = {}
                if not isinstance(result, dict):
                    raise BootError("Boot operation processor returned a non-dict result.", status_code=500)
            except BootError as exc:
                current = nowIso()
                self.last_error = str(exc)
                retryable = True
                if self.failure_max_attempts > 0 and operation.attempt_count >= self.failure_max_attempts:
                    retryable = False
                elif exc.status_code is None:
                    retryable = True
                elif exc.status_code >= 500:
                    retryable = True
                else:
                    retryable = exc.status_code in {408, 425, 429}

                if not retryable:
                    result = {}
                    if exc.status_code is not None:
                        result["status_code"] = exc.status_code
                    self.store.failBootOperation(
                        operation.operation_id,
                        last_error=str(exc),
                        result=result,
                        now=current,
                    )
                    logger.warning(
                        f"Boot operation {operation.operation_id} failed permanently: {exc}"
                    )
                    yield tock
                    continue

                delay = min(
                    self.failure_backoff_seconds * (2 ** max(operation.attempt_count - 1, 0)),
                    self.failure_backoff_max_seconds,
                )
                retry_at = (datetime.fromisoformat(current) + timedelta(seconds=delay)).isoformat()
                self.store.rescheduleBootOperation(
                    operation.operation_id,
                    due_at=retry_at,
                    now=current,
                    last_error=str(exc),
                )
                logger.warning(
                    f"Boot operation {operation.operation_id} failed and was rescheduled for {retry_at}: {exc}"
                )
            except Exception as exc:
                message = getattr(exc, "description", "") or getattr(exc, "title", "") or exc
                self.last_error = str(message)
                self.store.failBootOperation(
                    operation.operation_id,
                    last_error=self.last_error,
                    now=nowIso(),
                )
                logger.exception(f"Boot operation {operation.operation_id} failed unexpectedly")
            else:
                self.store.succeedBootOperation(
                    operation.operation_id,
                    result=result,
                    now=nowIso(),
                )

            yield tock

        return processed
