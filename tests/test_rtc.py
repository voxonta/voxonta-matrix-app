"""The names nobody hands over, worked out the way lk-jwt-service does.

The vectors are real: a room and a device from calls on a live server,
checked against what LiveKit itself logged. If these drift, the capture service
joins an empty LiveKit room, or hears people it cannot name.
"""
from __future__ import annotations

from voxonta_matrix.rtc import _hash, livekit_room, parse_legacy_member

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
