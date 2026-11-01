"""Real Synapse automation without a widget session; gateway artifacts are synthetic."""

from __future__ import annotations

import asyncio
import runpy
import time
from pathlib import Path
from urllib.parse import quote

import aiohttp
from conftest import FakeGateway, make_config

from voxonta_matrix.room_access import AccessUnavailable, RoomAccess
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
        # Ordinary top-level file messages are discoverable by the room Files view.
        messages = ok("GET", path + "/messages?dir=b&limit=100", token=token)["chunk"]
        files = [e["content"] for e in messages if e["content"].get("org.voxonta.meeting") == sid]
        assert len(files) == 2, files
        assert all(f["msgtype"] == "m.file" and "m.relates_to" not in f for f in files)
        assert files[0]["filename"].endswith(" — Расшифровка.md")  # newest first
        assert files[1]["filename"].endswith(" — Саммари.md")
        for f, expected in zip(files, (b"Transcript", b"Summary")):
            server, media = f["url"].removeprefix("mxc://").split("/", 1)
            async with session.get(
                "http://127.0.0.1:18770/_matrix/client/v1/media/download/" + server + "/" + media,
                headers={"Authorization": "Bearer " + token},
            ) as response:
                assert response.status == 200
                assert await response.read() == expected
        await access.publish(room, alice["user_id"], sid, {
            "summary": {"content": "U3VtbWFyeQ=="},
            "transcript": {"content": "VHJhbnNjcmlwdA=="},
        })
        repeated = ok("GET", path + "/messages?dir=b&limit=100", token=token)["chunk"]
        assert len([e for e in repeated if e["content"].get("org.voxonta.meeting") == sid]) == 2
        # Model a lost response after sending only the first file. Retrying the
        # whole pair must complete the second without duplicating the first.
        partial_sid = sid + "-partial"
        await access.publish(room, alice["user_id"], partial_sid, {
            "summary": {"content": "U3VtbWFyeQ=="},
        })
        await access.publish(room, alice["user_id"], partial_sid, {
            "summary": {"content": "U3VtbWFyeQ=="},
            "transcript": {"content": "VHJhbnNjcmlwdA=="},
        })
        messages = ok("GET", path + "/messages?dir=b&limit=100", token=token)["chunk"]
        partial = [e for e in messages if e["content"].get("org.voxonta.meeting") == partial_sid]
        assert len(partial) == 2
        assert all(e["content"]["msgtype"] == "m.file" for e in partial)
        # Validate the entire pair before sending anything.
        try:
            await access.publish(room, alice["user_id"], sid + "-invalid", {
                "summary": {"content": "U3VtbWFyeQ=="}, "transcript": {"content": "%%%"},
            })
        except AccessUnavailable:
            pass
        else:
            raise AssertionError("invalid file accepted")
        messages = ok("GET", path + "/messages?dir=b&limit=100", token=token)["chunk"]
        assert not any(e["content"].get("org.voxonta.meeting") == sid + "-invalid" for e in messages)
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
