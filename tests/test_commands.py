"""Telling the room, and letting its administrator stop and resume recording.

The bot is invisible in a call, so the message at its start is the only way
anyone knows it is being transcribed — nothing is recorded before that message
is in the room. !без-записи and !запись work as /без-записи and /запись do in
Talk, except that only a room administrator may use them.
"""
from __future__ import annotations

import asyncio

import pytest
from conftest import BOT, ROOM, SERVER, Clock, FakeMatrix, device, member, room_name

from voxonta_matrix import commands as c
from voxonta_matrix.integration import ANNOUNCE_WARN_EVERY_S, Integration, TryAgain
from voxonta_matrix.matrix import MatrixError
from voxonta_matrix.rooms import Room
from voxonta_matrix.store import MemoryStore

ALICE = f"@alice:{SERVER}"   # created the room, administrator
BOB = f"@bob:{SERVER}"       # an ordinary member


def create(sender: str, version: str = "10", *, extra: list[str] | None = None) -> dict:
    content = {"room_version": version}
    if extra is not None:
        content["additional_creators"] = extra
    return {"type": "m.room.create", "room_id": ROOM, "state_key": "", "sender": sender,
            "content": content}


def power(users: dict[str, int], default: int = 0) -> dict:
    return {"type": "m.room.power_levels", "room_id": ROOM, "state_key": "", "sender": ALICE,
            "content": {"users": users, "users_default": default}}


_n = 0


def message(sender: str, body: str, msgtype: str = "m.text", **content) -> dict:
    global _n
    _n += 1
    return {"type": "m.room.message", "room_id": ROOM, "sender": sender,
            "event_id": f"$msg{_n}", "content": {"msgtype": msgtype, "body": body, **content}}


class Setup:
    def __init__(self, *, store=None, clock=None) -> None:
        self.clock = clock or Clock()
        self.store = store or MemoryStore()
        self.matrix = FakeMatrix({ROOM: [
            room_name("Дейли"), create(ALICE), power({ALICE: 100, BOT: 50}),
            member(BOT), member(ALICE, name="Алиса"), member(BOB, name="Боб")]})
        self.integration = Integration(_config(), self.store, self.matrix, clock=self.clock)
        self._txn = 0

    async def start(self) -> Setup:
        await self.integration.start()
        return self

    async def send(self, *events: dict) -> None:
        self._txn += 1
        await self.integration.on_transaction(f"txn-{self._txn}", list(events))

    async def tick(self, after: float = 0) -> None:
        self.clock.now += after
        await self.integration.reconcile()

    def recording(self) -> bool:
        [call] = self.integration.calls_listing()
        return call["recording"]

    def said(self) -> list[str]:
        return [content["body"] for _, content, _, _ in self.matrix.sent]

    def replies(self) -> list[tuple[str, str]]:
        """(the event replied to, what was said)."""
        return [(content["m.relates_to"]["m.in_reply_to"]["event_id"], content["body"])
                for _, content, _, _ in self.matrix.sent if "m.relates_to" in content]


def _config():
    from conftest import make_config
    return make_config()


def run(coro):
    return asyncio.run(coro)


# ── who is an administrator ───────────────────────────────────────────

def room_with(*events) -> Room:
    room = Room(ROOM)
    for event in events:
        room.apply(event)
    return room


def test_an_administrator_is_power_level_100():
    room = room_with(create(ALICE), power({ALICE: 100, BOB: 50}))
    assert room.is_admin(ALICE) and not room.is_admin(BOB)
    assert not room.is_admin(f"@carol:{SERVER}")


def test_the_default_level_counts_for_everyone_not_listed():
    room = room_with(create(ALICE), power({BOB: 0}, default=100))
    assert room.is_admin(f"@carol:{SERVER}") and not room.is_admin(BOB)


def test_without_power_levels_the_creator_is_the_administrator():
    room = room_with(create(ALICE))
    assert room.is_admin(ALICE) and not room.is_admin(BOB)


def test_before_version_12_a_creator_is_whatever_the_power_levels_say():
    room = room_with(create(ALICE, "11"), power({ALICE: 0, BOB: 100}))
    assert not room.is_admin(ALICE) and room.is_admin(BOB)


def test_from_version_12_creators_are_administrators_without_being_listed():
    room = room_with(create(ALICE, "12", extra=[BOB]), power({}))
    assert room.is_admin(ALICE) and room.is_admin(BOB)
    assert not room.is_admin(f"@carol:{SERVER}")


# ── reading a command ─────────────────────────────────────────────────

@pytest.mark.parametrize("body, action", [
    ("!без-записи", c.STOP), ("  !БЕЗ-ЗАПИСИ ", c.STOP), ("/без-записи", c.STOP),
    ("!no-record", c.STOP), ("!no-transcribe", c.STOP),
    ("!запись", c.START), ("/record", c.START), ("!transcribe", c.START),
    ("!записать протокол", None), ("!запись встречи", None), ("!recording", None),
    ("запись", None), ("!", None), ("", None),
    ("> <@bob:example.org> пишем?\n\n!без-записи", c.STOP),
])
def test_commands_are_matched_whole(body, action):
    assert c.command_in(message(BOB, body)) == action


def test_edits_notices_and_other_events_are_not_commands():
    assert c.command_in(message(BOB, "!без-записи", "m.notice")) is None
    # Element marks an edit's fallback body with "* "; not every client does.
    assert c.command_in(message(BOB, "!без-записи",
                                **{"m.relates_to": {"rel_type": "m.replace"}})) is None
    assert c.command_in({"type": "m.reaction", "content": {"body": "!запись"}}) is None


# ── the announcement ──────────────────────────────────────────────────

def test_the_call_is_announced_at_once_and_only_once():
    async def go():
        s = await Setup().start()
        await s.send(device(ALICE, "LAPTOP"))
        at_once = s.recording(), s.said()
        await s.send(device(BOB, "PHONE"))
        await s.tick()
        await s.tick(5)
        return s, at_once

    s, at_once = run(go())
    # Told within the transaction that started the call, not on the next tick.
    assert at_once == (True, [c.ANNOUNCE])
    assert s.said() == [c.ANNOUNCE]
    room, content, txn, _ = s.matrix.sent[0]
    sid = s.integration.calls_listing()[0]["session_id"]
    assert (room, content["msgtype"], txn) == (ROOM, "m.notice", f"voxonta.{sid}.start")


def test_a_room_the_bot_cannot_post_in_is_not_recorded(caplog):
    async def go():
        s = await Setup().start()
        s.matrix.fail_send = MatrixError(403, "M_FORBIDDEN", "power level too low")
        s.matrix.sticky = True
        await s.send(device(ALICE, "LAPTOP"))
        for _ in range(10):
            await s.tick(5)
        still = s.recording()
        s.matrix.fail_send = None
        await s.tick(ANNOUNCE_WARN_EVERY_S)
        return s, still

    s, still = run(go())
    assert still is False and s.recording() is True
    warnings = [r for r in caplog.records if "not being recorded" in r.getMessage()]
    assert len(warnings) == 1     # said once, not every five seconds


def test_an_announcement_whose_answer_was_lost_is_not_posted_twice():
    async def go():
        s = await Setup().start()
        real_send = s.matrix.send

        async def answer_lost_once(room, content, txn_id):
            s.matrix.send = real_send
            await real_send(room, content, txn_id)
            raise TimeoutError

        s.matrix.send = answer_lost_once
        await s.send(device(ALICE, "LAPTOP"))
        first = s.recording()
        await s.tick(5)
        return s, first

    s, first = run(go())
    assert first is False and s.recording() is True
    assert s.said() == [c.ANNOUNCE]


def test_a_restart_mid_call_does_not_announce_again():
    async def go():
        store = MemoryStore()
        s = await Setup(store=store).start()
        await s.send(device(ALICE, "LAPTOP"))
        await s.tick()
        again = await Setup(store=store, clock=s.clock).start()
        await again.tick(5)
        return again

    again = run(go())
    assert again.said() == [] and again.recording() is True


# ── the commands in a call ────────────────────────────────────────────

async def _in_a_call() -> Setup:
    s = await Setup().start()
    await s.send(device(ALICE, "LAPTOP"))
    await s.tick()
    return s


def test_an_administrator_stops_and_resumes_the_call():
    stop, start = message(ALICE, "!без-записи"), message(ALICE, "!запись")

    async def go():
        s = await _in_a_call()
        await s.send(stop)
        stopped = s.recording(), [c.recording for c in await s.store.load_calls()]
        await s.send(start)
        return s, stopped

    s, stopped = run(go())
    assert stopped == (False, [False])          # stored, not only in memory
    assert s.recording() is True
    assert s.replies() == [(stop["event_id"], c.STOPPED), (start["event_id"], c.RESUMED)]


def test_someone_who_is_not_an_administrator_is_told_so_and_nothing_changes():
    stop, start = message(BOB, "!без-записи"), message(BOB, "!запись")

    async def go():
        s = await _in_a_call()
        await s.send(stop, start)
        return s

    s = run(go())
    assert s.recording() is True
    assert s.replies() == [(stop["event_id"], c.ONLY_ADMIN_STOPS),
                           (start["event_id"], c.ONLY_ADMIN_RESUMES)]


def test_a_stop_that_could_not_be_stored_says_so_and_changes_nothing():
    stop = message(ALICE, "!без-записи")

    async def go():
        s = await _in_a_call()

        async def broken(call):
            raise ConnectionError("database went away")

        s.store.save_call = broken
        await s.send(stop)
        return s

    s = run(go())
    assert s.recording() is True
    assert s.replies() == [(stop["event_id"], c.COULD_NOT_STOP)]


def test_a_reply_that_could_not_be_sent_is_retried_with_the_transaction():
    stop = message(ALICE, "!без-записи")

    async def go():
        s = await _in_a_call()
        s.matrix.fail_send = MatrixError(502, "", "bad gateway")
        with pytest.raises(TryAgain):
            await s.integration.on_transaction("txn-stop", [stop])
        held = s.recording()
        await s.integration.on_transaction("txn-stop", [stop])   # the homeserver retries
        return s, held

    s, held = run(go())
    # Stored before answering, so the call stopped even though nobody was told yet.
    assert held is False and s.recording() is False
    assert s.replies() == [(stop["event_id"], c.STOPPED)]


def test_the_bots_own_messages_are_not_commands():
    async def go():
        s = await _in_a_call()
        await s.send(message(BOT, "!без-записи"))
        return s

    s = run(go())
    assert s.recording() is True and s.replies() == []


def test_the_opt_out_ends_with_the_call():
    async def go():
        s = await _in_a_call()
        await s.send(message(ALICE, "!без-записи"))
        await s.send(device(ALICE, "LAPTOP", left=True))
        await s.tick(91)
        await s.send(device(ALICE, "PHONE", created_s=s.clock.now))
        await s.tick()
        return s

    s = run(go())
    assert s.recording() is True
    assert s.said()[-1] == c.ANNOUNCE


# ── before a call ─────────────────────────────────────────────────────

def test_an_opt_out_before_the_call_holds_the_next_call_only():
    stop = message(ALICE, "!без-записи")

    async def go():
        s = await Setup().start()
        await s.send(stop)
        await s.send(device(ALICE, "LAPTOP"))
        await s.tick()
        first = s.recording(), list(s.said())
        await s.send(device(ALICE, "LAPTOP", left=True))
        await s.tick(91)
        await s.send(device(ALICE, "PHONE", created_s=s.clock.now))
        await s.tick()
        return s, first

    s, (recorded, said) = run(go())
    assert recorded is False
    assert said == [c.STOPPED_NEXT, c.ANNOUNCE_OPTED_OUT]
    assert s.recording() is True and s.said()[-1] == c.ANNOUNCE
    assert run(s.store.load_opt_outs()) == set()


def test_an_opt_out_before_the_call_survives_a_restart():
    async def go():
        store = MemoryStore()
        s = await Setup(store=store).start()
        await s.send(message(ALICE, "!без-записи"))
        again = await Setup(store=store, clock=s.clock).start()
        again._txn = 100   # the homeserver never reuses a transaction id
        await again.send(device(ALICE, "LAPTOP"))
        await again.tick()
        return again

    again = run(go())
    assert again.recording() is False and again.said() == [c.ANNOUNCE_OPTED_OUT]


def test_resuming_before_the_call_cancels_the_opt_out():
    start = message(ALICE, "!запись")

    async def go():
        s = await Setup().start()
        await s.send(message(ALICE, "!без-записи"), start)
        await s.send(device(ALICE, "LAPTOP"))
        await s.tick()
        return s

    s = run(go())
    assert s.recording() is True
    assert s.replies()[-1] == (start["event_id"], c.RESUMED_NEXT)
    assert s.said()[-1] == c.ANNOUNCE
