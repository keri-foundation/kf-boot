from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import falcon
from keri import help
from keri.app.configing import Configer
from keri.app.habbing import Habery
from keri.core import Parser
from keri.core.eventing import Kevery
from keri.core.kraming import Kramer
from keri.kering import Vrsn_1_0

from kfboot.boot_exchanger import BootContext, BootExchanger
from kfboot.config import Config
from kfboot.onboarding import CesrSurfaceEnd
from kfboot.store import Store, nowIso
from kfboot.sweeping import CleanupState


logger = help.ogler.getLogger(__name__)

DEFAULT_KRAM_CONFIG = {
    "kram": {
        "enabled": True,
        "denials": [],
        "caches": {
            "~": ["1000", "5000", "5000", "86400000", "5000", "5000", "86400000"],
            "exn": ["1000", "5000", "5000", "86400000", "5000", "5000", "86400000"],
        },
    }
}


class StaticConfig:
    def __init__(self, data: dict[str, Any]):
        self.data = data

    def get(self):
        return self.data


@dataclass
class Context:
    config: Config
    store: Store
    witness_boots: dict[str, Any]
    watcher_boot: Any | None
    habery: Habery
    host_hab: Any
    kramer: Kramer | None
    kvy: Kevery | None
    parser: Parser | None
    exchanger: BootExchanger | None
    cleanup: CleanupState

    def close(self, *, clear: bool = False) -> None:
        logger.info(
            "App Context is closing",
        )
        self.store.close()
        self.habery.close(clear=clear)
        logger.info(
            "App context closed",
        )


class HealthEnd:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def on_get(self, _req: falcon.Request, rep: falcon.Response) -> None:
        current = nowIso()
        cleanup_state = self.ctx.cleanup
        configured = bool(self.ctx.config.cleanup_runner_enabled)
        expected_running = cleanup_state.expected_running
        running = cleanup_state.is_running
        runner_state = cleanup_state.snapshot(now=current)
        backlog = self.ctx.store.cleanupBacklogSnapshot(now=current)

        # Expose cleanup queue pressure so operators can tell the difference
        # between "no work pending" and "work is piling up behind the sweeper".
        cleanup = {
            "configured": configured,
            "expected_running": expected_running,
            "running": running,
            **backlog,
            **runner_state,
        }

        # Create list of reasons
        reasons: list[str] = []

        # Check if the runner should be running but is not
        if expected_running and not running:
            reasons.append("runner_not_running")

        if reasons:
            rep.status = falcon.HTTP_503
            cleanup["reason"] = reasons[0]
            cleanup["reasons"] = reasons
            rep.media = {"status": "degraded", "cleanup": cleanup}
            return

        rep.media = {"status": "ok", "cleanup": cleanup}


class BootstrapConfigEnd:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def on_get(self, _req: falcon.Request, rep: falcon.Response) -> None:
        rep.media = {
            "bootstrap": {
                "account_options": [
                    self.ctx.config.account_option(code)
                    for code in self.ctx.config.bootstrap_account_options
                ],
                "watcher_required": self.ctx.config.bootstrap_watcher_required,
            },
            "region": {
                "id": self.ctx.config.region_id,
                "name": self.ctx.config.region_name,
            },
            "surfaces": {
                "onboarding": self.ctx.config.onboarding_surface,
                "account": self.ctx.config.account_surface,
            },
        }


def create_app(config: Config | None = None, *, temp: bool = False) -> tuple[falcon.App, Context]:
    config = config or Config.from_env()
    logger.info(
        "App config loaded and app is starting"
    )
    # Instantiate store
    store = Store(
        config.db_path,
        session_ttl_seconds=config.session_ttl_seconds,
        account_ttl_seconds=config.account_ttl_seconds,
        closed_session_retention_seconds=config.closed_session_retention_seconds or 0.0,
        expired_account_retention_seconds=config.expired_account_retention_seconds,
    )
    cf = Configer(
        name=config.keri_name,
        base="",
        temp=temp,
        reopen=True,
        clear=False,
        headDirPath=config.keri_dir,
    )

    hby = Habery(name=config.keri_name, temp=temp, headDirPath=config.keri_dir, cf=cf)
    host_hab = hby.habByName(config.boot_hab_name)
    if host_hab is None:
        host_hab = hby.makeHab(
            name=config.boot_hab_name,
            transferable=True,
            isith="1",
            icount=1,
            nsith="1",
            ncount=1,
        )

    ctx = Context(
        config=config,
        store=store,
        witness_boots={},
        watcher_boot=None,
        habery=hby,
        host_hab=host_hab,
        kramer=None,
        kvy=None,
        parser=None,
        exchanger=None,
        cleanup=CleanupState(
            enabled=config.cleanup_runner_enabled,
            interval=config.cleanup_interval_seconds,
        ),
    )

    exchanger = BootExchanger(
        BootContext(
            config=config,
            store=store,
            witness_boots=ctx.witness_boots,
            watcher_boot=ctx.watcher_boot,
            host_hab=host_hab,
            habery=hby,
        )
    )
    kramer = Kramer(db=hby.db, cf=StaticConfig(DEFAULT_KRAM_CONFIG))
    kvy = Kevery(db=hby.db, lax=False, local=False, rvy=hby.rvy, exc=exchanger, kramer=kramer)
    kvy.registerReplyRoutes(router=hby.rtr)
    parser = Parser(framed=True, kvy=kvy, rvy=hby.rvy, exc=exchanger, local=False, version=Vrsn_1_0)

    hby.exc = exchanger
    hby.kvy = kvy
    hby.psr = parser
    host_hab.kvy = kvy
    host_hab.psr = parser
    host_hab.rvy = hby.rvy
    host_hab.rtr = hby.rtr

    ctx.kramer = kramer
    ctx.kvy = kvy
    ctx.parser = parser
    ctx.exchanger = exchanger

    app = falcon.App()
    app.add_route("/health", HealthEnd(ctx))
    app.add_route("/bootstrap/config", BootstrapConfigEnd(ctx))
    app.add_route(config.onboarding_path, CesrSurfaceEnd(ctx, surface="onboarding"))
    app.add_route(config.account_path, CesrSurfaceEnd(ctx, surface="account"))
    logger.info(
        "App routes registered and ready to serve requests",
    )

    return app, ctx
