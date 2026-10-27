"""What goes over the wire to the gateway and to the homeserver.

Real HTTP against small aiohttp servers that answer like the real ones: the
gateway's /v1/meetings routes (platform's services/brain/src/http_api.py) and
the two client-server endpoints publishing needs.
"""
from __future__ import annotations

import asyncio
import hashlib
from urllib.parse import unquote

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from voxonta_matrix.gateway import GatewayClient, GatewayUnavailable, UnknownMeeting
from voxonta_matrix.matrix import MatrixClient, MatrixError

SID = "a1b2c3d4-0000-4000-8000-000000000000"
BODY = "# Транскрипт\n".encode()
SHA = hashlib.sha256(BODY).hexdigest()


def run(coro):
    return asyncio.run(coro)


async def serve(routes, seen):
    @web.middleware
    async def record(request, handler):
        seen.append((request.method, request.raw_path, dict(request.headers),
                     await request.read()))
        return await handler(request)

    app = web.Application(middlewares=[record])
    app.add_routes(routes)
    server = TestServer(app)
    await server.start_server()
    return server


# ── the gateway ───────────────────────────────────────────────────────

def gateway_routes(*, damaged=False):
    async def meeting(request):
        if request.match_info["sid"] != SID:
            return web.json_response({"error": "unknown meeting"}, status=404)
        return web.json_response({
            "session_id": SID, "status": "complete", "final": True, "detail": "",
            "artifacts": [{"name": "2026-10-27 - Дейли.md", "kind": "transcript",
                           "sha256": SHA, "media_type": "text/markdown; charset=utf-8",
                           "size": len(BODY)}]})

    async def artifact(request):
        return web.Response(body=BODY[:-1] if damaged else BODY,
                            content_type="text/markdown", charset="utf-8")

    async def undelivered(request):
        return web.json_response({"meetings": [
            {"session_id": SID, "source": "element-call", "room_token": "!r:x.org",
             "room_name": "Дейли"}, {"bad": "row"}]})

    async def ack(request):
        return web.json_response({"acked": len((await request.json())["sha256"])})

    return [web.get("/v1/meetings/undelivered", undelivered),
            web.get("/v1/meetings/{sid}", meeting),
            web.get("/v1/meetings/{sid}/artifacts/{sha}", artifact),
            web.post("/v1/meetings/{sid}/ack", ack)]


def test_the_gateway_is_asked_like_the_nextcloud_app_asks_it():
    seen = []

    async def go():
        server = await serve(gateway_routes(), seen)
        async with aiohttp.ClientSession() as session:
            gw = GatewayClient(str(server.make_url("/")) + "/", "gw-key", session)
            state = await gw.meeting(SID)
            body = await gw.artifact(SID, SHA)
            rows = await gw.undelivered(["element-call", ""])
            await gw.ack(SID, [SHA])
        await server.close()
        return state, body, rows

    state, body, rows = run(go())
    assert (state.status, state.final) == ("complete", True)
    assert [(a.name, a.kind, a.sha256) for a in state.artifacts] == [
        ("2026-10-27 - Дейли.md", "transcript", SHA)]
    assert body == BODY
    assert [r["session_id"] for r in rows] == [SID]          # the malformed row is dropped
    assert all(h["Authorization"] == "Bearer gw-key" for _, _, h, _ in seen)
    paths = [(m, p) for m, p, _, _ in seen]
    assert paths == [("GET", f"/v1/meetings/{SID}"),
                     ("GET", f"/v1/meetings/{SID}/artifacts/{SHA}"),
                     # Repeated, not source[]=: the gateway reads it that way.
                     ("GET", "/v1/meetings/undelivered?source=element-call&source="),
                     ("POST", f"/v1/meetings/{SID}/ack")]
    assert seen[-1][3] == b'{"sha256": ["' + SHA.encode() + b'"]}'


def test_an_unknown_meeting_is_told_apart_from_a_gateway_that_is_down():
    async def go():
        server = await serve(gateway_routes(), [])
        async with aiohttp.ClientSession() as session:
            gw = GatewayClient(str(server.make_url("/")), "k", session)
            with pytest.raises(UnknownMeeting):
                await gw.meeting("other")
            await server.close()
            with pytest.raises(GatewayUnavailable):
                await gw.meeting(SID)
            assert await gw.undelivered(["element-call"]) == []
            await gw.ack(SID, [SHA])   # advisory: does not raise

    run(go())


def test_a_damaged_file_is_refused():
    async def go():
        server = await serve(gateway_routes(damaged=True), [])
        async with aiohttp.ClientSession() as session:
            gw = GatewayClient(str(server.make_url("/")), "k", session)
            with pytest.raises(GatewayUnavailable, match="corrupted"):
                await gw.artifact(SID, SHA)
        await server.close()

    run(go())


# ── the homeserver ────────────────────────────────────────────────────

def homeserver_routes(*, refuse=False):
    async def send(request):
        if refuse:
            return web.json_response({"errcode": "M_FORBIDDEN", "error": "not in room"},
                                     status=403)
        return web.json_response({"event_id": "$e1"})

    async def upload(request):
        return web.json_response({"content_uri": "mxc://x.org/abc"})

    return [web.put("/_matrix/client/v3/rooms/{room}/send/m.room.message/{txn}", send),
            web.post("/_matrix/media/v3/upload", upload)]


def test_a_message_and_a_file_go_where_the_spec_puts_them():
    seen = []

    async def go():
        server = await serve(homeserver_routes(), seen)
        async with aiohttp.ClientSession() as session:
            mx = MatrixClient(str(server.make_url("/")), "as-key", "@bot:x.org", session)
            event = await mx.send("!r:x.org", {"msgtype": "m.text", "body": "hi"},
                                  txn_id=f"voxonta.{SID}.announcement")
            uri = await mx.upload(BODY, "2026-10-27 Дейли — итоги.md", "text/markdown")
        await server.close()
        return event, uri

    event, uri = run(go())
    assert (event, uri) == ("$e1", "mxc://x.org/abc")
    (m1, p1, h1, _), (m2, p2, h2, b2) = seen
    # aiohttp sends ! and : in a path as they are, which the spec allows.
    assert m1 == "PUT"
    assert unquote(p1) == f"/_matrix/client/v3/rooms/!r:x.org/send/m.room.message/voxonta.{SID}" \
                          ".announcement"
    assert m2 == "POST" and p2.startswith("/_matrix/media/v3/upload?filename=2026-10-27")
    assert h2["Content-Type"] == "text/markdown" and b2 == BODY
    assert h1["Authorization"] == h2["Authorization"] == "Bearer as-key"


def test_a_refusal_carries_its_status():
    async def go():
        server = await serve(homeserver_routes(refuse=True), [])
        async with aiohttp.ClientSession() as session:
            mx = MatrixClient(str(server.make_url("/")), "k", "@bot:x.org", session)
            with pytest.raises(MatrixError) as e:
                await mx.send("!r:x.org", {}, txn_id="t")
        await server.close()
        return e.value

    error = run(go())
    assert (error.status, error.errcode) == (403, "M_FORBIDDEN")
