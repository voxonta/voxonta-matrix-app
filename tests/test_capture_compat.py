"""The real capture service's poller, against this service, over HTTP.

The service API exists to be read by `nextcloud-talk-capture`, which was built
against the Nextcloud app. Matching that app field by field is easy to get
almost right; this runs the capture service's own client and monitor against
it, so "almost" fails here instead of on a live call.

Needs the capture package (0.3.2 or later, where calls carry a platform);
skipped without it.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestServer
from conftest import BOT, ROOM, SERVER, Clock, FakeMatrix, device, make_config, member, room_name

from voxonta_matrix.http import service_app
from voxonta_matrix.integration import Integration
from voxonta_matrix.store import MemoryStore

app_client = pytest.importorskip("talk_capture.app_client")
app_monitor = pytest.importorskip("talk_capture.app_monitor")
call_info = pytest.importorskip("talk_capture.call_info")
if not hasattr(call_info.CallInfo, "__dataclass_fields__") or \
        "platform" not in call_info.CallInfo.__dataclass_fields__:
    pytest.skip("capture package predates platform-aware calls", allow_module_level=True)

ALICE = f"@alice:{SERVER}"


def test_the_capture_service_sees_the_call_and_everything_it_needs():
    async def go():
        clock = Clock()
        integration = Integration(
            make_config(), MemoryStore(),
            FakeMatrix({ROOM: [room_name("Дейли"), member(BOT), member(ALICE, name="Алиса")]}),
            clock=clock)
        await integration.start()
        await integration.on_transaction("t1", [device(ALICE, "LAPTOP")])

        server = TestServer(service_app(integration, "service-token"))
        await server.start_server()
        try:
            client = app_client.AppClient(str(server.make_url("")), "service-token")
            settings = await client.fetch_settings()
            config = SimpleNamespace(nc_user="", nc_password="", hpb_url="", hpb_secret="",
                                     transcript_folder="", analysis_folder="",
                                     transcript_user="", included_rooms=[], excluded_rooms=[],
                                     poll_interval=5)
            app_client.overlay_app_settings(config, settings)

            started = []

            async def on_started(info):
                started.append(info)

            async def on_ended(token):
                pass

            monitor = app_monitor.AppCallMonitor(config, client, on_started, on_ended)
            await monitor._process(await client.fetch_calls())
            await asyncio.sleep(0)
            await client.heartbeat("0.3.2")
            return settings, config, started
        finally:
            await server.close()

    settings, config, started = asyncio.run(go())
    # Nothing from Nextcloud lands on the capture side's config.
    assert settings.nc_user is None and settings.hpb_url is None
    assert config.nc_user == "" and config.included_rooms == []

    [info] = started
    assert info.room_token == ROOM
    assert info.platform == "livekit"
    assert info.source == "element-call"
    assert info.room_type == 1  # Alice and the bot: one person, as RoomKind counts it
    assert info.recording is True
    assert info.details["livekit"]["token"]
    assert info.details["members"][f"{ALICE}:LAPTOP"]["name"] == "Алиса"
