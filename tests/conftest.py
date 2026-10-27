"""Fakes for the homeserver, the gateway and a clock, shared by the tests.

Nothing here talks to a network: a call is a handful of state events, the
homeserver is the few client calls the integration makes, and the gateway is
a dict of meetings answering in the shape of its HTTP API.
"""
from __future__ import annotations

import hashlib

import pytest

from voxonta_matrix.config import Config
from voxonta_matrix.gateway import Artifact, GatewayUnavailable, MeetingState, UnknownMeeting
from voxonta_matrix.matrix import MatrixError

SERVER = "example.org"
BOT = f"@voxonta-bot:{SERVER}"
ROOM = f"!daily:{SERVER}"
T0 = 1_793_086_800.0  # 2026-10-27, seconds


def make_config(**overrides) -> Config:
    values = {
        "homeserver_url": "http://homeserver:8008",
        "server_name": SERVER,
        "as_token": "as-token",
        "hs_token": "hs-token",
        "livekit_url": "wss://livekit.example.org",
        "livekit_api_key": "voxonta-key",
        "livekit_api_secret": "a-livekit-secret-long-enough-for-hs256",
        "database_url": "",
        "service_token": "service-token",
        "gateway_url": "https://gateway.example.org",
        "gateway_token": "gateway-token",
        "call_end_grace_s": 90.0,
    }
    values.update(overrides)
    return Config(**values)


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeMatrix:
    """The client calls the integration makes, against rooms held in memory."""

    def __init__(self, state: dict[str, list[dict]] | None = None,
                 timeline: dict[str, list[dict]] | None = None) -> None:
        self.state = {room: list(events) for room, events in (state or {}).items()}
        # Events that are not state — sticky call memberships — oldest first.
        self.timeline = {room: list(events) for room, events in (timeline or {}).items()}
        self.fail_recent: Exception | None = None
        self.joined = set(self.state)
        self.log: list[tuple[str, str]] = []
        self.fail_join: Exception | None = None
        self.displayname = None
        # Messages posted, in order: (room, content, txn_id, event_id).
        self.sent: list[tuple[str, dict, str, str]] = []
        self.uploads: dict[str, tuple[bytes, str, str]] = {}
        # Raised by the next send/upload, then cleared — or kept, if sticky.
        self.fail_send: Exception | None = None
        self.fail_upload: Exception | None = None
        self.sticky = False
        # Synapse answers a repeated transaction id with the event it already
        # made — for half an hour. Off, to test what holds beyond that.
        self.remember_txns = True

    async def ensure_bot(self, displayname: str) -> None:
        self.displayname = displayname

    async def joined_rooms(self) -> list[str]:
        return sorted(self.joined)

    async def join(self, room_id: str) -> None:
        self.log.append(("join", room_id))
        if self.fail_join is not None:
            raise self.fail_join
        self.joined.add(room_id)

    async def leave(self, room_id: str) -> None:
        self.log.append(("leave", room_id))
        self.joined.discard(room_id)

    async def room_state(self, room_id: str) -> list[dict]:
        return list(self.state.get(room_id, []))

    async def recent_events(self, room_id: str, types: list[str], since_ms: int) -> list[dict]:
        self.log.append(("recent", room_id, since_ms))
        if self.fail_recent is not None:
            raise self.fail_recent
        return [e for e in reversed(self.timeline.get(room_id, []))
                if e["type"] in types and e["origin_server_ts"] >= since_ms]

    def _failure(self, attr: str) -> None:
        error = getattr(self, attr)
        if error is not None:
            if not self.sticky:
                setattr(self, attr, None)
            raise error

    async def send(self, room_id: str, content: dict, txn_id: str) -> str:
        self._failure("fail_send")
        if self.remember_txns:
            for room, _, txn, event_id in self.sent:
                if (room, txn) == (room_id, txn_id):
                    return event_id
        event_id = f"$event{len(self.sent) + 1}"
        self.sent.append((room_id, content, txn_id, event_id))
        return event_id

    async def upload(self, data: bytes, filename: str, content_type: str) -> str:
        self._failure("fail_upload")
        uri = f"mxc://{SERVER}/file{len(self.uploads) + 1}"
        self.uploads[uri] = (data, filename, content_type)
        return uri

    # What a person in the room sees.
    def announcements(self) -> list[dict]:
        return [c for _, c, _, _ in self.sent if c.get("msgtype") == "m.text"]

    def files(self) -> list[tuple[str, bytes, str]]:
        """(name, content, thread root) for every file posted."""
        return [(c["body"], self.uploads[c["url"]][0], c["m.relates_to"]["event_id"])
                for _, c, _, _ in self.sent if c.get("msgtype") == "m.file"]


FORBIDDEN = MatrixError(403, "M_FORBIDDEN", "not in room")


class FakeGateway:
    """Meetings by session id, answering like the gateway's HTTP API."""

    def __init__(self) -> None:
        self.meetings: dict[str, dict] = {}
        self.acked: list[tuple[str, list[str]]] = []
        self.rows: list[dict] = []          # what /undelivered answers
        self.down = False
        self.corrupt: set[str] = set()      # digests that arrive damaged
        self.asked: list[str] = []
        self.audiences: list[tuple[str, list[dict]]] = []

    def put(self, session_id: str, status: str, files: dict[str, tuple[str, bytes]] | None = None,
            detail: str = "") -> None:
        """files: name -> (kind, content)."""
        self.meetings[session_id] = {
            "status": status, "detail": detail,
            "artifacts": [(Artifact(name, kind, hashlib.sha256(body).hexdigest(), len(body)), body)
                          for name, (kind, body) in (files or {}).items()]}

    async def meeting(self, session_id: str) -> MeetingState:
        self.asked.append(session_id)
        if self.down:
            raise GatewayUnavailable("down")
        m = self.meetings.get(session_id)
        if m is None:
            raise UnknownMeeting(session_id)
        return MeetingState(m["status"], m["status"] in ("complete", "failed"), m["detail"],
                            tuple(a for a, _ in m["artifacts"]))

    async def artifact(self, session_id: str, sha256: str) -> bytes:
        if self.down or sha256 in self.corrupt:
            raise GatewayUnavailable("no")
        for a, body in self.meetings[session_id]["artifacts"]:
            if a.sha256 == sha256:
                return body
        raise GatewayUnavailable("HTTP 404")

    async def undelivered(self, sources: list[str]) -> list[dict]:
        return [] if self.down else [dict(r) for r in self.rows]

    async def ack(self, session_id: str, digests: list[str]) -> None:
        self.acked.append((session_id, list(digests)))

    async def audience(self, session_id: str, people: list[dict]) -> bool:
        if self.down:
            return False
        self.audiences.append((session_id, [dict(p) for p in people]))
        return True


def member(user: str, membership: str = "join", name: str = "", *,
           room: str = ROOM, sender: str | None = None) -> dict:
    content = {"membership": membership}
    if name:
        content["displayname"] = name
    return {"type": "m.room.member", "room_id": room, "state_key": user,
            "sender": sender or user, "content": content}


def room_name(name: str, room: str = ROOM) -> dict:
    return {"type": "m.room.name", "room_id": room, "state_key": "",
            "sender": "@a:example.org", "content": {"name": name}}


def device(user: str, device_id: str, *, created_s: float = T0,
           expires_ms: int = 28_800_000, room: str = ROOM, left: bool = False) -> dict:
    """An Element Call membership event, shaped like the real ones."""
    content = {} if left else {
        "application": "m.call", "scope": "m.room", "call_id": "",
        "device_id": device_id, "membershipID": f"{user}:{device_id}",
        "created_ts": int(created_s * 1000), "expires": expires_ms,
        "m.call.intent": "video",
        "focus_active": {"type": "livekit", "focus_selection": "multi_sfu"},
        "foci_preferred": [{"type": "livekit", "livekit_alias": room,
                            "livekit_service_url": "https://livekit.example.org/livekit/jwt"}],
    }
    return {"type": "org.matrix.msc3401.call.member", "room_id": room,
            "state_key": f"_{user}_{device_id}_m.call", "sender": user,
            "origin_server_ts": int(created_s * 1000), "content": content}


RTC_MEMBER = "org.matrix.msc4143.rtc.member"


def rtc_device(user: str, device_id: str, *, sent_s: float = T0, room: str = ROOM,
               left: bool = False, member_id: str = "", event_id: str = "",
               duration_ms: int = 3_600_000) -> dict:
    """A MatrixRTC membership, shaped as matrix-js-sdk's sticky membership
    manager sends it: joined, or left with `member.membership`."""
    member_id = member_id or f"{device_id.lower()}-0f3c-4d2e-9a1b-member"
    content = {
        "slot_id": "m.call#ROOM", "member": {"id": member_id, "membership": "leave"},
        "msc4354_sticky_key": member_id,
    } if left else {
        "application": {"type": "m.call", "m.call.intent": "video"},
        "slot_id": "m.call#ROOM",
        "transports": {"published": [{"type": "livekit", "url": "wss://livekit.example.org"}],
                       "can_subscribe": ["livekit"]},
        "member": {"device_id": device_id, "user_id": user, "id": member_id,
                   "membership": "join"},
        "msc4354_sticky_key": member_id,
    }
    sent_ms = int(sent_s * 1000)
    return {"type": RTC_MEMBER, "room_id": room, "sender": user,
            "event_id": event_id or f"${sent_ms}{device_id}{'L' if left else 'J'}",
            "origin_server_ts": sent_ms, "msc4354_sticky": {"duration_ms": duration_ms},
            "content": content}


@pytest.fixture
def clock() -> Clock:
    return Clock()
