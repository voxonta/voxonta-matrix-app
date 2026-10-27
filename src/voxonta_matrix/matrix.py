"""Acting as the bot on the homeserver's client API.

An application service needs no login: the registration's as_token is the
bot's credential, and requests without `user_id` act as the registration's
sender, which is the bot.
"""
from __future__ import annotations

import json
import logging
from urllib.parse import quote

import aiohttp

logger = logging.getLogger(__name__)


class MatrixError(Exception):
    def __init__(self, status: int, errcode: str, message: str = "") -> None:
        super().__init__(f"HTTP {status} {errcode}: {message}".strip())
        self.status = status
        self.errcode = errcode


class MatrixClient:
    def __init__(self, homeserver_url: str, as_token: str, bot_user_id: str,
                 session: aiohttp.ClientSession) -> None:
        self._base = homeserver_url.rstrip("/") + "/_matrix/client/v3"
        self._headers = {"Authorization": f"Bearer {as_token}"}
        self.bot_user_id = bot_user_id
        self._session = session

    async def _call(self, method: str, path: str, body: dict | None = None):
        async with self._session.request(method, self._base + path, json=body,
                                         headers=self._headers) as resp:
            text = await resp.text()
            try:
                data = json.loads(text) if text else {}
            except ValueError:
                data = {}
            if resp.status >= 400:
                data = data if isinstance(data, dict) else {}
                raise MatrixError(resp.status, str(data.get("errcode", "")),
                                  str(data.get("error", "") or text[:200]))
            return data

    async def ensure_bot(self, displayname: str) -> None:
        """Make sure the bot account exists and is called what it should be.

        Registering an account that exists is answered with M_USER_IN_USE,
        which is the normal case after the first start.
        """
        localpart = self.bot_user_id.lstrip("@").split(":", 1)[0]
        try:
            await self._call("POST", "/register", {
                "type": "m.login.application_service",
                "username": localpart,
                "inhibit_login": True,
            })
            logger.info("registered %s", self.bot_user_id)
        except MatrixError as e:
            if e.errcode != "M_USER_IN_USE":
                raise
        user = quote(self.bot_user_id, safe="")
        try:
            current = (await self._call("GET", f"/profile/{user}/displayname")).get(
                "displayname")
        except MatrixError:
            current = None
        if current != displayname:
            await self._call("PUT", f"/profile/{user}/displayname",
                             {"displayname": displayname})

    async def joined_rooms(self) -> list[str]:
        return list((await self._call("GET", "/joined_rooms")).get("joined_rooms") or [])

    async def join(self, room_id: str) -> None:
        await self._call("POST", f"/rooms/{quote(room_id, safe='')}/join", {})

    async def leave(self, room_id: str) -> None:
        await self._call("POST", f"/rooms/{quote(room_id, safe='')}/leave", {})

    async def room_state(self, room_id: str) -> list[dict]:
        state = await self._call("GET", f"/rooms/{quote(room_id, safe='')}/state")
        return state if isinstance(state, list) else []
