"""Run the integration: two HTTP listeners and a timer.

The timer is what ends calls. Devices that drop off without saying so only stop
counting when their membership expires, and the grace period runs out in
silence too; nothing would push an event at those moments.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from importlib.metadata import PackageNotFoundError, version

import aiohttp
from aiohttp import web

from voxonta_matrix.config import Config
from voxonta_matrix.http import appservice_app, service_app
from voxonta_matrix.integration import Integration
from voxonta_matrix.matrix import MatrixClient
from voxonta_matrix.store import PostgresStore

logger = logging.getLogger("voxonta_matrix")

try:
    VERSION = version("voxonta-matrix-app")
except PackageNotFoundError:  # run from a checkout without installing
    VERSION = "dev"
TICK_S = 5.0


async def _tick(integration: Integration, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await integration.reconcile()
        except Exception:
            logger.exception("reconciling calls failed")
        try:
            await asyncio.wait_for(stop.wait(), TICK_S)
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
        matrix = MatrixClient(config.homeserver_url, config.as_token,
                              config.bot_user_id, session)
        integration = Integration(config, store, matrix)
        # Listen before reading the rooms: the homeserver may already be
        # retrying transactions, and those wait until start() is done rather
        # than being refused and backed off for minutes.
        runners = []
        for app, port in ((appservice_app(integration, config.hs_token), config.appservice_port),
                          (service_app(integration, config.service_token), config.service_port)):
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            await web.TCPSite(runner, "0.0.0.0", port).start()
            runners.append(runner)
        logger.info("voxonta-matrix-app %s: homeserver API on :%d, service API on :%d",
                    VERSION, config.appservice_port, config.service_port)

        await integration.start()
        ticker = asyncio.create_task(_tick(integration, stop))
        await stop.wait()
        logger.info("stopping")
        ticker.cancel()
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
