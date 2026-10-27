"""What the integration knows about a room: its name, who is in it, who is in its call.

Built from the room's state, and its recent call memberships, when the bot
joins (or the service starts), and kept current from the events the homeserver
pushes. Held in memory only — the homeserver has it all, and asking again on
start is cheaper than keeping a second copy that can disagree with the first.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from voxonta_matrix.rtc import (
    LEGACY_MEMBER,
    Membership,
    RtcEntry,
    parse_legacy_member,
    parse_rtc_member,
)

logger = logging.getLogger(__name__)

# meeting.v1 RoomKind, sent as the call's `type` and passed on unchanged.
ROOM_ONE_TO_ONE = 1
ROOM_GROUP = 2

# Who may stop and resume recording: a room administrator, as Element labels
# power level 100.
ADMIN_LEVEL = 100


@dataclass
class Room:
    room_id: str
    name: str = ""
    alias: str = ""
    # Joined members: user id → display name ("" when they never set one).
    members: dict[str, str] = field(default_factory=dict)
    # Devices in the call, by the state key of their membership event.
    memberships: dict[str, Membership] = field(default_factory=dict)
    # Devices in the call that announce themselves with sticky m.rtc.member
    # events, by sender and sticky key.
    rtc: dict[tuple[str, str], RtcEntry] = field(default_factory=dict)
    encrypted: bool = False
    # From m.room.create: who made the room, and its version.
    creators: set[str] = field(default_factory=set)
    version: str = "1"
    # From m.room.power_levels; None until the room has one.
    power_users: dict[str, int] | None = None
    power_default: int = 0

    def apply(self, event: dict) -> None:
        """Take in one state event. Anything not about the room's call or its
        name and people is not this class's business and is left alone."""
        etype = event.get("type")
        key = event.get("state_key")
        content = event.get("content") or {}
        if key is None:
            return
        if etype == "m.room.name":
            self.name = str(content.get("name") or "")
        elif etype == "m.room.canonical_alias":
            self.alias = str(content.get("alias") or "")
        elif etype == "m.room.member":
            if content.get("membership") == "join":
                self.members[key] = str(content.get("displayname") or "")
            else:
                self.members.pop(key, None)
        elif etype == "m.room.create":
            self.version = str(content.get("room_version") or "1")
            extra = content.get("additional_creators")
            self.creators = {str(event.get("sender") or "")} | (
                {str(u) for u in extra} if isinstance(extra, list) else set())
        elif etype == "m.room.power_levels":
            users = content.get("users") if isinstance(content.get("users"), dict) else {}
            self.power_users = {str(u): _level(v) for u, v in users.items()}
            self.power_default = _level(content.get("users_default"))
        elif etype == "m.room.encryption":
            if not self.encrypted:
                # Not a formality: an encrypted call is one the bot cannot hear.
                logger.warning("room %s is encrypted; calls in it cannot be transcribed",
                               self.room_id)
            self.encrypted = True
        elif etype == LEGACY_MEMBER:
            membership = parse_legacy_member(event)
            if membership is None:
                self.memberships.pop(key, None)
            else:
                self.memberships[key] = membership

    def apply_rtc(self, event: dict, now_ms: int) -> None:
        """Take in one `m.rtc.member` event, from a transaction or the
        room's recent history. Entries that have stopped counting go."""
        entry = parse_rtc_member(event, now_ms)
        if entry is not None:
            current = self.rtc.get(entry.key)
            if current is None or entry.supersedes(current):
                self.rtc[entry.key] = entry
        for key in [k for k, e in self.rtc.items() if e.expires_at_ms <= now_ms]:
            del self.rtc[key]

    def active(self, now_ms: int) -> list[Membership]:
        """The devices in the call now, in whichever format they said so."""
        rtc = [e.membership for e in self.rtc.values()
               if e.membership is not None and e.membership.active(now_ms)]
        return [m for m in self.memberships.values() if m.active(now_ms)] + rtc

    def name_of(self, user_id: str) -> str:
        """How to call a person: their display name, else the local part of the id."""
        return self.members.get(user_id) or user_id.lstrip("@").split(":", 1)[0]

    def people(self, bot_user_id: str) -> list[str]:
        return sorted(u for u in self.members if u != bot_user_id)

    def display_name(self, bot_user_id: str) -> str:
        """The room's name for a heading and a file name.

        Direct rooms usually have none; for those the people in it are the
        name, as Element itself shows it.
        """
        if self.name:
            return self.name
        if self.alias:
            return self.alias
        people = self.people(bot_user_id)
        if people:
            return ", ".join(self.name_of(u) for u in people[:3])
        return self.room_id

    def is_admin(self, user_id: str) -> bool:
        """Whether this person is a room administrator.

        Power level 100 or more. In room version 12 and later the creators
        stand above the power levels and are not listed in them at all; in
        any room without a power levels event the creator is the one with 100.
        """
        if user_id in self.creators and (_creators_privileged(self.version)
                                         or self.power_users is None):
            return True
        if self.power_users is None:
            return False
        return self.power_users.get(user_id, self.power_default) >= ADMIN_LEVEL

    def kind(self, bot_user_id: str) -> int:
        """One-to-one when two people (the bot not counted) are in the room."""
        return ROOM_ONE_TO_ONE if len(self.people(bot_user_id)) <= 2 else ROOM_GROUP


def _level(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _creators_privileged(version: str) -> bool:
    """Room version 12 made creators unconditionally all-powerful."""
    try:
        return int(version) >= 12
    except ValueError:
        return False
