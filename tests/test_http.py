"""The two HTTP faces: who may call them, and what they answer.

The service API is read by a capture service built for the Nextcloud app, so it
has to look like that app's — the same paths, the same key headers, the same
answer to a wrong key. A difference shows up as a capture service that polls
happily and never sees a call.
"""
from __future__ import annotations

import asyncio

from aiohttp.test_utils import TestClient, TestServer
from conftest import BOT, ROOM, SERVER, Clock, FakeMatrix, device, make_config, member, room_name

from voxonta_matrix.http import SERVICE_PREFIX, appservice_app, service_app
from voxonta_matrix.integration import Integration
from voxonta_matrix.store import MemoryStore

ALICE = f"@alice:{SERVER}"


def integration(state=None):
    matrix = FakeMatrix({ROOM: [room_name("Дейли"), member(BOT), member(ALICE, name="Алиса")]}
                        if state is None else state)
    return Integration(make_config(), MemoryStore(), matrix, clock=Clock())


async def client(app) -> TestClient:
    c = TestClient(TestServer(app))
    await c.start_server()
    return c


def run(coro):
    return asyncio.run(coro)


# ── the capture service's side ────────────────────────────────────────

def test_a_wrong_or_missing_key_is_refused_like_the_nextcloud_app_does():
    async def go():
        c = await client(service_app(integration(), "service-token"))
        try:
            out = []
            for headers in ({}, {"Authorization": "Bearer nope"},
                            {"X-Transcription-Token": "nope"}):
                r = await c.get(SERVICE_PREFIX + "/calls", headers=headers)
                out.append((r.status, await r.json()))
            return out
        finally:
            await c.close()

    assert run(go()) == [(401, {"message": "unauthorised"})] * 3


def test_both_key_headers_work():
    # Some proxies strip Authorization; the capture service sends both.
    async def go():
        c = await client(service_app(integration(), "service-token"))
        try:
            a = await c.get(SERVICE_PREFIX + "/config",
                            headers={"Authorization": "Bearer service-token"})
            b = await c.get(SERVICE_PREFIX + "/config",
                            headers={"X-Transcription-Token": "service-token"})
            return a.status, b.status, await a.json()
        finally:
            await c.close()

    a, b, body = run(go())
    assert (a, b) == (200, 200)
    # Nothing Nextcloud-shaped to hand over; empty rooms, because scope is the
    # bot's invitations, decided here.
    assert body == {"enabled": True, "publish_to_chat": True, "rooms": [], "folders": {},
                    "nextcloud": None, "signaling": None}


def test_calls_lists_the_live_call():
    async def go():
        i = integration()
        await i.start()
        await i.on_transaction("t1", [device(ALICE, "LAPTOP")])
        c = await client(service_app(i, "service-token"))
        try:
            r = await c.get(SERVICE_PREFIX + "/calls",
                            headers={"Authorization": "Bearer service-token"})
            return await r.json()
        finally:
            await c.close()

    [call] = run(go())["calls"]
    assert call["token"] == ROOM and call["platform"] == "livekit"


def test_a_heartbeat_is_answered_with_the_settings_and_shows_in_health():
    async def go():
        i = integration()
        await i.start()
        c = await client(service_app(i, "service-token", clock=Clock()))
        try:
            before = await (await c.get("/healthz")).json()
            r = await c.post(SERVICE_PREFIX + "/heartbeat", json={"version": "0.3.2"},
                             headers={"Authorization": "Bearer service-token"})
            after = await (await c.get("/healthz")).json()
            return before, r.status, await r.json(), after
        finally:
            await c.close()

    before, status, body, after = run(go())
    assert before["capture_seen_s_ago"] is None
    assert status == 200 and body["enabled"] is True
    assert after == {"status": "ok", "rooms": 1, "calls": 0, "capture_seen_s_ago": 0}


# ── the homeserver's side ─────────────────────────────────────────────

def test_the_homeserver_has_to_prove_itself():
    async def go():
        c = await client(appservice_app(integration(), "hs-token"))
        try:
            out = []
            for headers, query in (({}, ""), ({"Authorization": "Bearer nope"}, ""),
                                   ({"Authorization": "Bearer hs-token"}, ""),
                                   ({}, "?access_token=hs-token")):
                r = await c.post("/_matrix/app/v1/ping" + query, json={}, headers=headers)
                out.append(r.status)
            return out
        finally:
            await c.close()

    assert run(go()) == [401, 403, 200, 200]


def test_a_transaction_is_applied_and_acknowledged():
    async def go():
        i = integration()
        await i.start()
        c = await client(appservice_app(i, "hs-token"))
        try:
            r = await c.put("/_matrix/app/v1/transactions/1",
                            json={"events": [device(ALICE, "LAPTOP")]},
                            headers={"Authorization": "Bearer hs-token"})
            return r.status, await r.json(), i.calls_listing()
        finally:
            await c.close()

    status, body, calls = run(go())
    assert (status, body) == (200, {})
    assert len(calls) == 1


def test_a_transaction_that_hit_a_passing_failure_is_left_for_a_retry():
    from voxonta_matrix.matrix import MatrixError

    async def go():
        i = integration(state={})
        await i.start()
        i.matrix.fail_join = MatrixError(503, "", "")
        c = await client(appservice_app(i, "hs-token"))
        try:
            r = await c.put("/_matrix/app/v1/transactions/1", json={"events": [
                member(BOT, "invite", room="!new:example.org", sender=ALICE)]},
                headers={"Authorization": "Bearer hs-token"})
            return r.status, await i.store.txn_seen("1")
        finally:
            await c.close()

    assert run(go()) == (503, False)


def test_only_the_bot_exists_in_our_namespace():
    async def go():
        c = await client(appservice_app(integration(), "hs-token"))
        try:
            auth = {"Authorization": "Bearer hs-token"}
            bot = await c.get(f"/_matrix/app/v1/users/{BOT}", headers=auth)
            other = await c.get(f"/_matrix/app/v1/users/@voxonta-other:{SERVER}", headers=auth)
            alias = await c.get(f"/_matrix/app/v1/rooms/%23x:{SERVER}", headers=auth)
            return bot.status, other.status, alias.status
        finally:
            await c.close()

    assert run(go()) == (200, 404, 404)
