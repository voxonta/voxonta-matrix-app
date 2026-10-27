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


def test_delivery_progress_survives_and_finished_meetings_leave_the_queue(make_store):
    room = f"!{uuid.uuid4().hex}:example.org"
    due, later, done = (str(uuid.uuid4()) for _ in range(3))

    async def go():
        store = make_store()
        await store.open()
        for sid in (due, later, done):
            assert await store.add_meeting(Meeting(sid, room, "Дейли", 0, 4600))
        assert not await store.add_meeting(Meeting(due, room, "другое", 0, 1))

        m = await store.get_meeting(later)
        m.misses, m.next_try = 2, 9000.0
        await store.save_meeting(m)
        m = await store.get_meeting(due)
        m.thread_root, m.published = "$root", ["a" * 64, "b" * 64]
        await store.save_meeting(m)
        m = await store.get_meeting(done)
        m.done_at = 5000.0
        await store.save_meeting(m)

        mine = {m.session_id for m in await store.due_meetings(8000.0)} & {due, later, done}
        pending = {m.session_id for m in await store.pending_meetings()} & {due, later, done}
        loaded = await store.get_meeting(due)
        await store.prune_meetings(4999.0)
        kept = await store.get_meeting(done)
        await store.prune_meetings(5001.0)
        pruned = await store.get_meeting(done)
        await store.close()
        return mine, pending, loaded, kept, pruned

    mine, pending, loaded, kept, pruned = run(go())
    assert mine == {due}
    assert pending == {due, later}
    assert loaded == Meeting(due, room, "Дейли", 0, 4600, thread_root="$root",
                             published=["a" * 64, "b" * 64])
    assert kept is not None and pruned is None


@pytest.mark.skipif(not _PG, reason="TEST_DATABASE_URL is not set")
def test_a_meetings_table_from_before_publishing_gets_its_new_columns():
    import psycopg

    schema = f"s{uuid.uuid4().hex[:12]}"
    sid = str(uuid.uuid4())

    async def go():
        from voxonta_matrix.store import PostgresStore

        with psycopg.connect(_PG, autocommit=True) as conn:
            conn.execute(f"CREATE SCHEMA {schema}")
            conn.execute(f"CREATE TABLE {schema}.meetings (session_id TEXT PRIMARY KEY,"
                         " room_id TEXT NOT NULL, room_name TEXT NOT NULL DEFAULT '',"
                         " started_at BIGINT NOT NULL, ended_at BIGINT NOT NULL)")
            conn.execute(f"INSERT INTO {schema}.meetings VALUES (%s, '!r:x', 'Дейли', 1, 2)",
                         (sid,))
        sep = "&" if "?" in _PG else "?"
        store = PostgresStore(f"{_PG}{sep}options=-csearch_path%3D{schema}")
        try:
            await store.open()
            [meeting] = await store.due_meetings(0.0)
            meeting.published = ["c" * 64]
            await store.save_meeting(meeting)
            return await store.get_meeting(sid)
        finally:
            await store.close()
            with psycopg.connect(_PG, autocommit=True) as conn:
                conn.execute(f"DROP SCHEMA {schema} CASCADE")

    meeting = run(go())
    assert (meeting.session_id, meeting.misses, meeting.published) == (sid, 0, ["c" * 64])


def test_opt_outs_and_announcements_survive(make_store):
    room = f"!{uuid.uuid4().hex}:example.org"
    other = f"!{uuid.uuid4().hex}:example.org"

    async def go():
        store = make_store()
        await store.open()
        await store.set_opt_out(room, True, 100.0)
        await store.set_opt_out(room, True, 200.0)    # said twice
        await store.set_opt_out(other, True, 100.0)
        await store.set_opt_out(other, False, 300.0)  # and taken back
        opt_outs = await store.load_opt_outs()
        await store.save_call(Call(room, str(uuid.uuid4()), 1000, recording=False,
                                   announced=True))
        [call] = [c for c in await store.load_calls() if c.room_id == room]
        await store.set_opt_out(room, False, 400.0)
        await store.close()
        return opt_outs, call

    opt_outs, call = run(go())
    assert room in opt_outs and other not in opt_outs
    assert (call.recording, call.announced) == (False, True)


@pytest.mark.skipif(not _PG, reason="TEST_DATABASE_URL is not set")
def test_a_calls_table_from_before_announcing_gets_its_column():
    import psycopg

    schema = f"s{uuid.uuid4().hex[:12]}"

    async def go():
        from voxonta_matrix.store import PostgresStore

        with psycopg.connect(_PG, autocommit=True) as conn:
            conn.execute(f"CREATE SCHEMA {schema}")
            conn.execute(f"CREATE TABLE {schema}.calls (room_id TEXT PRIMARY KEY,"
                         " session_id TEXT NOT NULL, started_at BIGINT NOT NULL,"
                         " empty_since DOUBLE PRECISION,"
                         " recording BOOLEAN NOT NULL DEFAULT TRUE,"
                         " members JSONB NOT NULL DEFAULT '{}')")
            conn.execute(f"INSERT INTO {schema}.calls (room_id, session_id, started_at)"
                         " VALUES ('!r:x', 's', 1)")
        sep = "&" if "?" in _PG else "?"
        store = PostgresStore(f"{_PG}{sep}options=-csearch_path%3D{schema}")
        try:
            await store.open()
            return await store.load_calls()
        finally:
            await store.close()
            with psycopg.connect(_PG, autocommit=True) as conn:
                conn.execute(f"DROP SCHEMA {schema} CASCADE")

    [call] = run(go())
    # A call from before this version has not been announced: it will be, once.
    assert (call.room_id, call.announced) == ("!r:x", False)
