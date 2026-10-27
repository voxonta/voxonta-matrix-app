"""The store keeps what a restart must not lose, on both backends alike.

Postgres runs when TEST_DATABASE_URL points at one (CI provides it); the
memory store always runs, so the contract is checked on every machine.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from voxonta_matrix.store import Call, Meeting, MemoryStore

_PG = os.environ.get("TEST_DATABASE_URL")


def _stores():
    yield "memory", MemoryStore
    if _PG:
        from voxonta_matrix.store import PostgresStore

        yield "postgres", lambda: PostgresStore(_PG)


@pytest.fixture(params=[name for name, _ in _stores()])
def make_store(request):
    return dict(_stores())[request.param]


def run(coro):
    return asyncio.run(coro)


def test_a_call_comes_back_as_it_was_saved(make_store):
    room = f"!{uuid.uuid4().hex}:example.org"

    async def go():
        store = make_store()
        await store.open()
        call = Call(room, str(uuid.uuid4()), 1000, empty_since=1500.5, recording=False,
                    members={"@a:example.org:D": {"name": "Алиса", "user": "@a:example.org"}})
        await store.save_call(call)
        call.members["x"] = {"name": "later", "user": "@x:example.org"}
        await store.save_call(call)
        loaded = [c for c in await store.load_calls() if c.room_id == room]
        await store.close()
        return call, loaded

    call, [loaded] = run(go())
    assert loaded == call


def test_ending_a_call_moves_it_to_the_meetings_to_collect(make_store):
    room = f"!{uuid.uuid4().hex}:example.org"
    session = str(uuid.uuid4())

    async def go():
        store = make_store()
        await store.open()
        call = Call(room, session, 1000)
        await store.save_call(call)
        await store.end_call(call, Meeting(session, room, "Дейли", 1000, 4600))
        calls = [c for c in await store.load_calls() if c.room_id == room]
        meetings = [m for m in await store.pending_meetings() if m.session_id == session]
        await store.close()
        return calls, meetings

    calls, meetings = run(go())
    assert calls == []
    assert meetings == [Meeting(session, room, "Дейли", 1000, 4600)]


def test_transactions_are_remembered_and_forgotten_in_time(make_store):
    old, new = f"old-{uuid.uuid4().hex}", f"new-{uuid.uuid4().hex}"

    async def go():
        store = make_store()
        await store.open()
        await store.mark_txn(old, 100.0)
        await store.mark_txn(new, 200.0)
        await store.mark_txn(new, 200.0)  # a retried transaction
        await store.prune_txns(150.0)
        seen = (await store.txn_seen(old), await store.txn_seen(new))
        await store.close()
        return seen

    assert run(go()) == (False, True)
