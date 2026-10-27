"""What has to outlive a restart: live calls, finished meetings, seen transactions.

A call's session id is the one thing that cannot be rebuilt. The capture
service streams under it and the gateway files the meeting under it; a restart
that handed out a new one in the middle of a call would turn one meeting into
two. Rooms and memberships are not kept here — the homeserver has them, and the
integration asks it again on start.

Two implementations with one surface: Postgres for a deployment, memory for
tests and for trying the service out.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Call:
    room_id: str
    # UUIDv4, issued when the call starts and kept until it ends.
    session_id: str
    started_at: int  # unix seconds
    # When the last device left; the call ends once this is old enough.
    empty_since: float | None = None
    # False while the room has opted out (!без-записи); lasts until the call ends.
    recording: bool = True
    # Every LiveKit identity seen in this call → {"name", "user"}. Only grows:
    # someone who left still has to be named in the transcript.
    members: dict[str, dict[str, str]] = field(default_factory=dict)
    # Whether the room has been told the call is transcribed. Nothing is
    # recorded before it has: the bot is invisible in the call.
    announced: bool = False


@dataclass
class Meeting:
    """A call that has ended, and how far delivering its results has got."""

    session_id: str
    room_id: str
    room_name: str
    started_at: int
    ended_at: int
    # Backing off after answers that brought nothing: see delivery.py.
    misses: int = 0
    next_try: float = 0.0
    # The message the files hang under, once it is posted; files that come
    # later go into the same thread rather than a second announcement.
    thread_root: str = ""
    # Digests already in the room. Kept per file, so a crash between two
    # uploads resumes with the second instead of posting the first again.
    published: list[str] = field(default_factory=list)
    # Set when nothing more is expected. The row stays for a while after: the
    # gateway lists a meeting until every file is acknowledged, and a row that
    # was deleted would make it look new — and get announced twice.
    done_at: float | None = None


class MemoryStore:
    def __init__(self) -> None:
        self.calls: dict[str, Call] = {}
        self.meetings: dict[str, Meeting] = {}
        self.txns: dict[str, float] = {}
        self.opt_outs: dict[str, float] = {}

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def load_calls(self) -> list[Call]:
        return [Call(**vars(c)) for c in self.calls.values()]

    async def save_call(self, call: Call) -> None:
        self.calls[call.room_id] = Call(**{**vars(call), "members": dict(call.members)})

    async def end_call(self, call: Call, meeting: Meeting) -> None:
        self.calls.pop(call.room_id, None)
        self.meetings.setdefault(meeting.session_id, _copy(meeting))

    async def pending_meetings(self) -> list[Meeting]:
        return sorted((_copy(m) for m in self.meetings.values() if m.done_at is None),
                      key=lambda m: m.ended_at)

    async def due_meetings(self, now: float) -> list[Meeting]:
        return [m for m in await self.pending_meetings() if m.next_try <= now]

    async def get_meeting(self, session_id: str) -> Meeting | None:
        m = self.meetings.get(session_id)
        return _copy(m) if m is not None else None

    async def add_meeting(self, meeting: Meeting) -> bool:
        if meeting.session_id in self.meetings:
            return False
        self.meetings[meeting.session_id] = _copy(meeting)
        return True

    async def save_meeting(self, meeting: Meeting) -> None:
        self.meetings[meeting.session_id] = _copy(meeting)

    async def prune_meetings(self, done_before: float) -> None:
        for sid in [s for s, m in self.meetings.items()
                    if m.done_at is not None and m.done_at < done_before]:
            del self.meetings[sid]

    async def load_opt_outs(self) -> set[str]:
        return set(self.opt_outs)

    async def set_opt_out(self, room_id: str, opted_out: bool, now: float) -> None:
        if opted_out:
            self.opt_outs.setdefault(room_id, now)
        else:
            self.opt_outs.pop(room_id, None)

    async def txn_seen(self, txn_id: str) -> bool:
        return txn_id in self.txns

    async def mark_txn(self, txn_id: str, now: float) -> None:
        self.txns[txn_id] = now

    async def prune_txns(self, before: float) -> None:
        for txn_id in [t for t, at in self.txns.items() if at < before]:
            del self.txns[txn_id]


def _copy(m: Meeting) -> Meeting:
    return Meeting(**{**vars(m), "published": list(m.published)})


_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    room_id     TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    started_at  BIGINT NOT NULL,
    empty_since DOUBLE PRECISION,
    recording   BOOLEAN NOT NULL DEFAULT TRUE,
    members     JSONB NOT NULL DEFAULT '{}'
);
ALTER TABLE calls ADD COLUMN IF NOT EXISTS announced BOOLEAN NOT NULL DEFAULT FALSE;
-- Rooms that said !без-записи with no call on: it holds their next call.
CREATE TABLE IF NOT EXISTS room_opt_outs (
    room_id     TEXT PRIMARY KEY,
    since       DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS meetings (
    session_id  TEXT PRIMARY KEY,
    room_id     TEXT NOT NULL,
    room_name   TEXT NOT NULL DEFAULT '',
    started_at  BIGINT NOT NULL,
    ended_at    BIGINT NOT NULL
);
-- Delivery, added with publishing; a table from before gets the columns here.
ALTER TABLE meetings ADD COLUMN IF NOT EXISTS misses      INTEGER NOT NULL DEFAULT 0;
ALTER TABLE meetings ADD COLUMN IF NOT EXISTS next_try    DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE meetings ADD COLUMN IF NOT EXISTS thread_root TEXT NOT NULL DEFAULT '';
ALTER TABLE meetings ADD COLUMN IF NOT EXISTS published   JSONB NOT NULL DEFAULT '[]';
ALTER TABLE meetings ADD COLUMN IF NOT EXISTS done_at     DOUBLE PRECISION;
CREATE TABLE IF NOT EXISTS transactions (
    txn_id      TEXT PRIMARY KEY,
    received_at DOUBLE PRECISION NOT NULL
);
"""


_MEETING_COLUMNS = ("session_id, room_id, room_name, started_at, ended_at, misses, next_try,"
                    " thread_root, published, done_at")


class PostgresStore:
    """The same surface on Postgres. The schema is created on start: the role
    owns its database and nothing else, and a migration tool for four tables
    would be more to run than the tables themselves."""

    def __init__(self, dsn: str) -> None:
        from psycopg_pool import AsyncConnectionPool

        # A pool rather than one connection: it reconnects by itself when the
        # database restarts, which a nightly backup or an upgrade will do.
        self._pool = AsyncConnectionPool(dsn, min_size=1, max_size=4, open=False,
                                         kwargs={"autocommit": True})

    async def open(self) -> None:
        await self._pool.open(wait=True)
        async with self._pool.connection() as conn:
            await conn.execute(_SCHEMA)
            held = await (await conn.execute("SELECT COUNT(*) FROM calls")).fetchone()
        logger.info("state on postgres (%d live call(s) carried over)", held[0])

    async def close(self) -> None:
        await self._pool.close()

    async def load_calls(self) -> list[Call]:
        async with self._pool.connection() as conn:
            rows = await (await conn.execute(
                "SELECT room_id, session_id, started_at, empty_since, recording, members,"
                "       announced FROM calls")).fetchall()
        return [Call(r[0], r[1], int(r[2]), r[3], bool(r[4]), dict(r[5] or {}), bool(r[6]))
                for r in rows]

    async def save_call(self, call: Call) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO calls (room_id, session_id, started_at, empty_since,"
                "                   recording, members, announced)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (room_id) DO UPDATE SET session_id = excluded.session_id,"
                "   started_at = excluded.started_at, empty_since = excluded.empty_since,"
                "   recording = excluded.recording, members = excluded.members,"
                "   announced = excluded.announced",
                (call.room_id, call.session_id, call.started_at, call.empty_since,
                 call.recording, json.dumps(call.members, ensure_ascii=False),
                 call.announced))

    async def end_call(self, call: Call, meeting: Meeting) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO meetings (session_id, room_id, room_name, started_at,"
                "                      ended_at)"
                " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (session_id) DO NOTHING",
                (meeting.session_id, meeting.room_id, meeting.room_name,
                 meeting.started_at, meeting.ended_at))
            await conn.execute("DELETE FROM calls WHERE room_id = %s AND session_id = %s",
                               (call.room_id, call.session_id))

    async def _meetings(self, where: str, args: tuple) -> list[Meeting]:
        async with self._pool.connection() as conn:
            rows = await (await conn.execute(
                f"SELECT {_MEETING_COLUMNS} FROM meetings WHERE {where} ORDER BY ended_at",
                args)).fetchall()
        return [Meeting(r[0], r[1], r[2], int(r[3]), int(r[4]), int(r[5]), float(r[6]),
                        r[7], list(r[8] or []), r[9]) for r in rows]

    async def pending_meetings(self) -> list[Meeting]:
        return await self._meetings("done_at IS NULL", ())

    async def due_meetings(self, now: float) -> list[Meeting]:
        return await self._meetings("done_at IS NULL AND next_try <= %s", (now,))

    async def get_meeting(self, session_id: str) -> Meeting | None:
        found = await self._meetings("session_id = %s", (session_id,))
        return found[0] if found else None

    async def add_meeting(self, meeting: Meeting) -> bool:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO meetings (session_id, room_id, room_name, started_at, ended_at)"
                " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (session_id) DO NOTHING",
                (meeting.session_id, meeting.room_id, meeting.room_name,
                 meeting.started_at, meeting.ended_at))
            return cur.rowcount == 1

    async def save_meeting(self, meeting: Meeting) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE meetings SET misses = %s, next_try = %s, thread_root = %s,"
                " published = %s, done_at = %s WHERE session_id = %s",
                (meeting.misses, meeting.next_try, meeting.thread_root,
                 json.dumps(meeting.published), meeting.done_at, meeting.session_id))

    async def prune_meetings(self, done_before: float) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("DELETE FROM meetings WHERE done_at < %s", (done_before,))

    async def load_opt_outs(self) -> set[str]:
        async with self._pool.connection() as conn:
            rows = await (await conn.execute("SELECT room_id FROM room_opt_outs")).fetchall()
        return {r[0] for r in rows}

    async def set_opt_out(self, room_id: str, opted_out: bool, now: float) -> None:
        async with self._pool.connection() as conn:
            if opted_out:
                await conn.execute(
                    "INSERT INTO room_opt_outs (room_id, since) VALUES (%s, %s)"
                    " ON CONFLICT (room_id) DO NOTHING", (room_id, now))
            else:
                await conn.execute("DELETE FROM room_opt_outs WHERE room_id = %s", (room_id,))

    async def txn_seen(self, txn_id: str) -> bool:
        async with self._pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT 1 FROM transactions WHERE txn_id = %s", (txn_id,))).fetchone()
        return row is not None

    async def mark_txn(self, txn_id: str, now: float) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO transactions (txn_id, received_at) VALUES (%s, %s)"
                " ON CONFLICT (txn_id) DO NOTHING", (txn_id, now))

    async def prune_txns(self, before: float) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("DELETE FROM transactions WHERE received_at < %s", (before,))
