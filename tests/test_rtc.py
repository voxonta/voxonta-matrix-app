"""The names nobody hands over, worked out the way lk-jwt-service does.

The vectors are real: a room and a device from calls on a live server,
checked against what LiveKit itself logged. If these drift, the capture service
joins an empty LiveKit room, or hears people it cannot name.
"""
from __future__ import annotations

import pytest
from conftest import T0, rtc_device

from voxonta_matrix.rooms import Room
from voxonta_matrix.rtc import (
    MAX_STICKY_MS,
    _hash,
    livekit_room,
    parse_legacy_member,
    parse_rtc_member,
)

REAL_ROOM = "!xoT5zQVbiJM2RsokJxgSyyQy0QQH8cOodP3MPCiZvGY"


def real_event(**content_overrides) -> dict:
    content = {
        "application": "m.call", "scope": "m.room", "call_id": "",
        "device_id": "OfzxlNfFVM", "membershipID": "@alice:example.org:OfzxlNfFVM",
        "created_ts": 1793086819956, "expires": 28800000, "m.call.intent": "video",
        "focus_active": {"type": "livekit", "focus_selection": "multi_sfu"},
        "foci_preferred": [{"type": "livekit", "livekit_alias": REAL_ROOM,
                            "livekit_service_url": "https://lk.example.org/livekit/jwt"}],
    }
    content.update(content_overrides)
    return {"type": "org.matrix.msc3401.call.member", "room_id": REAL_ROOM,
            "sender": "@alice:example.org",
            "state_key": "_@alice:example.org_OfzxlNfFVM_m.call", "content": content}


def test_the_livekit_room_is_the_one_livekit_logged():
    assert livekit_room(REAL_ROOM) == "TM3WoLvaZppR8uWsOUsd0rE/QA34ThcK8h+/gbBC+7o"


def test_a_membership_reads_as_the_device_it_announces():
    m = parse_legacy_member(real_event())
    assert (m.user_id, m.device_id) == ("@alice:example.org", "OfzxlNfFVM")
    assert m.expires_at_ms == 1793086819956 + 28800000


def test_every_identity_a_client_may_use_is_offered():
    # Today's clients join as user:device; MSC4195 clients join under one of
    # two hashes. Which one a client picks is not ours to know.
    m = parse_legacy_member(real_event())
    member = "@alice:example.org:OfzxlNfFVM"
    assert m.identities() == [
        member,
        _hash("@alice:example.org", "OfzxlNfFVM", member),
        _hash("@alice:example.org", member),
    ]


def test_user_and_device_is_offered_whatever_the_member_id():
    # lk-jwt-service's /sfu/get names a device user:device, member id or not.
    m = parse_legacy_member(real_event(membershipID="9b2e-uuid"))
    assert m.identities() == [
        "@alice:example.org:OfzxlNfFVM",
        _hash("@alice:example.org", "OfzxlNfFVM", "9b2e-uuid"),
        _hash("@alice:example.org", "9b2e-uuid"),
    ]


def test_the_hash_is_compact_json_sha256_unpadded_base64():
    import base64
    import hashlib

    digest = hashlib.sha256(b'["a","b"]').digest()
    assert _hash("a", "b") == base64.b64encode(digest).decode().rstrip("=")


def test_empty_content_is_a_device_leaving():
    event = real_event()
    event["content"] = {}
    assert parse_legacy_member(event) is None


def test_a_membership_expires_on_the_senders_clock():
    m = parse_legacy_member(real_event(expires=60_000))
    assert m.active(1793086819956 + 59_999)
    assert not m.active(1793086819956 + 60_000)


def test_only_a_room_wide_livekit_call_counts():
    assert parse_legacy_member(real_event(application="m.other")) is None
    assert parse_legacy_member(real_event(scope="m.user")) is None
    assert parse_legacy_member(real_event(focus_active={"type": "full_mesh"})) is None


def test_the_old_per_user_shape_is_ignored_not_misread():
    event = real_event()
    event["content"] = {"memberships": [{"device_id": "X"}]}
    assert parse_legacy_member(event) is None


# ── MatrixRTC sticky memberships (MSC4143 over MSC4354) ───────────────

ALICE = "@alice:example.org"
NOW = int(T0 * 1000)


def test_a_sticky_membership_reads_as_the_device_it_announces():
    entry = parse_rtc_member(rtc_device(ALICE, "LAPTOP", member_id="m-1"), NOW)
    assert entry.key == (ALICE, "m-1")
    m = entry.membership
    assert (m.user_id, m.device_id, m.member_id) == (ALICE, "LAPTOP", "m-1")
    # The homeserver gave no time left, so the sender's own hour counts.
    assert m.expires_at_ms == entry.expires_at_ms == NOW + 3_600_000
    # The hash without the device is what lk-jwt-service issues these clients.
    assert _hash(ALICE, "m-1") in m.identities()
    assert f"{ALICE}:LAPTOP" in m.identities()


def test_the_time_the_homeserver_says_is_left_wins():
    event = rtc_device(ALICE, "LAPTOP", sent_s=T0 - 600)
    event["unsigned"] = {"msc4354_sticky_duration_ttl_ms": 120_000}
    assert parse_rtc_member(event, NOW).expires_at_ms == NOW + 120_000
    event["unsigned"] = {"sticky_duration_remaining_ms": 60_000}
    assert parse_rtc_member(event, NOW).expires_at_ms == NOW + 60_000


def test_no_sticky_event_lasts_more_than_an_hour():
    long = rtc_device(ALICE, "LAPTOP", duration_ms=10 * MAX_STICKY_MS)
    assert parse_rtc_member(long, NOW).expires_at_ms == NOW + MAX_STICKY_MS
    long["unsigned"] = {"msc4354_sticky_duration_ttl_ms": 10 * MAX_STICKY_MS}
    assert parse_rtc_member(long, NOW).expires_at_ms == NOW + MAX_STICKY_MS
    # Not marked sticky at all: still not a call that never ends.
    del long["msc4354_sticky"], long["unsigned"]
    assert parse_rtc_member(long, NOW).expires_at_ms == NOW + MAX_STICKY_MS
    # The stable name of the field is read too.
    long["sticky"] = {"duration_ms": 60_000}
    assert parse_rtc_member(long, NOW).expires_at_ms == NOW + 60_000


@pytest.mark.parametrize("content", [
    # What matrix-js-sdk sends today.
    {"slot_id": "m.call#ROOM", "member": {"id": "m-1", "membership": "leave"},
     "msc4354_sticky_key": "m-1", "leave_reason": {"code": "hangup"}},
    # What it sent before `member.membership`: everything emptied but these.
    {"slot_id": "m.call#ROOM", "msc4354_sticky_key": "m-1"},
    {"sticky_key": "m-1"},
])
def test_a_device_leaving_is_an_entry_without_a_membership(content):
    event = rtc_device(ALICE, "LAPTOP", member_id="m-1")
    event["content"] = content
    entry = parse_rtc_member(event, NOW)
    assert entry.key == (ALICE, "m-1") and entry.membership is None


def test_an_event_without_a_sticky_key_is_no_entry():
    event = rtc_device(ALICE, "LAPTOP", member_id="m-1")
    del event["content"]["msc4354_sticky_key"]
    assert parse_rtc_member(event, NOW) is None
    event["content"]["sticky_key"] = "m-1"            # the stable name
    assert parse_rtc_member(event, NOW).key == (ALICE, "m-1")
    event["content"]["msc4354_sticky_key"] = "m-2"    # two keys that disagree
    assert parse_rtc_member(event, NOW) is None


def test_fields_of_the_wrong_type_make_no_membership_and_raise_nothing():
    for field in ("member", "application", "transports"):
        event = rtc_device(ALICE, "LAPTOP", member_id="m-1")
        event["content"][field] = ["not", "an", "object"]
        assert parse_rtc_member(event, NOW).membership is None
    event = rtc_device(ALICE, "LAPTOP", member_id="m-1")
    event["msc4354_sticky"], event["unsigned"] = [1], "x"
    assert parse_rtc_member(event, NOW).expires_at_ms == NOW + MAX_STICKY_MS


def edited(**changes):
    event = rtc_device(ALICE, "LAPTOP", member_id="m-1")
    for path, value in changes.items():
        *parents, leaf = path.split("__")
        target = event["content"]
        for name in parents:
            target = target[name]
        target[leaf] = value
    return event


@pytest.mark.parametrize("event", [
    edited(slot_id="m.call#other"),                       # another call in the room
    edited(application={"type": "m.other"}),
    edited(transports={"published": [{"type": "full_mesh"}], "can_subscribe": []}),
    edited(member__user_id="@bob:example.org"),          # in someone else's name
    edited(member__device_id=""),
    edited(member__membership="knock"),
], ids=["slot", "application", "transport", "impostor", "no-device", "membership"])
def test_only_this_rooms_livekit_call_in_ones_own_name_counts(event):
    entry = parse_rtc_member(event, NOW)
    # Still an entry: it replaces whatever this device said before.
    assert entry.key == (ALICE, "m-1") and entry.membership is None


def test_an_older_livekit_service_transport_counts_too():
    event = edited(transports={"published": [{"type": "livekit", "livekit_service_url":
                                             "https://lk.example.org/livekit/jwt"}],
                               "can_subscribe": ["livekit"]})
    assert parse_rtc_member(event, NOW).membership is not None


# ── the map they make ──

def test_the_device_says_what_its_latest_event_says_whatever_arrives_first():
    joined = rtc_device(ALICE, "LAPTOP", member_id="m-1", sent_s=T0)
    left = rtc_device(ALICE, "LAPTOP", member_id="m-1", sent_s=T0 + 60, left=True)
    for order in ([joined, left], [left, joined]):
        room = Room("!r:example.org")
        for event in order:
            room.apply_rtc(event, NOW + 61_000)
        assert room.active(NOW + 61_000) == []


def test_an_event_not_marked_sticky_does_not_outrank_a_later_one():
    unmarked = rtc_device(ALICE, "LAPTOP", member_id="m-1")
    del unmarked["msc4354_sticky"]
    left = rtc_device(ALICE, "LAPTOP", member_id="m-1", sent_s=T0 + 60, left=True)
    for order in ([unmarked, left], [left, unmarked]):
        room = Room("!r:example.org")
        for event in order:
            room.apply_rtc(event, NOW + 61_000)
        assert room.active(NOW + 61_000) == []


def test_a_tie_goes_to_the_higher_event_id():
    a = rtc_device(ALICE, "LAPTOP", member_id="m-1", event_id="$A")
    z = rtc_device(ALICE, "LAPTOP", member_id="m-1", event_id="$Z", left=True)
    for order in ([a, z], [z, a]):
        room = Room("!r:example.org")
        for event in order:
            room.apply_rtc(event, NOW)
        assert room.active(NOW) == []


def test_devices_are_apart_by_sender_and_key():
    room = Room("!r:example.org")
    room.apply_rtc(rtc_device(ALICE, "LAPTOP", member_id="m-1"), NOW)
    room.apply_rtc(rtc_device(ALICE, "PHONE", member_id="m-2"), NOW)
    room.apply_rtc(rtc_device("@bob:example.org", "PHONE", member_id="m-2"), NOW)
    assert sorted(m.device_id for m in room.active(NOW)) == ["LAPTOP", "PHONE", "PHONE"]


def test_an_entry_that_ran_out_is_dropped():
    room = Room("!r:example.org")
    room.apply_rtc(rtc_device(ALICE, "LAPTOP", member_id="m-1", duration_ms=60_000), NOW)
    assert len(room.active(NOW + 59_999)) == 1
    assert room.active(NOW + 60_000) == []
    room.apply_rtc(rtc_device(ALICE, "PHONE", member_id="m-2"), NOW + 60_000)
    assert list(room.rtc) == [(ALICE, "m-2")]


def test_both_formats_count_in_one_room():
    room = Room(REAL_ROOM)
    room.apply(real_event())
    room.apply_rtc(rtc_device("@bob:example.org", "PHONE"), NOW)
    assert sorted(m.user_id for m in room.active(NOW)) == ["@alice:example.org",
                                                          "@bob:example.org"]
