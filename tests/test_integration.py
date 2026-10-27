"""When the integration serves a room, and when a call in it starts and ends.

Stage 1 is accepted on exactly this: in a room with the bot, a call shows up in
/calls within one poll and disappears after the pause; a call in a room without
the bot never shows up. The rest is what keeps one meeting one meeting — the
session id survives a reconnect, a device swap and a restart of this service.
"""
from __future__ import annotations

import asyncio
import uuid

import jwt
import pytest
from conftest import (
    BOT,
    ROOM,
    SERVER,
    T0,
    Clock,
    FakeMatrix,
    device,
    make_config,
    member,
    room_name,
)

from voxonta_matrix.integration import Integration, TryAgain
from voxonta_matrix.matrix import MatrixError
from voxonta_matrix.rtc import livekit_room
from voxonta_matrix.store import MemoryStore

ALICE = f"@alice:{SERVER}"
BOB = f"@bob:{SERVER}"
CAROL = f"@carol:{SERVER}"


def daily_room(*extra: dict) -> dict[str, list[dict]]:
    return {ROOM: [room_name("Дейли"), member(BOT), member(ALICE, name="Алиса"),
                   member(BOB, name="Боб"), member(CAROL), *extra]}


class Setup:
    def __init__(self, state=None, *, store=None, clock=None) -> None:
        self.clock = clock or Clock()
        self.store = store or MemoryStore()
        self.matrix = FakeMatrix(daily_room() if state is None else state)
        self.integration = Integration(make_config(), self.store, self.matrix, clock=self.clock)
        self._txn = 0

    async def start(self) -> Setup:
        await self.integration.start()
        return self

    async def send(self, *events: dict) -> None:
        self._txn += 1
        await self.integration.on_transaction(f"txn-{self._txn}", list(events))

    async def after(self, seconds: float) -> None:
        self.clock.now += seconds
        await self.integration.reconcile()

    def listing(self) -> list[dict]:
        return self.integration.calls_listing()


def run(coro):
    return asyncio.run(coro)


# ── which rooms ───────────────────────────────────────────────────────

def test_an_invite_from_a_local_user_is_accepted():
    async def go():
        s = await Setup(state={}).start()
        s.matrix.state["!new:example.org"] = [member(BOT), member(ALICE)]
        await s.send(member(BOT, "invite", room="!new:example.org", sender=ALICE))
        return s

    s = run(go())
    assert ("join", "!new:example.org") in s.matrix.log
    assert "!new:example.org" in s.integration.rooms


def test_an_invite_from_another_server_is_declined():
    async def go():
        s = await Setup(state={}).start()
        await s.send(member(BOT, "invite", room="!far:other.org", sender="@eve:other.org"))
        return s

    s = run(go())
    assert s.matrix.log == [("leave", "!far:other.org")]
    assert s.integration.rooms == {}


def test_a_call_in_a_room_without_the_bot_never_shows_up():
    # Direct calls go through the same LiveKit; only rooms the bot was invited
    # to are anyone's business here. The homeserver does not even send these,
    # but if it did they must not count.
    async def go():
        s = await Setup().start()
        await s.send(device(ALICE, "PHONE", room="!private:example.org"))
        return s

    assert run(go()).listing() == []


# ── a call ────────────────────────────────────────────────────────────

def test_a_call_shows_up_as_soon_as_someone_joins_it():
    async def go():
        s = await Setup().start()
        await s.send(device(ALICE, "LAPTOP"))
        return s

    [call] = run(go()).listing()
    assert call["token"] == ROOM
    assert call["name"] == "Дейли"
    assert call["type"] == 2
    assert call["started_at"] == int(T0)
    assert call["platform"] == "livekit"
    assert call["recording"] is True
    assert uuid.UUID(call["session_id"]).version == 4


def test_the_capture_service_gets_a_listen_only_token_for_this_room():
    async def go():
        s = await Setup().start()
        await s.send(device(ALICE, "LAPTOP"))
        return s

    [call] = run(go()).listing()
    lk = call["livekit"]
    assert lk["url"] == "wss://livekit.example.org"
    assert lk["room"] == livekit_room(ROOM)
    claims = jwt.decode(lk["token"], "a-livekit-secret-long-enough-for-hs256",
                        algorithms=["HS256"], options={"verify_nbf": False, "verify_exp": False})
    assert claims["iss"] == "voxonta-key"
    assert claims["sub"] == f"voxonta:{call['session_id']}"
    assert claims["exp"] - claims["nbf"] == 3600
    assert claims["video"] == {
        "room": livekit_room(ROOM), "roomJoin": True, "canSubscribe": True,
        "canPublish": False, "canPublishData": False, "hidden": True, "roomCreate": False,
    }


def test_every_identity_of_every_device_names_its_person():
    async def go():
        s = await Setup().start()
        await s.send(device(ALICE, "LAPTOP"), device(CAROL, "PHONE"))
        return s

    [call] = run(go()).listing()
    members = call["members"]
    assert members[f"{ALICE}:LAPTOP"] == {"name": "Алиса", "user": ALICE}
    # No display name set: the local part, not an empty label.
    assert members[f"{CAROL}:PHONE"] == {"name": "carol", "user": CAROL}
    assert len(members) == 6  # three forms per device


def test_someone_who_left_is_still_named():
    # Their audio is in the meeting; the transcript must not call them
    # "unknown" because they hung up before the end.
    async def go():
        s = await Setup().start()
        await s.send(device(ALICE, "LAPTOP"), device(BOB, "PHONE"))
        await s.send(device(BOB, "PHONE", left=True))
        return s

    [call] = run(go()).listing()
    assert call["members"][f"{BOB}:PHONE"]["name"] == "Боб"


def test_the_call_outlasts_a_short_gap_and_keeps_its_session():
    # Someone's network drops, or they move from laptop to phone: the room is
    # empty for a moment, and it is still the same meeting.
    async def go():
        s = await Setup().start()
        await s.send(device(ALICE, "LAPTOP"))
        first = s.listing()[0]["session_id"]
        await s.send(device(ALICE, "LAPTOP", left=True))
        await s.after(60)
        still = s.listing()
        await s.send(device(ALICE, "PHONE", created_s=s.clock.now))
        await s.after(120)
        return first, still, s.listing()

    first, still, later = run(go())
    assert [c["session_id"] for c in still] == [first]
    assert [c["session_id"] for c in later] == [first]


def test_the_call_ends_after_the_pause_and_waits_to_be_collected():
    async def go():
        s = await Setup().start()
        await s.send(device(ALICE, "LAPTOP"))
        session = s.listing()[0]["session_id"]
        s.clock.now += 1800
        await s.send(device(ALICE, "LAPTOP", left=True))
        await s.after(89)
        during = len(s.listing())
        await s.after(1)
        return s, session, during

    s, session, during = run(go())
    assert during == 1
    assert s.listing() == []
    [meeting] = run(s.store.pending_meetings())
    assert (meeting.session_id, meeting.room_name) == (session, "Дейли")
    assert meeting.ended_at - meeting.started_at == 1800 + 90


def test_a_device_that_vanished_stops_counting_when_it_expires():
    # A phone that loses power never sends "left". Its membership runs out,
    # and nothing pushes an event when it does — the timer has to notice.
    async def go():
        s = await Setup().start()
        await s.send(device(ALICE, "PHONE", expires_ms=600_000))
        await s.after(599)
        alive = len(s.listing())
        await s.after(1)
        await s.after(90)
        return s, alive

    s, alive = run(go())
    assert alive == 1
    assert s.listing() == []


def test_removing_the_bot_ends_its_call_at_once():
    async def go():
        s = await Setup().start()
        await s.send(device(ALICE, "LAPTOP"))
        await s.send(member(BOT, "leave", sender=ALICE))
        return s

    s = run(go())
    assert s.listing() == []
    assert ROOM not in s.integration.rooms
    assert len(run(s.store.pending_meetings())) == 1


# ── the shape of a room ───────────────────────────────────────────────

def test_a_room_of_two_is_one_to_one_and_named_after_the_other_person():
    state = {ROOM: [member(BOT), member(ALICE, name="Алиса"), member(BOB, name="Боб")]}

    async def go():
        s = await Setup(state).start()
        await s.send(device(ALICE, "LAPTOP"))
        return s

    [call] = run(go()).listing()
    assert call["type"] == 1
    assert call["name"] == "Алиса, Боб"


# ── surviving restarts and retries ────────────────────────────────────

def test_a_restart_in_the_middle_of_a_call_keeps_its_session():
    store = MemoryStore()
    clock = Clock()

    async def go():
        s = await Setup(daily_room(device(ALICE, "LAPTOP")), store=store, clock=clock).start()
        before = s.listing()[0]["session_id"]
        clock.now += 300
        again = await Setup(daily_room(device(ALICE, "LAPTOP")), store=store,
                            clock=clock).start()
        return before, again.listing()

    before, after = run(go())
    assert [c["session_id"] for c in after] == [before]


def test_a_call_that_ended_while_the_service_was_down_ends_after_the_pause():
    store = MemoryStore()
    clock = Clock()

    async def go():
        await Setup(daily_room(device(ALICE, "LAPTOP")), store=store, clock=clock).start()
        clock.now += 3600
        s = await Setup(daily_room(device(ALICE, "LAPTOP", left=True)), store=store,
                        clock=clock).start()
        listed = len(s.listing())
        await s.after(90)
        return s, listed

    s, listed = run(go())
    assert listed == 1
    assert s.listing() == []


def test_a_repeated_transaction_is_applied_once():
    async def go():
        s = await Setup(state={}).start()
        s.matrix.state["!new:example.org"] = [member(BOT)]
        invite = member(BOT, "invite", room="!new:example.org", sender=ALICE)
        await s.integration.on_transaction("same", [invite])
        await s.integration.on_transaction("same", [invite])
        return s

    assert run(go()).matrix.log.count(("join", "!new:example.org")) == 1


def test_an_invite_that_could_not_be_answered_is_retried_not_lost():
    async def go():
        s = await Setup(state={}).start()
        s.matrix.state["!new:example.org"] = [member(BOT)]
        s.matrix.fail_join = MatrixError(502, "", "bad gateway")
        invite = member(BOT, "invite", room="!new:example.org", sender=ALICE)
        with pytest.raises(TryAgain):
            await s.integration.on_transaction("t1", [invite])
        s.matrix.fail_join = None
        await s.integration.on_transaction("t1", [invite])
        return s

    assert "!new:example.org" in run(go()).integration.rooms


def test_an_invite_the_server_refuses_is_dropped_not_retried():
    async def go():
        s = await Setup(state={}).start()
        s.matrix.fail_join = MatrixError(403, "M_FORBIDDEN", "not invited")
        await s.integration.on_transaction(
            "t1", [member(BOT, "invite", room="!gone:example.org", sender=ALICE)])
        return s

    s = run(go())
    assert run(s.store.txn_seen("t1"))
