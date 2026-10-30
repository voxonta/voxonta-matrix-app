"""Durable widget state. Each update is a row-locked transaction, including commands."""

from __future__ import annotations

import asyncio
import base64
import copy
from contextlib import asynccontextmanager

from psycopg.types.json import Jsonb


def empty_state():
    return {"rooms": {}, "commands": {}, "reports": {}}


class MemoryWidgetStore:
    def __init__(self):
        self.state = empty_state()
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            state = copy.deepcopy(self.state)
            yield state
            self.state = state


class PostgresWidgetStore:
    """A separate namespace keeps existing bot calls and pending deliveries intact.

    The row lock serializes two administrators and two service replicas. Files
    are committed in a separate BYTEA table in the same transaction as metadata.
    """

    def __init__(self, pool):
        self.pool = pool

    async def open(self):
        async with self.pool.connection() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS voxonta_widget_state ("
                "id INTEGER PRIMARY KEY, state JSONB NOT NULL)"
            )
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS voxonta_widget_artifacts ("
                "room_id TEXT NOT NULL, session_id TEXT NOT NULL, kind TEXT NOT NULL, "
                "sha256 TEXT NOT NULL, content BYTEA NOT NULL, "
                "PRIMARY KEY (room_id, session_id, kind))"
            )
            await conn.execute(
                "INSERT INTO voxonta_widget_state VALUES (1, %s) " "ON CONFLICT DO NOTHING",
                (Jsonb(empty_state()),),
            )

    @asynccontextmanager
    async def transaction(self):
        async with self.pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute("SELECT state FROM voxonta_widget_state WHERE id=1 FOR UPDATE")
            ).fetchone()
            state = row[0]
            yield state
            for room_id, record in state["rooms"].items():
                for meeting in record["meetings"]:
                    for kind, artifact in meeting["artifacts"].items():
                        content = artifact.pop("content", None)
                        if content is not None:
                            await conn.execute(
                                "INSERT INTO voxonta_widget_artifacts VALUES "
                                "(%s,%s,%s,%s,%s) ON CONFLICT (room_id,session_id,kind) "
                                "DO UPDATE SET sha256=excluded.sha256, content=excluded.content",
                                (
                                    room_id,
                                    meeting["session_id"],
                                    kind,
                                    artifact["sha256"],
                                    base64.b64decode(content),
                                ),
                            )
            await conn.execute(
                "UPDATE voxonta_widget_state SET state=%s WHERE id=1", (Jsonb(state),)
            )

    async def artifact(self, room_id, session_id, kind):
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT content FROM voxonta_widget_artifacts "
                    "WHERE room_id=%s AND session_id=%s AND kind=%s",
                    (room_id, session_id, kind),
                )
            ).fetchone()
            if row is None:
                raise ValueError("Материал не найден")
            return bytes(row[0])
