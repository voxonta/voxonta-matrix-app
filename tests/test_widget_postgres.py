import asyncio
import os
import uuid

import pytest
from conftest import ROOM
from psycopg_pool import AsyncConnectionPool
from test_widget import ALICE, BOB, finish, setup

from voxonta_matrix.widget import Widget
from voxonta_matrix.widget_store import PostgresWidgetStore


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="Postgres not configured")
def test_postgres_serializes_two_replicas_and_preserves_commands_after_restart():
    async def go():
        schema = "widget_" + uuid.uuid4().hex
        async with AsyncConnectionPool(os.environ["TEST_DATABASE_URL"], open=False) as admin:
            async with admin.connection() as conn:
                await conn.execute(f"CREATE SCHEMA {schema}")
            try:
                async with AsyncConnectionPool(
                    os.environ["TEST_DATABASE_URL"],
                    open=False,
                    kwargs={"options": f"-c search_path={schema}"},
                ) as pool:
                    db = PostgresWidgetStore(pool)
                    await db.open()
                    s = setup()
                    one = Widget(s.w.config, db, s.access, s.gw, clock=s.clock)
                    two = Widget(s.w.config, db, s.access, s.gw, clock=s.clock)
                    answers = await asyncio.gather(
                        one.command(ROOM, ALICE, "start", "one"),
                        two.command(ROOM, ALICE, "start", "two"),
                    )
                    assert answers[0]["session_id"] == answers[1]["session_id"]
                    retry = await two.command(ROOM, ALICE, "start", "one")
                    assert retry == answers[0]
                    with pytest.raises(RuntimeError):
                        async with db.transaction() as state:
                            state["rooms"].clear()
                            raise RuntimeError("rollback")
                    await two.tick()
                    assert (await two.calls_listing())[0]["session_id"] == retry["session_id"]
                    s.w = two
                    sid = retry["session_id"]
                    s.gw.put(sid, "complete", {"01_Executive_Summary.md": ("summary", b"durable")})
                    await finish(s)
                    assert not s.gw.acked
                    restarted = Widget(
                        s.w.config, PostgresWidgetStore(pool), s.access, s.gw, clock=s.clock
                    )
                    assert await restarted.result(ROOM, BOB, sid, "summary") == b"durable"
                    async with restarted.store.transaction() as state:
                        artifact = state["rooms"][ROOM]["meetings"][0]["artifacts"]["summary"]
                        assert "content" not in artifact
                    await restarted.tick()
                    assert s.gw.acked
            finally:
                async with admin.connection() as conn:
                    await conn.execute(f"DROP SCHEMA {schema} CASCADE")

    asyncio.run(go())
