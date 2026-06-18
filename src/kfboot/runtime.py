from __future__ import annotations

from hio.core import http
from keri import help
from keri.app import httping, indirecting

from kfboot.app import Context, create_app
from kfboot.boot_client import HioBootClient
from kfboot.config import Config
from kfboot.operating import BootOperationDoer, BootOperationProcessor
from kfboot.sweeping import CleanupDoer

logger = help.ogler.getLogger(__name__)


def setup(config: Config | None = None, *, temp: bool = False):
    """Build the Falcon app, service context, and root HIO doers."""

    app, ctx = create_app(config=config, temp=temp)
    return app, ctx, build_doers(app, ctx)


def build_doers(app, ctx: Context) -> list:
    """Return the HIO doers that run the service."""

    server = indirecting.createHttpServer(
        host=ctx.config.host,
        port=ctx.config.port,
        app=app,
    )
    service_doers = [http.ServerDoer(server=server)]

    operation_clienter = httping.Clienter()
    operation_witness_boots = {
        backend.id: HioBootClient(
            backend.boot_url,
            clienter=operation_clienter,
            timeout=ctx.config.boot_api_timeout_seconds,
        )
        for backend in ctx.config.witness_backends
    }
    operation_watcher_boot = HioBootClient(
        ctx.config.wat_boot_url,
        clienter=operation_clienter,
        timeout=ctx.config.boot_api_timeout_seconds,
    )
    service_doers.append(
        BootOperationDoer(
            store=ctx.store,
            witness_boots=operation_witness_boots,
            watcher_boot=operation_watcher_boot,
            processor=BootOperationProcessor(provisioner=ctx.exchanger.provisioner),
            clienter=operation_clienter,
            failure_max_attempts=ctx.config.operation_failure_max_attempts,
        )
    )

    if ctx.cleanup.expected_running:
        clienter = httping.Clienter()
        cleanup_witness_boots = {
            backend.id: HioBootClient(
                backend.boot_url,
                clienter=clienter,
                timeout=ctx.config.boot_api_timeout_seconds,
            )
            for backend in ctx.config.witness_backends
        }
        cleanup_watcher_boot = HioBootClient(
            ctx.config.wat_boot_url,
            clienter=clienter,
            timeout=ctx.config.boot_api_timeout_seconds,
        )
        ctx.exchanger.provisioner.configureCleanupBootClients(
            witness_boots=cleanup_witness_boots,
            watcher_boot=cleanup_watcher_boot,
        )
        service_doers.append(
            CleanupDoer(
                expirer=ctx.exchanger.expirer,
                clienter=clienter,
                interval=ctx.config.cleanup_interval_seconds,
                batch_size=ctx.config.cleanup_batch_size,
                time_budget_seconds=ctx.config.cleanup_time_budget_seconds,
                state=ctx.cleanup,
            )
        )
    elif not ctx.config.cleanup_runner_enabled:
        logger.info("Periodic cleanup sweeper disabled because cleanup_runner_enabled is false")
    else:
        logger.info("Periodic cleanup sweeper disabled because cleanup_interval_seconds <= 0")

    return service_doers
