import asyncio
from dataclasses import replace

import jwt
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from conftest import ROOM, make_config
from test_widget import ALICE, BOB, setup

from voxonta_matrix.widget_http import widget_app

ORIGIN = "https://voxonta.example"


def test_widget_http_checks_origin_openid_scope_and_current_membership():
    async def go():
        import aiohttp

        s = setup()

        async def identity(request):
            if request.query.get("access_token") == "valid-openid":
                return web.json_response({"sub": ALICE})
            raise web.HTTPForbidden()

        hs = web.Application()
        hs.router.add_get("/_matrix/federation/v1/openid/userinfo", identity)
        hs_server = TestServer(hs)
        await hs_server.start_server()
        config = replace(
            make_config(),
            homeserver_url=str(hs_server.make_url("")).rstrip("/"),
            widget_public_url=ORIGIN + "/widget",
            widget_session_secret="a-local-session-test-secret-long-enough",
            widget_parent_origins=("https://element.example",),
        )
        async with aiohttp.ClientSession() as session:
            client = TestClient(TestServer(widget_app(s.w, config, session)))
            await client.start_server()
            try:
                body = {
                    "room_id": ROOM,
                    "openid": {"matrix_server_name": "example.org", "access_token": "valid-openid"},
                }
                assert (await client.post("/widget/api/session", json=body)).status == 403
                response = await client.post(
                    "/widget/api/session", json=body, headers={"Origin": ORIGIN}
                )
                assert response.status == 200
                token = (await response.json())["token"]
                headers = {"Origin": ORIGIN, "Authorization": "Bearer " + token}
                assert (
                    await client.post("/widget/api/status", json={}, headers=headers)
                ).status == 200
                forged = jwt.encode(
                    {"sub": BOB, "room": ROOM, "exp": 9999999999, "aud": "voxonta-widget"},
                    "wrong-key-that-is-at-least-32-characters",
                    algorithm="HS256",
                )
                assert (
                    await client.post(
                        "/widget/api/status",
                        json={},
                        headers={**headers, "Authorization": "Bearer " + forged},
                    )
                ).status == 401
                # HTTP body room_id cannot override the signed room scope.
                response = await client.post(
                    "/widget/api/command",
                    json={"room_id": "!other:example.org", "action": "start", "request_id": "one"},
                    headers=headers,
                )
                assert response.status == 202
                assert set(s.db.state["rooms"]) == {ROOM}
                s.access.room.members.pop(ALICE)
                assert (
                    await client.post("/widget/api/status", json={}, headers=headers)
                ).status == 403
                assert (
                    await client.post(
                        "/widget/api/session",
                        json={
                            **body,
                            "openid": {
                                "matrix_server_name": "attacker.example",
                                "access_token": "x",
                            },
                        },
                        headers={"Origin": ORIGIN},
                    )
                ).status == 403
                page = await client.get("/widget/")
                assert page.status == 200
                assert (
                    "frame-ancestors https://element.example"
                    in page.headers["Content-Security-Policy"]
                )
                assert "Voxonta" in await page.text()
                assert (await client.get("/widget/../config.py")).status == 404
            finally:
                await client.close()
        await hs_server.close()

    asyncio.run(go())


def test_old_capture_builds_never_receive_widget_calls():
    async def go():
        from types import SimpleNamespace

        from voxonta_matrix.http import SERVICE_PREFIX, service_app

        s = setup()
        await s.w.command(ROOM, ALICE, "start", "start")
        integration = SimpleNamespace(calls_listing=list)
        client = TestClient(TestServer(service_app(integration, "service-secret", widget=s.w)))
        await client.start_server()
        try:
            headers = {"Authorization": "Bearer service-secret"}
            assert (await (await client.get(SERVICE_PREFIX + "/calls", headers=headers)).json())[
                "calls"
            ] == []
            response = await client.get(
                SERVICE_PREFIX + "/calls",
                headers={**headers, "X-Voxonta-Capture-Protocol": "widget-v1"},
            )
            assert len((await response.json())["calls"]) == 1
        finally:
            await client.close()

    asyncio.run(go())
