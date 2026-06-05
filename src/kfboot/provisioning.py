# provisioning.py
from __future__ import annotations

import falcon
from hashlib import blake2b
from typing import Any

from keri import help

from kfboot.basing import (
    SESSION_STATE_FAILED,
    SESSION_STATE_WITNESS_POOL_ALLOCATED,
    TERMINAL_SESSION_STATES,
    SessionRecord,
)

from kfboot.boot_client import BootError
from kfboot.store import (
    makeRecord,
    nowIso,
    parsePublicUrl,
)

logger = help.ogler.getLogger(__name__)


class Provisioner:
    def __init__(self, ctx, exchanger):
        self.ctx = ctx
        self.exchanger = exchanger
        self.cleanup_witness_boots: dict[str, Any] = {}
        self.cleanup_watcher_boot: Any | None = None

    def configureCleanupBootClients(self, *, witness_boots, watcher_boot) -> None:
        self.cleanup_witness_boots = witness_boots
        self.cleanup_watcher_boot = watcher_boot

    def provisionSessionResourcesDo(
        self,
        *,
        session: SessionRecord,
        witness_boots,
        watcher_boot,
        operation_id: str,
        tymth,
        tock: float = 0.0,
    ):
        session = self.ctx.store.getSession(session.session_id) or session
        terminal = self._terminalProvisionResult(session)
        if terminal is not None:
            return terminal

        missing_witnesses = max(session.witness_count - len(session.witness_eids), 0)
        if missing_witnesses:
            logger.info(
                f"Witness(es) requested for session {session.session_id}"
                f" with {missing_witnesses} missing witness(es)"
            )
            self._ensureCapacity(kind="witness", requested=missing_witnesses)
            planned_backends = self._plannedWitnessBackends(session=session)
            start_index = len(session.witness_eids)
            for index, backend in enumerate(planned_backends[start_index:], start=start_index):
                logger.info(
                    f"Witness allocation start for session {session.session_id}"
                )
                client = self._witnessClient(backend.id, witness_boots=witness_boots)
                created = yield from client.allocateWitnessDo(
                    session.account_aid,
                    idempotency_key=f"{operation_id}:witness:{backend.id}:{index}",
                    tymth=tymth,
                    tock=tock,
                )
                current = self.ctx.store.getSession(session.session_id)
                if current is not None:
                    session = current
                record = makeRecord(
                    kind="witness",
                    eid=str(created.get("eid", "")),
                    backend_id=backend.id,
                    cid="",
                    principal="",
                    session_id=session.session_id,
                    name=str(created.get("name", "") or f"witness-{index + 1}"),
                    identifier_alias=session.account_alias,
                    region_id=session.region_id,
                    region_name=session.region_name,
                    public_url=backend.public_url,
                    boot_url=backend.boot_url,
                    oobis=list(created.get("oobis", []) or []),
                    status=str(created.get("status", "") or "allocated"),
                )
                self.ctx.store.addResource(record)
                terminal = self._terminalProvisionResult(session, cleanup_debt_added=True)
                if terminal is not None:
                    return terminal
                if record.eid not in session.witness_eids:
                    session.witness_eids.append(record.eid)
                session.updated_at = nowIso()
                self.ctx.store.saveSession(session)
                logger.info(
                    f"Witness allocated for session {session.session_id}: witness {record.eid}"
                )

            session = self.ctx.store.getSession(session.session_id) or session
            terminal = self._terminalProvisionResult(session)
            if terminal is not None:
                return terminal
            session.state = SESSION_STATE_WITNESS_POOL_ALLOCATED
            session.updated_at = nowIso()
            self.ctx.store.saveSession(session)
            logger.info(
                f"Witness pool allocated for session {session.session_id}: witness EIDs {session.witness_eids}"
            )

        session = self.ctx.store.getSession(session.session_id) or session
        terminal = self._terminalProvisionResult(session)
        if terminal is not None:
            return terminal
        if session.watcher_required and not session.watcher_eid:
            logger.info(
                f"Watcher requested for session {session.session_id}"
            )
            self._ensureCapacity(kind="watcher", requested=1)
            first_witness = self.ctx.store.getResource("witness", session.witness_eids[0])
            oobi = first_witness.oobis[0] if first_witness and first_witness.oobis else None
            logger.info(
                f"Watcher allocation start for session {session.session_id}"
            )
            created = yield from watcher_boot.allocateWatcherDo(
                session.account_aid,
                oobi=oobi,
                idempotency_key=f"{operation_id}:watcher",
                tymth=tymth,
                tock=tock,
            )
            current = self.ctx.store.getSession(session.session_id)
            if current is not None:
                session = current
            record = makeRecord(
                kind="watcher",
                eid=str(created.get("eid", "")),
                cid="",
                principal="",
                session_id=session.session_id,
                name=str(created.get("name", "") or "watcher"),
                identifier_alias=session.account_alias,
                region_id=session.region_id,
                region_name=session.region_name,
                public_url=self.ctx.config.wat_public_url,
                boot_url=getattr(watcher_boot, "base_url", self.ctx.config.wat_boot_url),
                oobis=list(created.get("oobis", []) or []),
                status=str(created.get("status", "") or "created"),
            )
            self.ctx.store.addResource(record)
            terminal = self._terminalProvisionResult(session, cleanup_debt_added=True)
            if terminal is not None:
                return terminal
            session.watcher_eid = record.eid
            session.updated_at = nowIso()
            self.ctx.store.saveSession(session)
            logger.info(
                f"Watcher allocated for session {session.session_id}: watcher {record.eid}"
            )

        return {
            "session_id": session.session_id,
            "state": session.state,
            "witness_eids": list(session.witness_eids),
            "watcher_eid": session.watcher_eid,
        }

    def _terminalProvisionResult(
        self,
        session: SessionRecord,
        *,
        cleanup_debt_added: bool = False,
    ) -> dict[str, Any] | None:
        if session.state not in TERMINAL_SESSION_STATES:
            return None

        if cleanup_debt_added and session.resources_cleaned_at:
            session.resources_cleaned_at = ""
            session.updated_at = nowIso()
            self.ctx.store.saveSession(session)

        if session.state == SESSION_STATE_FAILED:
            logger.warning(
                "Session in failed state during resource provisioning"
            )
            raise BootError(
                session.failure_reason or "The onboarding session is in a failed state.",
                status_code=409,
            )
        logger.info(
            f"Session in terminal state {session.state} during resource provisioning"
        )
        return {
            "session_id": session.session_id,
            "state": session.state,
            "witness_eids": list(session.witness_eids),
            "watcher_eid": session.watcher_eid,
        }

    def _ensureCapacity(self, *, kind: str, requested: int) -> None:
        if requested <= 0:
            return
        count = self.ctx.store.countResources(kind)
        limit = (
            self.ctx.config.witness_limit
            if kind == "witness"
            else self.ctx.config.watcher_limit
        )
        
        # Log warnings if the projected resource count approaches or exceeds the limit,
        # but still allow the request to proceed until the hard limit is reached
        projected = count + requested
        ratio = projected / max(limit, 1)
        if ratio >= 0.95:
            logger.warning(
                f"Projected {kind} usage is at 95% of capacity limit",
            )
        elif ratio >= 0.85:
            logger.info(
                f"Projected {kind} usage is at 85% of capacity limit",
            )
        elif ratio >= 0.7:
            logger.info(
                f"Projected {kind} usage is at 70% of capacity limit",
            )
        if projected > limit:
            logger.error(
                f"Capacity for {kind} exceeded: cannot provision {requested} as it would exceed the limit of {limit}"
                f" with current count at {count}"
            )
            raise falcon.HTTPConflict(
                title="Capacity exceeded",
                description=(
                    f"{kind} limit is {limit}, current count is {count}, "
                    f"requested {requested} additional"
                ),
            )

    def _plannedWitnessBackends(self, *, session: SessionRecord) -> list[Any]:
        if session.witness_backend_ids:
            if len(session.witness_backend_ids) != session.witness_count:
                logger.error(
                    "Session witness backend selection does not match witness count",
                )
                raise BootError(
                    "Session witness backend selection does not match the configured witness count.",
                    status_code=503,
                )
            logger.debug(
                "Witness backends already selected for session",
            )
            return [self._witnessBackend(backend_id) for backend_id in session.witness_backend_ids]

        backends = self._selectWitnessBackends(
            count=session.witness_count,
            seed=session.session_id,
        )
        session.witness_backend_ids = [backend.id for backend in backends]
        session.updated_at = nowIso()
        self.ctx.store.saveSession(session)
        logger.info(
            f"Witness backends selected for session {session.session_id}"
        )
        return backends

    def _selectWitnessBackends(self, *, count: int, seed: str) -> list[Any]:
        ordered = sorted(self.ctx.config.witness_backends, key=lambda backend: backend.id)
        if count > len(ordered):
            logger.error(
                f"Witness backend selection failed due to insufficient backends: {count} requested but only {len(ordered)} available"
            )
            raise BootError(
                f"Witness profile requires {count} backends but only {len(ordered)} are configured.",
                status_code=503,
            )
        if count <= 0:
            return []

        digest = blake2b(seed.encode("utf-8"), digest_size=8).digest()
        start = int.from_bytes(digest, "big") % len(ordered)
        return [ordered[(start + index) % len(ordered)] for index in range(count)]

    def _witnessBackend(self, backend_id: str) -> Any:
        for backend in self.ctx.config.witness_backends:
            if backend.id == backend_id:
                return backend
        raise BootError(f"Witness backend '{backend_id}' is not configured.", status_code=503)

    def _witnessClient(self, backend_id: str, *, witness_boots=None) -> Any:
        boots = self.ctx.witness_boots if witness_boots is None else witness_boots
        client = boots.get(backend_id)
        if client is None:
            raise BootError(f"Witness backend '{backend_id}' is not configured.", status_code=503)
        return client

    def _witnessClientForRecord(self, record, *, witness_boots=None) -> Any:
        boots = self.ctx.witness_boots if witness_boots is None else witness_boots
        if record.backend_id:
            return self._witnessClient(record.backend_id, witness_boots=boots)

        if record.boot_url:
            for backend in self.ctx.config.witness_backends:
                if backend.boot_url == record.boot_url:
                    return self._witnessClient(backend.id, witness_boots=boots)

        public_url = (record.url or "").rstrip("/")
        if public_url:
            matches = [
                backend for backend in self.ctx.config.witness_backends if backend.public_url == public_url
            ]
            if len(matches) == 1:
                return self._witnessClient(matches[0].id, witness_boots=boots)

        if record.public_host:
            matches = [
                backend
                for backend in self.ctx.config.witness_backends
                if parsePublicUrl(backend.public_url) == (record.public_host, record.public_port)
            ]
            if len(matches) == 1:
                return self._witnessClient(matches[0].id, witness_boots=boots)

        if len(boots) == 1:
            return next(iter(boots.values()))

        raise BootError(
            f"No witness backend matches stored routing data for witness '{record.eid}'.",
            status_code=503,
        )

    def teardownSessionResourcesDo(
        self,
        *,
        session: SessionRecord,
        account=None,
        tymth,
        tock: float = 0.0,
    ):
        operations = self._sessionResourceDeleteOps(session=session, account=account)
        logger.info(
            f"Session resource teardown started for session {session.session_id}"
        )

        # Delete each hosted resource cooperatively while keeping the sync error semantics.
        yield from self._deleteHostedResourceOpsDo(
            operations,
            session=session,
            account=account,
            context=f"session {session.session_id} teardown",
            tymth=tymth,
            tock=tock,
        )
        logger.info(
            f"Session resources teardown completed for session {session.session_id}"
        )

    def teardownAccountResourcesDo(
        self,
        *,
        account_aid: str,
        account=None,
        tymth,
        tock: float = 0.0,
    ):
        sessions = self.ctx.store.listSessionsForAccount(account_aid)
        operations = self._accountResourceDeleteOps(
            account_aid=account_aid,
            account=account,
            sessions=sessions,
        )
        logger.info(
            f"Resources teardown started for account AID {account_aid}"
        )

        # Delete account resources cooperatively while collecting every BootError before retrying.
        yield from self._deleteHostedResourceOpsDo(
            operations,
            account=account,
            context=f"account {account_aid} resource teardown",
            tymth=tymth,
            tock=tock,
        )

        # Clear account bindings
        if account is not None:
            account.watcher_eid = ""
            account.witness_eids = []
            account.session_id = ""
            self.ctx.store.saveAccount(account)

        logger.info(f"Resources teardown completed for account AID {account_aid}")

    def deleteAccountDo(
        self,
        *,
        account_aid: str,
        account=None,
        witness_boots=None,
        watcher_boot=None,
        tymth,
        tock: float = 0.0,
    ):
        sessions = self.ctx.store.listSessionsForAccount(account_aid)
        operations = self._accountResourceDeleteOps(
            account_aid=account_aid,
            account=account,
            sessions=sessions,
        )
        logger.info(
            f"Account deletion started for account AID {account_aid}"
        )
        # Delete hosted resources cooperatively before removing local account/session rows.
        yield from self._deleteHostedResourceOpsDo(
            operations,
            account=account,
            context=f"account {account_aid} deletion",
            witness_boots=witness_boots,
            watcher_boot=watcher_boot,
            tymth=tymth,
            tock=tock,
        )

        self.ctx.store.deleteBindingsForPrincipal(account_aid)
        self.ctx.store.deleteAccount(account_aid)
        for session in sessions:
            self.ctx.store.deleteSession(session.session_id)
        logger.info(
            f"Account deletion completed for account AID {account_aid}"
            f" with {len(sessions)} sessions, {self._operationCount(operations, 'watcher')} watchers,"
            f" and {self._operationCount(operations, 'witness')} witnesses deleted"
        )

    def _sessionResourceDeleteOps(
        self,
        *,
        session: SessionRecord,
        account=None,
    ) -> list[tuple[str, str]]:
        watcher_ids = self._collectSessionResourceIDs(kind="watcher", session=session, account=account)
        witness_ids = self._collectSessionResourceIDs(kind="witness", session=session, account=account)
        return self._resourceDeleteOps(watcher_ids=watcher_ids, witness_ids=witness_ids)

    def _accountResourceDeleteOps(
        self,
        *,
        account_aid: str,
        account=None,
        sessions: list[SessionRecord],
    ) -> list[tuple[str, str]]:
        watcher_ids = self._collectAccountResourceIDs(
            kind="watcher",
            account_aid=account_aid,
            account=account,
            sessions=sessions,
        )
        witness_ids = self._collectAccountResourceIDs(
            kind="witness",
            account_aid=account_aid,
            account=account,
            sessions=sessions,
        )
        return self._resourceDeleteOps(watcher_ids=watcher_ids, witness_ids=witness_ids)

    @staticmethod
    def _resourceDeleteOps(*, watcher_ids: list[str], witness_ids: list[str]) -> list[tuple[str, str]]:
        return [("watcher", eid) for eid in watcher_ids] + [
            ("witness", eid) for eid in witness_ids
        ]

    @staticmethod
    def _operationCount(operations: list[tuple[str, str]], kind: str) -> int:
        return sum(1 for operation_kind, _eid in operations if operation_kind == kind)

    def _deleteHostedResourceOpsDo(
        self,
        operations: list[tuple[str, str]],
        *,
        session: SessionRecord | None = None,
        account=None,
        context: str,
        witness_boots=None,
        watcher_boot=None,
        tymth,
        tock: float = 0.0,
    ):
        # Collect every per-resource BootError before retrying the aggregate task.
        errors: list[BootError] = []
        for kind, eid in operations:
            try:
                yield from self.deleteHostedResourceDo(
                    kind=kind,
                    eid=eid,
                    session=session,
                    account=account,
                    tolerate_missing_remote=True,
                    witness_boots=witness_boots,
                    watcher_boot=watcher_boot,
                    tymth=tymth,
                    tock=tock,
                )
            except BootError as exc:
                errors.append(exc)
                logger.warning(f"{context} failed to delete {kind} resource {eid}: {exc}")

        self._raiseResourceDeleteErrors(errors, context=context)

    @staticmethod
    def _raiseResourceDeleteErrors(errors: list[BootError], *, context: str) -> None:
        if not errors:
            return
        first = errors[0]
        detail = "; ".join(str(error) for error in errors)
        logger.warning(f"{context} completed with errors ({len(errors)}): {detail}")
        raise BootError(detail, status_code=first.status_code)

    def _collectAccountResourceIDs(
        self,
        *,
        kind: str,
        account_aid: str,
        account=None,
        sessions: list[SessionRecord] | None = None,
    ) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        sessions = sessions or []

        candidates: list[str] = []
        if kind == "witness":
            if account is not None:
                candidates.extend(account.witness_eids)
            for session in sessions:
                candidates.extend(session.witness_eids)
        else:
            if account is not None and account.watcher_eid:
                candidates.append(account.watcher_eid)
            for session in sessions:
                if session.watcher_eid:
                    candidates.append(session.watcher_eid)

        candidates.extend(
            record.eid
            for record in self.ctx.store.listResourcesForAccount(
                kind=kind,
                account_aid=account_aid,
            )
        )
        for session in sessions:
            candidates.extend(
                record.eid
                for record in self.ctx.store.listResourcesForSession(
                    kind=kind,
                    session_id=session.session_id,
                )
            )

        for eid in candidates:
            if not eid or eid in seen:
                continue
            seen.add(eid)
            ordered.append(eid)

        return ordered

    def _collectSessionResourceIDs(
        self,
        *,
        kind: str,
        session: SessionRecord,
        account=None,
    ) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()

        candidates: list[str] = []
        if kind == "witness":
            candidates.extend(session.witness_eids)
            if account is not None:
                candidates.extend(account.witness_eids)
        else:
            if session.watcher_eid:
                candidates.append(session.watcher_eid)
            if account is not None and account.watcher_eid:
                candidates.append(account.watcher_eid)

        candidates.extend(
            record.eid
            for record in self.ctx.store.listResourcesForSession(
                kind=kind,
                session_id=session.session_id,
            )
        )

        for eid in candidates:
            if not eid or eid in seen:
                continue
            seen.add(eid)
            ordered.append(eid)

        return ordered

    def deleteHostedResourceDo(
        self,
        *,
        kind: str,
        eid: str,
        session: SessionRecord | None = None,
        account=None,
        tolerate_missing_remote: bool = False,
        witness_boots=None,
        watcher_boot=None,
        tymth,
        tock: float = 0.0,
    ):
        if not eid:
            return

        record = self.ctx.store.getResource(kind, eid)
        if record is None:
            logger.info(
                f"Resource record not found for deletion for {kind} with EID {eid}"
            )
            self._removeResourceFromOwners(kind=kind, eid=eid, session=session, account=account)
            self._persistOwnerState(session=session, account=account)
            return
        if kind == "witness":
            boots = self.cleanup_witness_boots if witness_boots is None else witness_boots
            delete_remote = self._witnessClientForRecord(
                record,
                witness_boots=boots,
            ).deleteWitnessDo
        else:
            boot = self.cleanup_watcher_boot if watcher_boot is None else watcher_boot
            if boot is None:
                raise BootError("Cleanup watcher boot client is not configured.", status_code=503)
            delete_remote = boot.deleteWatcherDo
        logger.info(
            f"Resource deletion started for {kind} with EID {eid}",
        )
        try:
            yield from delete_remote(eid, tymth=tymth, tock=tock)
        except BootError as exc:
            if not (tolerate_missing_remote and exc.status_code == 404):
                logger.warning(
                    f"Resource deletion failed for {kind} with EID {eid}: {exc}",
                )
                raise
            logger.info(
                f"Resource not found during deletion for {kind} with EID {eid}, but tolerated: {exc}",
            )
        self._deleteLocalHostedResource(kind=kind, eid=eid, session=session, account=account)
        logger.info(
            f"Resource deletion completed for {kind} with EID {eid}"
        )

    def _deleteLocalHostedResource(
        self,
        *,
        kind: str,
        eid: str,
        session: SessionRecord | None = None,
        account=None,
    ) -> None:
        self.ctx.store.deleteResource(kind, eid)
        self._removeResourceFromOwners(kind=kind, eid=eid, session=session, account=account)
        self._persistOwnerState(session=session, account=account)

    def _removeResourceFromOwners(
        self,
        *,
        kind: str,
        eid: str,
        session: SessionRecord | None = None,
        account=None,
    ) -> None:
        if session is not None:
            if kind == "witness":
                session.witness_eids = [item for item in session.witness_eids if item != eid]
            elif session.watcher_eid == eid:
                session.watcher_eid = ""

        if account is not None:
            if kind == "witness":
                account.witness_eids = [item for item in account.witness_eids if item != eid]
            elif account.watcher_eid == eid:
                account.watcher_eid = ""

    def _persistOwnerState(
        self,
        *,
        session: SessionRecord | None = None,
        account=None,
    ) -> None:
        if session is not None:
            session.updated_at = nowIso()
            self.ctx.store.saveSession(session)
        if account is not None:
            self.ctx.store.saveAccount(account)
