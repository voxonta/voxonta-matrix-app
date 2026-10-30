"""Run the integration: two HTTP listeners and two timers.

One timer ends calls. Devices that drop off without saying so only stop
counting when their membership expires, and the grace period runs out in
silence too; nothing would push an event at those moments. The other collects
finished meetings from the gateway and publishes them.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from importlib.metadata import PackageNotFoundError, version

import aiohttp
from aiohttp import web

from voxonta_matrix.config import Config
from voxonta_matrix.delivery import Delivery
from voxonta_matrix.gateway import GatewayClient
from voxonta_matrix.http import appservice_app, service_app
from voxonta_matrix.integration import Integration
from voxonta_matrix.matrix import MatrixClient
from voxonta_matrix.room_access import RoomAccess
from voxonta_matrix.store import PostgresStore
from voxonta_matrix.widget import Widget
from voxonta_matrix.widget_http import widget_app
from voxonta_matrix.widget_store import PostgresWidgetStore

logger = logging.getLogger("voxonta_matrix")

try:
    VERSION = version("voxonta-matrix-app")
except PackageNotFoundError:  # run from a checkout without installing
    VERSION = "dev"
TICK_S = 5.0


async def _every(seconds: float, job, what: str, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await job()
        except Exception:
            logger.exception("%s failed", what)
        try:
            await asyncio.wait_for(stop.wait(), seconds)
        except TimeoutError:
            pass


async def run(config: Config) -> None:
    store = PostgresStore(config.database_url)
    await store.open()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        matrix = MatrixClient(config.homeserver_url, config.as_token, config.bot_user_id, session)
        gateway = GatewayClient(config.gateway_url, config.gateway_token, session)
        integration = Integration(config, store, matrix, gateway=gateway)
        delivery = Delivery(
            store,
            gateway,
            matrix,
            is_live=lambda sid: any(c.session_id == sid for c in integration.calls.values()),
        )
        widget = None
        apps = []
        if config.widget_enabled:
            widget_store = PostgresWidgetStore(store._pool)
            await widget_store.open()
            widget = Widget(
                config,
                widget_store,
                RoomAccess(config.widget_adapter_url, config.widget_adapter_token, session),
                gateway,
                legacy=integration,
            )
            integration.widget = delivery.widget = widget
            apps.append((widget_app(widget, config, session), config.widget_port))
        # Listen before reading the rooms: the homeserver may already be
        # retrying transactions, and those wait until start() is done rather
        # than being refused and backed off for minutes.
        runners = []
        if config.bot_mode_enabled:
            apps.append((appservice_app(integration, config.hs_token), config.appservice_port))
        apps.append(
            (service_app(integration, config.service_token, widget=widget), config.service_port)
        )
        for app, port in apps:
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            await web.TCPSite(runner, "0.0.0.0", port).start()
            runners.append(runner)
        logger.info(
            "voxonta-matrix-app %s: homeserver API on :%d, service API on :%d",
            VERSION,
            config.appservice_port,
            config.service_port,
        )

        if not config.bot_mode_enabled and (
            await store.load_calls() or await store.pending_meetings()
        ):
            raise SystemExit(
                "Finish bot-mode calls and pending deliveries before disabling bot mode"
            )
        await integration.start()
        timers = [
            asyncio.create_task(_every(TICK_S, integration.reconcile, "reconciling calls", stop)),
        ]
        if config.bot_mode_enabled:
            timers.append(
                asyncio.create_task(
                    _every(config.collect_interval_s, delivery.tick, "collecting results", stop)
                )
            )
        if widget:

            async def refresh_widget():
                await widget.tick(collect=False)

            timers.append(asyncio.create_task(_every(1, refresh_widget, "widget meetings", stop)))
            timers.append(
                asyncio.create_task(
                    _every(config.collect_interval_s, widget.collect, "widget results", stop)
                )
            )
        await stop.wait()
        logger.info("stopping")
        for timer in timers:
            timer.cancel()
        for runner in runners:
            await runner.cleanup()
    await store.close()


def main() -> None:
    config = Config.from_env()
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
