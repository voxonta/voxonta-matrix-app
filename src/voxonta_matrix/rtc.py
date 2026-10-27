"""Reading Element Call out of a room: who is in a call, under which names.

Element Call announces each device in a call with an event in the room: a state
event in the format clients send today, a sticky event (MSC4354) in the
MatrixRTC format (MSC4143) they are moving to. The call itself is never an
object anywhere — it is "the room has at least one live membership".
Everything here is pure: events in, plain values out.

Two names have to be worked out without asking anyone, because nothing hands
them over:

* the LiveKit room — lk-jwt-service derives it from the Matrix room id, and the
  capture service has to join the same one;
* each device's LiveKit identity — the key the capture service sees on every
  audio track. Clients have used three forms of it (below); which one a client
  uses is its business, so every form a membership could produce goes into the
  map, and the capture service just looks the track's identity up.

Both names were checked against lk-jwt-service 0.9.0, which issues them.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# The per-device membership state event Element Call sends today (MSC3401 lineage).
LEGACY_MEMBER = "org.matrix.msc3401.call.member"
# MSC4143's sticky event, stable and unstable; matrix-js-sdk sends the latter.
RTC_MEMBER_TYPES = ("m.rtc.member", "org.matrix.msc4143.rtc.member")

# MSC4354: a sticky event stops counting at most an hour after it was sent.
# Clients re-send their membership before then for as long as they stay.
MAX_STICKY_MS = 3600 * 1000

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

        The three lk-jwt-service issues: `@user:server:DEVICE` from /sfu/get,
        which clients use today; a hash with the device from its MSC4195
        /get_token; one without from the homeserver's /rtc/livekit/get_token,
        which MatrixRTC clients move to. Which one a client uses is its
        business. Unused forms cost a map entry; a missing one costs a speaker
        nobody can name.
        """
        return [
            f"{self.user_id}:{self.device_id}",
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


@dataclass(frozen=True)
class RtcEntry:
    """One `m.rtc.member` sticky event, as an entry of the room's map.

    MSC4354 makes these a map keyed by sender and `sticky_key`: of the events
    under one key, the one its sender's server meant to expire last is the
    device's current word, a later event id breaking a tie. That order, not
    the order of arrival, decides — events from other servers come late.
    """

    key: tuple[str, str]
    # origin_server_ts + sticky duration: what orders the events under a key.
    intended_ms: int
    event_id: str
    # When this entry stops counting, on this side's clock.
    expires_at_ms: int
    # None when the device left, or is in something other than the room's call.
    membership: Membership | None

    def supersedes(self, other: RtcEntry) -> bool:
        return (self.intended_ms, self.event_id) > (other.intended_ms, other.event_id)


def parse_rtc_member(event: dict, now_ms: int) -> RtcEntry | None:
    """The map entry an `m.rtc.member` event makes, read as matrix-js-sdk does.

    None for an event that makes no entry — one without a sticky key. An entry
    without a membership for a device that left: `member.membership` "leave",
    or, from clients predating that field, nothing left of the content but the
    slot and the key. Also without one for a membership that is not the
    room-wide call over LiveKit, or that names someone other than its sender.

    It stops counting when the homeserver says, in `unsigned`, how long it has
    left; failing that, at the time its sender meant (MSC4354 caps both at an
    hour). An event with no sticky duration at all is given the hour: a
    membership that never ends would keep a call open for good.
    """
    content = _obj(event.get("content"))
    sticky_key = content.get("sticky_key", content.get("msc4354_sticky_key"))
    if not isinstance(sticky_key, str) or not sticky_key:
        return None
    if content.get("msc4354_sticky_key", sticky_key) != sticky_key:
        return None
    sender = str(event.get("sender") or "")
    sent = _int(event.get("origin_server_ts"))
    duration = _int(_obj(event.get("sticky") or event.get("msc4354_sticky"))
                    .get("duration_ms")) or MAX_STICKY_MS
    unsigned = _obj(event.get("unsigned"))
    remaining = _int(unsigned.get("sticky_duration_remaining_ms",
                                  unsigned.get("msc4354_sticky_duration_ttl_ms")))
    expires_at = (now_ms + min(remaining, MAX_STICKY_MS) if remaining > 0
                  else sent + min(duration, MAX_STICKY_MS))
    return RtcEntry((sender, sticky_key), sent + duration, str(event.get("event_id") or ""),
                    expires_at, _rtc_membership(event, content, sender, expires_at))


def _rtc_membership(event: dict, content: dict, sender: str,
                    expires_at: int) -> Membership | None:
    member = _obj(content.get("member"))
    if member.get("membership", "join") != "join":
        return None
    # A leave from before `member.membership` has no application: it stops here.
    if content.get("slot_id") != CALL_SLOT \
            or _obj(content.get("application")).get("type") != "m.call":
        return None
    if member.get("user_id") != sender:
        # matrix-js-sdk refuses these too: nothing stops anyone from sending
        # one in someone else's name.
        logger.warning("call membership from %s in %s claims to be %r; ignored",
                       sender, event.get("room_id"), member.get("user_id"))
        return None
    device_id, member_id = member.get("device_id"), member.get("id")
    if not isinstance(device_id, str) or not device_id \
            or not isinstance(member_id, str) or not member_id:
        return None
    published = _obj(content.get("transports")).get("published")
    if not isinstance(published, list) or not any(
            isinstance(t, dict) and t.get("type") == "livekit" for t in published):
        logger.warning("call membership of %s in %s publishes no livekit transport",
                       sender, event.get("room_id"))
        return None
    return Membership(sender, device_id, member_id, expires_at)


def _obj(value) -> dict:
    """A JSON object, or an empty one for anything else: a field of the wrong
    type in someone's event is not a reason to fail the whole transaction."""
    return value if isinstance(value, dict) else {}


def _int(value) -> int:
    """A positive integer from JSON, else 0. Booleans are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)
