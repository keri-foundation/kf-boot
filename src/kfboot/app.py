from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import falcon
from keri.app.habbing import Habery

from kfboot.boot_client import BootClient, BootError
from kfboot.config import Config
from kfboot.store import Store, make_record
from kfboot.onboarding import OnboardingEnd
from kfboot.boot_exchanger import BootExchanger

def _page_int(req: falcon.Request, name: str, default: int) -> int:
    value = req.get_param(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise falcon.HTTPBadRequest(
            title="Invalid query parameter",
            description=f"{name} must be an integer",
        ) from exc


def _request_json(req: falcon.Request) -> Any:
    try:
        return req.get_media()
    except Exception as exc:
        raise falcon.HTTPBadRequest(
            title="Invalid request body",
            description=str(exc),
        ) from exc


def _boot_error(exc: BootError) -> falcon.HTTPError:
    if exc.status_code == 400:
        return falcon.HTTPBadRequest(
            title="Boot API rejected request",
            description=str(exc),
        )
    if exc.status_code == 404:
        return falcon.HTTPNotFound(
            title="Upstream resource not found",
            description=str(exc),
        )
    if exc.status_code == 409:
        return falcon.HTTPError(
            status="409 Conflict",
            title="Boot API conflict",
            description=str(exc),
        )
    return falcon.HTTPBadGateway(
        title="Boot API call failed",
        description=str(exc),
    )


def _capacity_error(kind: str, limit: int, count: int, requested: int) -> falcon.HTTPError:
    return falcon.HTTPError(
        status="409 Conflict",
        title="Capacity exceeded",
        description=(
            f"{kind} limit is {limit}, current count is {count}, "
            f"requested {requested} additional"
        ),
    )


@dataclass
class Context:
    config: Config
    store: Store
    witness_boot: BootClient
    watcher_boot: BootClient
    habery: Habery
    hostHab: Any


class HealthEnd:
    def on_get(self, _req: falcon.Request, rep: falcon.Response) -> None:
        rep.status = falcon.HTTP_204


def _account_option(code: str) -> dict[str, Any]:
    parts = code.lower().split("-of-")
    if len(parts) != 2:
        return {"code": code}

    try:
        toad = int(parts[0])
        witness_count = int(parts[1])
    except ValueError:
        return {"code": code}

    return {
        "code": code,
        "witness_count": witness_count,
        "toad": toad,
    }


class BootstrapConfigEnd:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def on_get(self, _req: falcon.Request, rep: falcon.Response) -> None:
        rep.media = {
            "bootstrap": {
                "account_options": [
                    _account_option(code)
                    for code in self.ctx.config.bootstrap_account_options
                ],
                "watcher_required": self.ctx.config.bootstrap_watcher_required,
                "accounts_per_ip": self.ctx.config.bootstrap_accounts_per_ip,
                "aids_per_ip": self.ctx.config.bootstrap_aids_per_ip,
            },
            "region": {
                "id": self.ctx.config.region_id,
                "name": self.ctx.config.region_name,
            },
        }


class CapacityEnd:
    def __init__(self, ctx: Context, kind: str):
        self.ctx = ctx
        self.kind = kind

    def on_get(self, _req: falcon.Request, rep: falcon.Response) -> None:
        count = self.ctx.store.count_resources(self.kind)
        limit = (
            self.ctx.config.witness_limit
            if self.kind == "witness"
            else self.ctx.config.watcher_limit
        )
        key = "witopnet" if self.kind == "witness" else "watopnet"
        rep.media = {
            "regions": [
                {
                    "id": self.ctx.config.region_id,
                    "name": self.ctx.config.region_name,
                }
            ],
            key: {"limit": limit, "count": count},
            "available": max(limit - count, 0),
        }


class WitnessCollectionEnd:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def on_get(self, req: falcon.Request, rep: falcon.Response) -> None:
        auth = get_auth(req)
        rep.media = self.ctx.store.list_resources(
            kind="witness",
            principal=auth.principal,
            is_admin=auth.is_admin,
            page=_page_int(req, "page", 0),
            page_size=max(_page_int(req, "page_size", 10), 1),
            filter_term=req.get_param("filter"),
            order=req.get_param_as_list("order"),
        )

    def on_post(self, req: falcon.Request, rep: falcon.Response) -> None:
        auth = get_auth(req)
        body = _request_json(req)
        docs = body if isinstance(body, list) else [body]
        if not docs:
            raise falcon.HTTPBadRequest(
                title="Invalid request body",
                description="At least one witness document is required",
            )
        count = self.ctx.store.count_resources("witness")
        if count + len(docs) > self.ctx.config.witness_limit:
            raise _capacity_error(
                "witness",
                self.ctx.config.witness_limit,
                count,
                len(docs),
            )

        records = []
        for idx, doc in enumerate(docs, start=1):
            _require_mapping(doc)
            cid = _required_str(doc, "cid")
            if not self.ctx.store.allow_controller(auth.principal, cid, auth.is_admin):
                raise falcon.HTTPForbidden(
                    title="Forbidden",
                    description=f"{auth.principal} cannot manage controller {cid}",
                )

            try:
                created = self.ctx.witness_boot.create_witness(cid)
            except BootError as exc:
                raise _boot_error(exc)

            name = _optional_str(doc, "name") or f"witness-{idx}"
            record = make_record(
                kind="witness",
                eid=created["eid"],
                cid=created["cid"],
                principal=auth.principal,
                name=name,
                identifier_alias=_optional_str(doc, "identifier_alias"),
                region_id=_optional_str(doc, "region_id") or self.ctx.config.region_id,
                region_name=self.ctx.config.region_name,
                public_url=self.ctx.config.wit_public_url,
                oobis=created.get("oobis", []),
            )
            self.ctx.store.add_resource(record)
            records.append(record.to_api())

        rep.status = falcon.HTTP_201
        rep.media = records


class WitnessResourceEnd:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def on_get(self, req: falcon.Request, rep: falcon.Response, eid: str) -> None:
        record = self._authorized_record(req, eid)
        rep.media = record.to_api()

    def on_delete(self, req: falcon.Request, rep: falcon.Response, eid: str) -> None:
        self._authorized_record(req, eid)
        try:
            self.ctx.witness_boot.delete_witness(eid)
        except BootError as exc:
            raise _boot_error(exc)
        self.ctx.store.delete_resource("witness", eid)
        rep.status = falcon.HTTP_204

    def _authorized_record(self, req: falcon.Request, eid: str):
        auth = get_auth(req)
        record = self.ctx.store.get_resource("witness", eid)
        if record is None:
            raise falcon.HTTPNotFound(title="Witness not found")
        if not self.ctx.store.allow_controller(auth.principal, record.cid, auth.is_admin):
            raise falcon.HTTPForbidden(title="Forbidden")
        return record


class WatcherCollectionEnd:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def on_get(self, req: falcon.Request, rep: falcon.Response) -> None:
        auth = get_auth(req)
        rep.media = self.ctx.store.list_resources(
            kind="watcher",
            principal=auth.principal,
            is_admin=auth.is_admin,
            page=_page_int(req, "page", 0),
            page_size=max(_page_int(req, "page_size", 10), 1),
            filter_term=req.get_param("filter"),
            order=req.get_param_as_list("order"),
        )

    def on_post(self, req: falcon.Request, rep: falcon.Response) -> None:
        auth = get_auth(req)
        doc = _request_json(req)
        _require_mapping(doc)
        cid = _required_str(doc, "cid")
        if not self.ctx.store.allow_controller(auth.principal, cid, auth.is_admin):
            raise falcon.HTTPForbidden(
                title="Forbidden",
                description=f"{auth.principal} cannot manage controller {cid}",
            )
        count = self.ctx.store.count_resources("watcher")
        if count + 1 > self.ctx.config.watcher_limit:
            raise _capacity_error(
                "watcher",
                self.ctx.config.watcher_limit,
                count,
                1,
            )

        try:
            created = self.ctx.watcher_boot.create_watcher(cid, _optional_str(doc, "oobi"))
        except BootError as exc:
            raise _boot_error(exc)

        record = make_record(
            kind="watcher",
            eid=created["eid"],
            cid=created["cid"],
            principal=auth.principal,
            name=_optional_str(doc, "name") or "watcher",
            identifier_alias=_optional_str(doc, "identifier_alias"),
            region_id=_optional_str(doc, "region_id") or self.ctx.config.region_id,
            region_name=self.ctx.config.region_name,
            public_url=self.ctx.config.wat_public_url,
            oobis=created.get("oobis", []),
        )
        self.ctx.store.add_resource(record)

        rep.status = falcon.HTTP_201
        rep.media = record.to_api()


class WatcherResourceEnd:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def on_get(self, req: falcon.Request, rep: falcon.Response, eid: str) -> None:
        record = self._authorized_record(req, eid)
        rep.media = record.to_api()

    def on_delete(self, req: falcon.Request, rep: falcon.Response, eid: str) -> None:
        self._authorized_record(req, eid)
        try:
            self.ctx.watcher_boot.delete_watcher(eid)
        except BootError as exc:
            raise _boot_error(exc)
        self.ctx.store.delete_resource("watcher", eid)
        rep.status = falcon.HTTP_204

    def _authorized_record(self, req: falcon.Request, eid: str):
        auth = get_auth(req)
        record = self.ctx.store.get_resource("watcher", eid)
        if record is None:
            raise falcon.HTTPNotFound(title="Watcher not found")
        if not self.ctx.store.allow_controller(auth.principal, record.cid, auth.is_admin):
            raise falcon.HTTPForbidden(title="Forbidden")
        return record


class WatcherStatusEnd:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def on_get(self, req: falcon.Request, rep: falcon.Response, eid: str) -> None:
        auth = get_auth(req)
        record = self.ctx.store.get_resource("watcher", eid)
        if record is None:
            raise falcon.HTTPNotFound(title="Watcher not found")
        if not self.ctx.store.allow_controller(auth.principal, record.cid, auth.is_admin):
            raise falcon.HTTPForbidden(title="Forbidden")
        try:
            rep.media = self.ctx.watcher_boot.watcher_status(eid)
        except BootError as exc:
            raise _boot_error(exc)


def create_app(config: Config | None = None, temp=False) -> tuple[falcon.App, Context]:
    config = config or Config.from_env()
    store = Store(config.db_path)
    
    # Create the server’s KERI environment
    habery = Habery(name="boot", temp=temp)

    # Create the server’s AID (hostHab)
    hostHab = habery.makeHab(name="boot")

    ctx = Context(
        config=config,
        store=store,
        witness_boot=BootClient(config.wit_boot_url),
        watcher_boot=BootClient(config.wat_boot_url),
        habery=habery,
        hostHab=hostHab,
    )

    # Create the boot exchanger
    ctx.exchanger = BootExchanger(ctx)
    # Wire BootExchanger into Kevery and Parser
    ctx.habery.kvy.exc = ctx.exchanger
    ctx.habery.psr.exc = ctx.exchanger
    
    # Falcon App
    app = falcon.App()

    # Public discovery
    app.add_route("/health", HealthEnd())
    app.add_route("/bootstrap/config", BootstrapConfigEnd(ctx))

    app.add_route("/capacity/witopnet", CapacityEnd(ctx, "witness"))
    app.add_route("/capacity/watopnet", CapacityEnd(ctx, "watcher"))
    app.add_route("/witnesses", WitnessCollectionEnd(ctx))
    app.add_route("/witnesses/{eid}", WitnessResourceEnd(ctx))
    app.add_route("/watchers", WatcherCollectionEnd(ctx))
    app.add_route("/watchers/{eid}", WatcherResourceEnd(ctx))
    app.add_route("/watchers/{eid}/status", WatcherStatusEnd(ctx))

    # CESR ingress endpoint
    app.add_route("/onboarding", OnboardingEnd(ctx))
    return app, ctx


def _require_mapping(doc: Any) -> None:
    if isinstance(doc, dict):
        return
    raise falcon.HTTPBadRequest(
        title="Invalid request body",
        description="Request body must be a JSON object",
    )


def _required_str(doc: dict[str, Any], key: str) -> str:
    value = _optional_str(doc, key)
    if not value:
        raise falcon.HTTPBadRequest(
            title="Invalid request body",
            description=f"{key} is required",
        )
    return value


def _optional_str(doc: dict[str, Any], key: str) -> str:
    value = doc.get(key, "")
    return value.strip() if isinstance(value, str) else ""
