"""The two HTTP faces of the integration, on two ports.

* The application service API, for the homeserver: transactions of events, and
  the existence checks it makes for users in our namespace. It stays inside the
  homeserver's network.
* The service API, for the capture service: the same paths, fields and key as
  the Nextcloud app's (/apps/voxonta/api/v1/service/...), so one capture build
  serves both. The capture service runs elsewhere, so this one is published —
  behind TLS and an address allowlist that the deployment provides.

Separate ports so that publishing one can never expose the other.
"""
from __future__ import annotations

import hmac
import logging
import time

from aiohttp import web

from voxonta_matrix.integration import TryAgain

logger = logging.getLogger(__name__)

SERVICE_PREFIX = "/apps/voxonta/api/v1/service"


def _same(given: str, expected: str) -> bool:
    return bool(given) and hmac.compare_digest(given.encode(), expected.encode())


# ── for the homeserver ────────────────────────────────────────────────

def appservice_app(integration, hs_token: str) -> web.Application:
    def authorized(request: web.Request) -> web.Response | None:
        """None when the homeserver proved itself, else the refusal to send.

        The token arrives as a bearer header; older homeservers put it in the
        query string, which is still accepted.
        """
        header = request.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else request.query.get(
            "access_token", "")
        if not token:
            return web.json_response({"errcode": "M_UNAUTHORIZED"}, status=401)
        if not _same(token, hs_token):
            return web.json_response({"errcode": "M_FORBIDDEN"}, status=403)
        return None

    async def transaction(request: web.Request) -> web.Response:
        if (refused := authorized(request)) is not None:
            return refused
        body = await request.json()
        events = body.get("events") or []
        try:
            await integration.on_transaction(request.match_info["txn_id"], events)
        except TryAgain as e:
            # Not acknowledged: the homeserver backs off and sends it again.
            logger.warning("transaction %s deferred: %s", request.match_info["txn_id"],
                           e.__cause__)
            return web.json_response({"errcode": "M_UNKNOWN"}, status=503)
        return web.json_response({})

    async def user(request: web.Request) -> web.Response:
        # Asked when someone addresses a user in our namespace. The bot exists
        # (it is registered on start); nobody else in the namespace does.
        if (refused := authorized(request)) is not None:
            return refused
        if request.match_info["user_id"] == integration.bot:
            return web.json_response({})
        return web.json_response({"errcode": "M_NOT_FOUND"}, status=404)

    async def room_alias(request: web.Request) -> web.Response:
        if (refused := authorized(request)) is not None:
            return refused
        return web.json_response({"errcode": "M_NOT_FOUND"}, status=404)

    async def ping(request: web.Request) -> web.Response:
        if (refused := authorized(request)) is not None:
            return refused
        return web.json_response({})

    app = web.Application(client_max_size=16 * 1024 * 1024)
    app.router.add_put("/_matrix/app/v1/transactions/{txn_id}", transaction)
    app.router.add_get("/_matrix/app/v1/users/{user_id}", user)
    app.router.add_get("/_matrix/app/v1/rooms/{alias}", room_alias)
    app.router.add_post("/_matrix/app/v1/ping", ping)
    return app


# ── for the capture service ───────────────────────────────────────────

def service_app(integration, service_token: str, clock=time.time) -> web.Application:
    seen: dict[str, object] = {}

    def authorized(request: web.Request) -> bool:
        # Both header forms, as the Nextcloud app accepts them: some proxies
        # strip Authorization, and the capture service sends both.
        header = request.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else request.headers.get(
            "X-Transcription-Token", "")
        return _same(token, service_token)

    def refuse() -> web.Response:
        return web.json_response({"message": "unauthorised"}, status=401)

    def settings() -> dict:
        # What the Nextcloud app sends, minus what only Nextcloud has. Rooms
        # are empty because scope is decided here: only rooms the bot was
        # invited to ever appear in /calls.
        return {
            "enabled": True,
            "publish_to_chat": True,
            "rooms": [],
            "folders": {},
            "nextcloud": None,
            "signaling": None,
        }

    async def config(request: web.Request) -> web.Response:
        if not authorized(request):
            return refuse()
        return web.json_response(settings())

    async def calls(request: web.Request) -> web.Response:
        if not authorized(request):
            return refuse()
        return web.json_response({"calls": integration.calls_listing()})

    async def heartbeat(request: web.Request) -> web.Response:
        if not authorized(request):
            return refuse()
        try:
            body = await request.json()
        except ValueError:
            body = dict(await request.post())
        version = str(body.get("version") or "")
        if version and version != seen.get("version"):
            logger.info("capture service %s checked in", version)
        seen.update(version=version, note=str(body.get("note") or ""), at=clock())
        return web.json_response(settings())

    async def health(request: web.Request) -> web.Response:
        # For the deployment's own monitoring; says nothing a caller could use.
        at = seen.get("at")
        return web.json_response({
            "status": "ok",
            "rooms": len(integration.rooms),
            "calls": len(integration.calls),
            "capture_seen_s_ago": None if at is None else int(clock() - at),
        })

    app = web.Application()
    app.router.add_get(SERVICE_PREFIX + "/config", config)
    app.router.add_get(SERVICE_PREFIX + "/calls", calls)
    app.router.add_post(SERVICE_PREFIX + "/heartbeat", heartbeat)
    app.router.add_get("/healthz", health)
    return app
