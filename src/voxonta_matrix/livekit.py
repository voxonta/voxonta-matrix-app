"""The LiveKit token the capture service joins a call with.

Signed here, so the capture service never holds the LiveKit secret: it gets a
token for one room, valid for an hour, that lets it listen and nothing else.
"""
from __future__ import annotations

import time

import jwt

TOKEN_TTL_S = 3600


def capture_token(api_key: str, api_secret: str, *, identity: str, room: str,
                  now: float | None = None, ttl_s: int = TOKEN_TTL_S) -> str:
    """A LiveKit access token for a hidden, listen-only participant.

    Hidden, because the bot in a call is otherwise a tile people see and
    wonder about — the room is told in its chat instead. No publish rights of
    any kind: it has nothing to say. No room creation: a call that has not
    started has nothing to capture.

    The identity is not a Matrix user id on purpose. Element treats a LiveKit
    participant named like a user as that user's device; this one is nobody's.
    """
    issued = int(time.time() if now is None else now)
    claims = {
        "iss": api_key,
        "sub": identity,
        "nbf": issued,
        "exp": issued + ttl_s,
        "video": {
            "room": room,
            "roomJoin": True,
            "canSubscribe": True,
            "canPublish": False,
            "canPublishData": False,
            "hidden": True,
            "roomCreate": False,
        },
    }
    return jwt.encode(claims, api_secret, algorithm="HS256")
