"""Real Synapse automation without a widget session; gateway artifacts are synthetic."""

from __future__ import annotations

import asyncio
import runpy
import time
from pathlib import Path
from urllib.parse import quote

import aiohttp
from conftest import FakeGateway, make_config

from voxonta_matrix.room_access import RoomAccess
from voxonta_matrix.widget import Widget
from voxonta_matrix.widget_store import MemoryWidgetStore


async def main():
    ctx = runpy.run_path(str(Path(__file__).with_name("synapse_widget_e2e.py")))
    ok, alice, bob = ctx["ok"], ctx["a"], ctx["b"]
    token = alice["access_token"]
    room = ok(
        "POST",
        "/_matrix/client/v3/createRoom",
        {"preset": "private_chat", "invite": [bob["user_id"]]},
        token,
    )["room_id"]
    path = "/_matrix/client/v3/rooms/" + quote(room, safe="")
    ok("POST", path + "/join", {}, bob["access_token"])
    config = make_config(
        widget_auto_transcribe_default=True, widget_auto_publish_default=True, call_end_grace_s=0.05
    )
    config.widget_public_url = "https://voxonta.example/widget"
    store, gateway = MemoryWidgetStore(), FakeGateway()
    async with aiohttp.ClientSession() as session:
        access = RoomAccess(
            "http://127.0.0.1:18770/_synapse/client/voxonta/widget", ctx["T"], session
        )
        widget = Widget(config, store, access, gateway)
        await widget.tick()
        content = {
            "application": "m.call",
            "scope": "m.room",
            "call_id": "",
            "device_id": "AUTO",
            "membershipID": alice["user_id"] + ":AUTO",
            "created_ts": int(time.time() * 1000),
            "expires": 3600000,
            "focus_active": {"type": "livekit"},
        }
        ok("PUT", path + "/state/org.matrix.msc3401.call.member/auto", content, token)
        await widget.tick()
        calls = [c for c in await widget.calls_listing() if c["token"] == room]
        assert len(calls) == 1 and calls[0]["recording"]
        sid = calls[0]["session_id"]
        gateway.put(
            sid,
            "complete",
            {
                "01_Executive_Summary.md": ("summary", b"Summary"),
                "09_Enriched_Transcript.md": ("analysis", b"Transcript"),
            },
        )
        ok("PUT", path + "/state/org.matrix.msc3401.call.member/auto", {}, token)
        await widget.tick()
        await asyncio.sleep(0.1)
        await widget.tick()
        await widget.tick()
        meeting = store.state["rooms"][room]["meetings"][0]
        assert meeting["delivery_status"] == "posted"
        assert await widget.result(room, alice["user_id"], sid, "summary") == b"Summary"
        snapshot = await access.request("snapshot", room_id=room)
        assert (
            len(
                [
                    e
                    for e in snapshot["state"]
                    if e["type"] == "m.room.member" and e["content"].get("membership") == "join"
                ]
            )
            == 2
        )
    print("Automatic Matrix flow: PASS (discovery/start/end/publication, no UI session or bot)")


if __name__ == "__main__":
    asyncio.run(main())
