"""Asking the Voxonta gateway what a meeting produced, and taking it.

The same four requests the Nextcloud app makes (GatewayClient.php), over the
gateway's HTTP API: a meeting's status and file list, one file by its digest,
the meetings still holding files nobody took, and the acknowledgement that
lets the gateway retire them. The key is the installation's gateway key; it
decides whose meetings these are.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from urllib.parse import quote

import aiohttp

logger = logging.getLogger(__name__)


class GatewayUnavailable(Exception):
    """No answer worth acting on — ask again later."""


class UnknownMeeting(Exception):
    """The gateway has no such meeting: the call's audio never reached it."""


@dataclass(frozen=True)
class Artifact:
    name: str
    kind: str      # transcript | summary | analysis | stats
    sha256: str
    size: int = 0


@dataclass(frozen=True)
class MeetingState:
    status: str    # transcribing | transcribed | analyzing | complete | failed
    final: bool
    detail: str
    artifacts: tuple[Artifact, ...]


class GatewayClient:
    def __init__(self, base_url: str, token: str, session: aiohttp.ClientSession) -> None:
        self._base = base_url.rstrip("/") + "/v1/meetings"
        self._headers = {"Authorization": f"Bearer {token}"}
        self._session = session

    async def _get(self, path: str, params=None) -> aiohttp.ClientResponse:
        try:
            resp = await self._session.get(self._base + path, params=params,
                                           headers=self._headers)
            await resp.read()
            return resp
        except (TimeoutError, aiohttp.ClientError) as e:
            raise GatewayUnavailable(str(e) or type(e).__name__) from e

    async def meeting(self, session_id: str) -> MeetingState:
        resp = await self._get("/" + quote(session_id, safe=""))
        if resp.status == 404:
            raise UnknownMeeting(session_id)
        if resp.status != 200:
            raise GatewayUnavailable(f"HTTP {resp.status}")
        try:
            body = await resp.json()
        except (ValueError, aiohttp.ContentTypeError) as e:
            raise GatewayUnavailable("unreadable answer") from e
        artifacts = tuple(
            Artifact(str(a.get("name", "")), str(a.get("kind", "")), str(a.get("sha256", "")),
                     int(a.get("size") or 0))
            for a in body.get("artifacts") or [] if isinstance(a, dict) and a.get("sha256"))
        return MeetingState(str(body.get("status") or "unknown"), bool(body.get("final")),
                            str(body.get("detail") or ""), artifacts)

    async def artifact(self, session_id: str, sha256: str) -> bytes:
        resp = await self._get(f"/{quote(session_id, safe='')}/artifacts/{quote(sha256, safe='')}")
        if resp.status != 200:
            raise GatewayUnavailable(f"HTTP {resp.status} for {sha256[:12]}")
        content = await resp.read()
        # The name is the digest, so checking costs nothing and keeps a cut-off
        # body from becoming a file someone opens.
        if hashlib.sha256(content).hexdigest() != sha256:
            raise GatewayUnavailable(f"{sha256[:12]} arrived corrupted")
        return content

    async def undelivered(self, sources: list[str]) -> list[dict]:
        """Meetings of these platforms with files not yet acknowledged.

        Empty when the gateway cannot be asked: a sweep that fails is one that
        runs again in a minute, not a reason to stop anything else.
        """
        try:
            resp = await self._get("/undelivered", params=[("source", s) for s in sources])
            if resp.status != 200:
                raise GatewayUnavailable(f"HTTP {resp.status}")
            body = await resp.json()
        except (GatewayUnavailable, ValueError, aiohttp.ContentTypeError) as e:
            logger.debug("could not ask the gateway what is undelivered: %s", e)
            return []
        rows = body.get("meetings") if isinstance(body, dict) else None
        return [r for r in rows or [] if isinstance(r, dict) and r.get("session_id")]

    async def ack(self, session_id: str, digests: list[str]) -> None:
        """Advisory: a failure costs a longer retention and nothing else."""
        if not digests:
            return
        try:
            async with self._session.post(
                    f"{self._base}/{quote(session_id, safe='')}/ack",
                    json={"sha256": digests}, headers=self._headers) as resp:
                if resp.status != 200:
                    logger.info("ack of %s answered HTTP %d", session_id[:8], resp.status)
        except (TimeoutError, aiohttp.ClientError) as e:
            logger.info("could not acknowledge %s: %s", session_id[:8], e)
