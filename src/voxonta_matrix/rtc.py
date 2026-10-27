"""Reading Element Call out of room state: who is in a call, under which names.

Element Call announces each device in a call with a state event in the room.
The call itself is never an object anywhere — it is "the room has at least one
live membership". Everything here is pure: events in, plain values out.

Two names have to be worked out without asking anyone, because nothing hands
them over:

* the LiveKit room — lk-jwt-service derives it from the Matrix room id, and the
  capture service has to join the same one;
* each device's LiveKit identity — the key the capture service sees on every
  audio track. Clients have used three forms of it (below); which one a client
  uses is its business, so every form a membership could produce goes into the
  map, and the capture service just looks the track's identity up.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# The per-device membership event Element Call sends today (MSC3401 lineage).
LEGACY_MEMBER = "org.matrix.msc3401.call.member"
# MSC4143. Not seen on a real server yet; handled in a later stage. Until then
# it is reported, not ignored silently: a client update can switch to it
# overnight, and the symptom would otherwise be calls that simply never start.
RTC_MEMBER_TYPES = ("m.rtc.member", "org.matrix.msc4143.rtc.member")

# A membership without its own `expires` lives this long — Element Call's
# default. A device that vanishes without saying so stops counting after it.
DEFAULT_EXPIRES_MS = 4 * 3600 * 1000

# The MatrixRTC slot of a room-wide Element call, as lk-jwt-service names it.
CALL_SLOT = "m.call#ROOM"


def _hash(*parts: str) -> str:
    """The MSC4195 derivation lk-jwt-service uses for rooms and identities.

    SHA-256 of the compact JSON array of the parts, unpadded standard base64.
    """
    raw = json.dumps(list(parts), separators=(",", ":"), ensure_ascii=False)
    return base64.b64encode(hashlib.sha256(raw.encode()).digest()).decode().rstrip("=")


def livekit_room(room_id: str) -> str:
    """The LiveKit room lk-jwt-service puts this Matrix room's call in.

    One way only: the name cannot be turned back into a room, so it is worked
    out for every room the bot is in.
    """
    return _hash(room_id, CALL_SLOT)


@dataclass(frozen=True)
class Membership:
    """One device in a room's call."""

    user_id: str
    device_id: str
    # The client's id for this membership; part of the identity derivations.
    member_id: str
    # Milliseconds since the epoch, on the sending client's clock.
    expires_at_ms: int

    def active(self, now_ms: int) -> bool:
        return now_ms < self.expires_at_ms

    def identities(self) -> list[str]:
        """Every LiveKit identity this device may appear under.

        `@user:server:DEVICE` is what clients send today; the two hashes are
        what lk-jwt-service issues to clients that have moved to MSC4195 — the
        newer one without the device, the legacy one with it. Unused forms cost
        a map entry; a missing one costs a speaker nobody can name.
        """
        return [
            self.member_id,
            _hash(self.user_id, self.device_id, self.member_id),
            _hash(self.user_id, self.member_id),
        ]


def parse_legacy_member(event: dict) -> Membership | None:
    """A device's membership from an `org.matrix.msc3401.call.member` event.

    None when the event says the device is not in a call: empty content is how
    a device leaves. Also None for memberships that are not a room-wide
    Element call over LiveKit — another application or focus in the same room
    is not something this integration can capture.
    """
    content = event.get("content") or {}
    if not content:
        return None
    if "memberships" in content:
        # The pre-2024 shape: one event per user listing every device. No
        # client in use sends it any more; say so rather than guess at it.
        logger.warning("ignoring an old-style call membership from %s in %s",
                       event.get("sender"), event.get("room_id"))
        return None
    if content.get("application") != "m.call" or content.get("scope", "m.room") != "m.room":
        return None
    focus = content.get("focus_active") or {}
    if focus.get("type") != "livekit":
        logger.warning("call membership of %s in %s uses focus %r, not livekit",
                       event.get("sender"), event.get("room_id"), focus.get("type"))
        return None

    user_id = str(event.get("sender") or "")
    device_id = str(content.get("device_id") or "")
    if not user_id or not device_id:
        return None
    member_id = str(content.get("membershipID") or f"{user_id}:{device_id}")
    created = content.get("created_ts") or event.get("origin_server_ts") or 0
    expires = content.get("expires") or DEFAULT_EXPIRES_MS
    try:
        expires_at = int(created) + int(expires)
    except (TypeError, ValueError):
        return None
    return Membership(user_id, device_id, member_id, expires_at)


def is_call_member_event(event_type: str) -> bool:
    return event_type == LEGACY_MEMBER or event_type in RTC_MEMBER_TYPES
