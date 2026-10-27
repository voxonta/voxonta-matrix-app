"""Fakes for the homeserver and a clock, shared by the tests.

Nothing here talks to a network: a call is a handful of state events, and the
homeserver is the few client calls the integration makes.
"""
from __future__ import annotations

import pytest

from voxonta_matrix.config import Config

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

    def __init__(self, state: dict[str, list[dict]] | None = None) -> None:
        self.state = {room: list(events) for room, events in (state or {}).items()}
        self.joined = set(self.state)
        self.log: list[tuple[str, str]] = []
        self.fail_join: Exception | None = None
        self.displayname = None

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


@pytest.fixture
def clock() -> Clock:
    return Clock()
