"""User-facing widget API, separate from capture and application-service tokens."""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
import jwt
from aiohttp import web

from voxonta_matrix.room_access import AccessUnavailable

ROOT = Path(__file__).with_name("web")


def widget_app(widget, config, session):
    origin = (
        f"{urlsplit(config.widget_public_url).scheme}://{urlsplit(config.widget_public_url).netloc}"
    )

    @web.middleware
    async def errors(request, handler):
        try:
            response = await handler(request)
            if "/api/" in request.path:
                response.headers["Cache-Control"] = "no-store"
            return response
        except PermissionError as e:
            return web.json_response({"error": str(e)}, status=403)
        except (ValueError, KeyError, TypeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        except (AccessUnavailable, aiohttp.ClientError, TimeoutError) as e:
            return web.json_response({"error": str(e)}, status=503)

    def claims(request):
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        try:
            return jwt.decode(
                token,
                config.widget_session_secret,
                algorithms=["HS256"],
                audience="voxonta-widget",
                options={"require": ["sub", "room", "exp"]},
            )
        except jwt.InvalidTokenError:
            raise web.HTTPUnauthorized(
                text='{"error":"Войдите через Element повторно"}', content_type="application/json"
            ) from None

    async def api(request):
        if request.headers.get("Origin") != origin:
            raise PermissionError("Недопустимый origin")
        body = await request.json()
        action = request.match_info["action"]
        if action == "session":
            oid = body["openid"]
            if not isinstance(oid, dict):
                raise ValueError("Неверный OpenID")
            if oid.get("matrix_server_name") != config.server_name:
                raise PermissionError("Homeserver не разрешён")
            # Never resolve a homeserver or URL supplied by the browser.
            async with session.get(
                config.homeserver_url + "/_matrix/federation/v1/openid/userinfo",
                params={"access_token": oid["access_token"]},
                timeout=aiohttp.ClientTimeout(total=8),
                allow_redirects=False,
            ) as resp:
                if resp.status != 200:
                    raise PermissionError("OpenID не подтверждён")
                user = (await resp.json())["sub"]
            room_id = body["room_id"]
            room = await widget.authorize(room_id, user)
            now = int(time.time())
            token = jwt.encode(
                {
                    "sub": user,
                    "room": room_id,
                    "aud": "voxonta-widget",
                    "iat": now,
                    "exp": now + 900,
                },
                config.widget_session_secret,
                algorithm="HS256",
            )
            return web.json_response({"token": token, "room_name": room.display_name("")})
        who = claims(request)
        room, user = who["room"], who["sub"]
        if action == "status":
            return web.json_response(await widget.status(room, user))
        if action == "command":
            answer = await widget.command(
                room, user, body["action"], body["request_id"], body.get("value")
            )
            return web.json_response(answer, status=202, headers={"Cache-Control": "no-store"})
        if action == "result":
            content = await widget.result(room, user, body["session_id"], body["kind"])
            return web.Response(
                body=content,
                content_type="text/markdown",
                charset="utf-8",
                headers={
                    "Cache-Control": "no-store",
                    "Content-Disposition": 'attachment; filename="meeting.md"',
                },
            )
        raise web.HTTPNotFound()

    async def static(request):
        if request.path == "/widget":
            raise web.HTTPFound("/widget/" + ("?" + request.query_string if request.query_string else ""))
        name = request.match_info.get("name", "index.html") or "index.html"
        if name not in {"index.html", "widget.js", "widget.css", "api.min.js"}:
            raise web.HTTPNotFound()
        response = web.FileResponse(ROOT / name)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors "
            + " ".join(config.widget_parent_origins)
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    app = web.Application(middlewares=[errors], client_max_size=32768)
    app.router.add_post("/widget/api/{action}", api)

    async def public_config(request):
        return web.json_response(
            {"parent_origins": list(config.widget_parent_origins)},
            headers={"Cache-Control": "no-store"},
        )

    app.router.add_get("/widget/config", public_config)
    app.router.add_get("/widget", static)
    app.router.add_get("/widget/{name:.*}", static)
    return app
