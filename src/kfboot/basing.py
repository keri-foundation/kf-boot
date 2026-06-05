# -*- encoding: utf-8 -*-
"""
kfboot.basing module

LMDB storage for KF boot onboarding, account, and hosted-resource metadata.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from keri.db import dbing, koming


SESSION_STATE_STARTED = "started"
SESSION_STATE_WITNESS_POOL_ALLOCATED = "witness_pool_allocated"
SESSION_STATE_ACCOUNT_CREATED = "account_created"
SESSION_STATE_COMPLETED = "completed"
SESSION_STATE_EXPIRED = "expired"
SESSION_STATE_FAILED = "failed"
SESSION_STATE_CANCELLED = "cancelled"

ACCOUNT_STATE_PENDING_ONBOARDING = "pending_onboarding"
ACCOUNT_STATE_ONBOARDED = "onboarded"
ACCOUNT_STATE_EXPIRED = "expired"
ACCOUNT_STATE_FAILED = "failed"

CLEANUP_TASK_SESSION_EXPIRE = "session_expire"
CLEANUP_TASK_SESSION_CLEANUP = "session_cleanup"
CLEANUP_TASK_SESSION_DELETE = "session_delete"
CLEANUP_TASK_ACCOUNT_EXPIRE = "account_expire"
CLEANUP_TASK_ACCOUNT_CLEANUP = "account_cleanup"
CLEANUP_TASK_ACCOUNT_DELETE = "account_delete"

BOOT_OPERATION_SESSION_PROVISION = "session_provision"
BOOT_OPERATION_WATCHER_STATUS_QUERY = "watcher_status_query"
BOOT_OPERATION_RESOURCE_DELETE = "resource_delete"
BOOT_OPERATION_ACCOUNT_DELETE = "account_delete"

BOOT_OPERATION_PENDING = "pending"
BOOT_OPERATION_RUNNING = "running"
BOOT_OPERATION_SUCCEEDED = "succeeded"
BOOT_OPERATION_FAILED = "failed"

TERMINAL_SESSION_STATES = {
    SESSION_STATE_COMPLETED,
    SESSION_STATE_EXPIRED,
    SESSION_STATE_FAILED,
    SESSION_STATE_CANCELLED,
}

ACTIVE_BOOT_OPERATION_STATES = {
    BOOT_OPERATION_PENDING,
    BOOT_OPERATION_RUNNING,
}


@dataclass
class ResourceRecord:
    kind: str = ""
    eid: str = ""
    backend_id: str = ""
    cid: str = ""
    principal: str = ""
    session_id: str = ""
    name: str = ""
    identifier_alias: str = ""
    region_id: str = ""
    region_name: str = ""
    url: str = ""
    boot_url: str = ""
    public_host: str = ""
    public_port: int | None = None
    oobis: list[str] = field(default_factory=list)
    status: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class BindingRecord:
    principal: str = ""
    cid: str = ""


@dataclass
class SessionRecord:
    session_id: str = ""
    ephemeral_aid: str = ""
    account_aid: str = ""
    account_alias: str = ""
    state: str = SESSION_STATE_STARTED
    created_at: str = ""
    updated_at: str = ""
    expires_at: str = ""
    client_ip: str = ""
    chosen_profile_code: str = ""
    witness_backend_ids: list[str] = field(default_factory=list)
    witness_eids: list[str] = field(default_factory=list)
    watcher_eid: str = ""
    watcher_required: bool = True
    region_id: str = ""
    region_name: str = ""
    witness_count: int = 0
    toad: int = 0
    account_tier: str = ""
    failure_reason: str = ""
    expired_at: str = ""
    resources_cleaned_at: str = ""


@dataclass
class AccountRecord:
    account_aid: str = ""
    account_alias: str = ""
    status: str = ACCOUNT_STATE_PENDING_ONBOARDING
    created_at: str = ""
    onboarded_at: str = ""
    witness_profile_code: str = ""
    witness_count: int = 0
    toad: int = 0
    watcher_required: bool = True
    region_id: str = ""
    region_name: str = ""
    session_id: str = ""
    witness_eids: list[str] = field(default_factory=list)
    watcher_eid: str = ""
    tier: str = ""
    expires_at: str = ""
    api_used: int = 0
    expired_at: str = ""
    resources_cleaned_at: str = ""


@dataclass
class QuotaRecord:
    scope: str = ""
    subject: str = ""
    window_start: str = ""
    count: int = 0
    blocked_until: str = ""


@dataclass
class CleanupTaskRecord:
    kind: str = ""
    subject: str = ""
    due_at: str = ""
    attempt_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    claimed_at: str = ""
    last_attempt_at: str = ""
    first_failed_at: str = ""
    last_error: str = ""
    blocked_at: str = ""
    blocked_reason: str = ""


@dataclass(frozen=True)
class CleanupDueRecord:
    kind: str = ""
    subject: str = ""
    due_at: str = ""


@dataclass
class CleanupAdminActionRecord:
    action_id: str = ""
    logged_at: str = ""
    action: str = ""
    actor: str = ""
    kind: str = ""
    subject: str = ""
    forced: bool = False
    operator_reason: str = ""
    task_blocked_at: str = ""
    task_blocked_reason: str = ""
    task_last_error: str = ""
    task_attempt_count: int = 0
    task_first_failed_at: str = ""
    safe_to_dismiss: bool = False
    cleanup_assured: bool = False
    local_resource_count: int = 0
    local_related_record_count: int = 0
    subject_exists: bool = False
    subject_state: str = ""
    resources_cleaned_at: str = ""
    assessment_reason: str = ""


@dataclass
class BootOperationRecord:
    operation_id: str = ""
    kind: str = ""
    subject: str = ""
    requester: str = ""
    route: str = ""
    state: str = BOOT_OPERATION_PENDING
    idempotency_key: str = ""
    payload: dict = field(default_factory=dict)
    result: dict = field(default_factory=dict)
    last_error: str = ""
    attempt_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    due_at: str = ""
    claimed_at: str = ""
    last_attempt_at: str = ""


@dataclass(frozen=True)
class BootOperationDueRecord:
    operation_id: str = ""
    kind: str = ""
    due_at: str = ""


class PlatformBaser(dbing.LMDBer):
    """LMDB database for the KF boot service."""

    TailDirPath = ""
    AltTailDirPath = ".kf-boot"
    TempPrefix = "kf_boot_"

    def __init__(self, name="platform", headDirPath=None, reopen=True, **kwa):
        self.resources = None
        self.bindings = None
        self.sessions = None
        self.accounts = None
        self.quotas = None
        self.cleanup_tasks = None
        self.cleanup_due = None
        self.cleanup_admin_actions = None
        self.boot_operations = None
        self.boot_operation_due = None
        super().__init__(
            name=name,
            headDirPath=headDirPath,
            reopen=reopen,
            **kwa,
        )

    def reopen(self, **kwa):
        super().reopen(**kwa)

        self.resources = koming.Komer(
            db=self,
            subkey="resc.",
            klas=ResourceRecord,
        )
        self.bindings = koming.Komer(
            db=self,
            subkey="bind.",
            klas=BindingRecord,
        )
        self.sessions = koming.Komer(
            db=self,
            subkey="sess.",
            klas=SessionRecord,
        )
        self.accounts = koming.Komer(
            db=self,
            subkey="acct.",
            klas=AccountRecord,
        )
        self.quotas = koming.Komer(
            db=self,
            subkey="quot.",
            klas=QuotaRecord,
        )
        # Periodic Flush DBs
        self.cleanup_tasks = koming.Komer(
            db=self,
            subkey="ctsk.",
            klas=CleanupTaskRecord,
        )
        self.cleanup_due = koming.Komer(
            db=self,
            subkey="cdue.",
            klas=CleanupDueRecord,
        )
        self.cleanup_admin_actions = koming.Komer(
            db=self,
            subkey="cada.",
            klas=CleanupAdminActionRecord,
        )
        self.boot_operations = koming.Komer(
            db=self,
            subkey="bopr.",
            klas=BootOperationRecord,
        )
        self.boot_operation_due = koming.Komer(
            db=self,
            subkey="bdue.",
            klas=BootOperationDueRecord,
        )
        return self.env


def open_baser(db_path: str) -> PlatformBaser:
    path = Path(db_path).expanduser()
    name = path.stem if path.suffix else path.name
    head = path.parent if path.parent != Path("") else Path(".")

    return PlatformBaser(
        name=name or "platform",
        headDirPath=str(head),
        reopen=True,
    )
