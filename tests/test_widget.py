import asyncio
import copy
from types import SimpleNamespace

import pytest
from conftest import ROOM, Clock, FakeGateway, device, make_config, member

from voxonta_matrix.room_access import AccessUnavailable
from voxonta_matrix.rooms import Room
from voxonta_matrix.widget import Widget
from voxonta_matrix.widget_store import MemoryWidgetStore

ALICE = "@alice:example.org"
BOB = "@bob:example.org"


class Access:
    def __init__(self):
        self.room = Room(ROOM)
        for event in [
            member(ALICE, name="Alice"),
            member(BOB, name="Bob"),
            {"type": "m.room.power_levels", "state_key": "", "content": {"users": {ALICE: 100}}},
            device(ALICE, "LAPTOP"),
        ]:
            self.room.apply(event)
        self.installed = True
        self.down = False
        self.sent = {}

    async def rooms(self):
        if self.down:
            raise AccessUnavailable("offline")
        return [{"room_id": ROOM, "sender": ALICE}] if self.installed else []

    async def snapshot(self, room, now):
        if self.down:
            raise AccessUnavailable("offline")
        return copy.deepcopy(self.room), self.installed

    async def publish(self, room, sender, sid, links):
        self.sent.setdefault(sid, {"event_id": "$" + sid})
        return self.sent[sid]


def setup():
    config = make_config()
    config.widget_public_url = "https://voxonta.example/widget"
    store, access, gateway, clock = MemoryWidgetStore(), Access(), FakeGateway(), Clock()
    widget = Widget(config, store, access, gateway, clock=clock)
    return SimpleNamespace(w=widget, db=store, access=access, gw=gateway, clock=clock)


async def start(s):
    answer = await s.w.command(ROOM, ALICE, "start", "start-1")
    await s.w.tick()
    return answer["session_id"]


async def finish(s):
    await s.w.command(ROOM, ALICE, "finish", "finish-1")
    await s.w.tick()
    call = (await s.w.status(ROOM, ALICE))["call"]
    assert call["phase"] == "stopping"
    await s.w.report(
        [{"session_id": call["session_id"], "revision": call["revision"], "phase": "paused"}]
    )
    await s.w.tick()


def test_start_is_idempotent_across_tabs_and_restart():
    async def go():
        s = setup()
        answers = await asyncio.gather(
            *(s.w.command(ROOM, ALICE, "start", str(n)) for n in range(10))
        )
        assert len({a["session_id"] for a in answers}) == 1
        restarted = Widget(s.w.config, s.db, s.access, s.gw, clock=s.clock)
        await restarted.tick()
        assert (await restarted.calls_listing())[0]["session_id"] == answers[0]["session_id"]
        assert not s.access.sent

    asyncio.run(go())


def test_pause_resume_needs_fresh_matching_report_and_keeps_session():
    async def go():
        s = setup()
        sid = await start(s)
        await s.w.command(ROOM, ALICE, "pause", "p")
        assert (await s.w.status(ROOM, ALICE))["call"]["phase"] == "stopping"
        await s.w.report([{"session_id": sid, "revision": 1, "phase": "paused"}])
        assert (await s.w.status(ROOM, ALICE))["call"]["phase"] == "stopping"
        await s.w.report([{"session_id": sid, "revision": 2, "phase": "paused"}])
        assert (await s.w.status(ROOM, ALICE))["call"]["phase"] == "paused"
        s.clock.now += 10
        assert (await s.w.status(ROOM, ALICE))["call"]["phase"] != "paused"
        await s.w.command(ROOM, ALICE, "resume", "r")
        await s.w.tick()
        assert (await s.w.calls_listing())[0]["session_id"] == sid

    asyncio.run(go())


def test_finish_waits_for_capture_then_new_start_gets_new_session():
    async def go():
        s = setup()
        sid = await start(s)
        await finish(s)
        assert not (await s.w.status(ROOM, ALICE))["call"]
        answer = await s.w.command(ROOM, ALICE, "start", "next")
        assert answer["session_id"] != sid

    asyncio.run(go())


@pytest.mark.parametrize("action", ["start", "pause", "resume", "finish", "settings"])
def test_nonadmin_cannot_control(action):
    async def go():
        s = setup()
        with pytest.raises(PermissionError):
            await s.w.command(ROOM, BOB, action, "bad", True)
        assert not s.db.state["rooms"]

    asyncio.run(go())


def test_removed_widget_and_room_member_lose_access():
    async def go():
        s = setup()
        await start(s)
        s.access.room.members.pop(BOB)
        with pytest.raises(PermissionError):
            await s.w.status(ROOM, BOB)
        s.access.installed = False
        with pytest.raises(PermissionError):
            await s.w.command(ROOM, ALICE, "pause", "bad")
        await s.w.tick()
        assert not await s.w.calls_listing()

    asyncio.run(go())


def test_adapter_failure_does_not_renew_lease_or_claim_stop():
    async def go():
        s = setup()
        await start(s)
        before = (await s.w.calls_listing())[0]["lease_until"]
        s.access.down = True
        s.clock.now += 10
        await s.w.tick()
        assert (await s.w.calls_listing())[0]["lease_until"] == before

    asyncio.run(go())


def test_command_id_cannot_be_reused_for_other_action():
    async def go():
        s = setup()
        await start(s)
        with pytest.raises(ValueError):
            await s.w.command(ROOM, ALICE, "pause", "start-1")

    asyncio.run(go())


def test_results_survive_restart_and_gateway_loss_and_are_not_acked_before_commit():
    async def go():
        s = setup()
        sid = await start(s)
        s.gw.put(
            sid,
            "complete",
            {
                "01_Executive_Summary.md": ("summary", b"Summary"),
                "09_Enriched_Transcript.md": ("analysis", b"Transcript"),
            },
        )
        await finish(s)
        assert not s.gw.acked
        assert await s.w.result(ROOM, BOB, sid, "summary") == b"Summary"
        await s.w.tick()
        assert s.gw.acked
        s.gw.down = True
        restarted = Widget(s.w.config, s.db, s.access, s.gw, clock=s.clock)
        assert await restarted.result(ROOM, BOB, sid, "transcript") == b"Transcript"
        with pytest.raises(PermissionError):
            await restarted.result(ROOM, "@outsider:example.org", sid, "summary")
        assert not s.access.sent

    asyncio.run(go())


def test_auto_publish_requires_explicit_setting_and_runs_once_without_browser():
    async def go():
        s = setup()
        await s.w.command(ROOM, ALICE, "settings", "auto", True)
        sid = await start(s)
        s.gw.put(sid, "complete", {"01_Executive_Summary.md": ("summary", b"Summary")})
        await finish(s)
        await s.w.tick()
        await s.w.tick()
        assert list(s.access.sent) == [sid]
        assert (await s.w.status(ROOM, ALICE))["meetings"][0]["delivery_status"] == "posted"

    asyncio.run(go())


def test_departed_publisher_revokes_pending_publication_but_not_archive():
    async def go():
        s = setup()
        await s.w.command(ROOM, ALICE, "settings", "auto", True)
        sid = await start(s)
        s.gw.put(sid, "complete", {"01_Executive_Summary.md": ("summary", b"Summary")})
        await finish(s)
        s.access.room.members.pop(ALICE)
        await s.w.tick()
        assert not s.access.sent
        assert (await s.w.status(ROOM, BOB))["meetings"][0]["delivery_status"] == "revoked"
        assert await s.w.result(ROOM, BOB, sid, "summary") == b"Summary"

    asyncio.run(go())


def test_store_rollback_never_accepts_half_command():
    async def go():
        s = setup()
        with pytest.raises(RuntimeError):
            async with s.db.transaction() as state:
                state["rooms"]["half"] = {}
                raise RuntimeError("database unavailable")
        assert not s.db.state["rooms"]

    asyncio.run(go())


def test_widget_only_mode_never_registers_a_bot():
    async def go():
        from conftest import FakeMatrix

        from voxonta_matrix.integration import Integration
        from voxonta_matrix.store import MemoryStore

        mx = FakeMatrix()
        config = make_config(bot_mode_enabled=False)
        integration = Integration(config, MemoryStore(), mx)
        await integration.start()
        assert mx.displayname is None
        assert not mx.log

    asyncio.run(go())


def test_widget_config_without_appservice_secrets_and_with_explicit_origins():
    from voxonta_matrix.config import _REQUIRED, Config

    env = {key: "test" for key in _REQUIRED}
    env.pop("MATRIX_AS_TOKEN")
    env.pop("MATRIX_HS_TOKEN")
    env.update(
        BOT_MODE_ENABLED="false",
        WIDGET_ENABLED="true",
        WIDGET_PUBLIC_URL="https://voxonta.example/widget",
        WIDGET_SESSION_SECRET="session-test-secret-at-least-32-characters",
        WIDGET_ADAPTER_TOKEN="adapter-test-secret-at-least-32-characters",
        WIDGET_ADAPTER_URL="http://synapse:8008/_synapse/client/voxonta/widget",
        WIDGET_PARENT_ORIGINS="https://element.example",
    )
    config = Config.from_env(env)
    assert not config.bot_mode_enabled and config.widget_enabled
    assert config.as_token == ""
    with pytest.raises(SystemExit):
        Config.from_env({**env, "WIDGET_PARENT_ORIGINS": "https://element.example/path"})


def test_consent_can_be_revoked_without_leaving_room():
    async def go():
        s = setup()
        await s.w.command(ROOM, ALICE, "settings", "auto", True)
        sid = await start(s)
        with pytest.raises(PermissionError):
            await s.w.command(ROOM, BOB, "revoke", "foreign", sid)
        await s.w.command(ROOM, ALICE, "revoke", "revoke", sid)
        s.gw.put(sid, "complete", {"01_Executive_Summary.md": ("summary", b"Summary")})
        await finish(s)
        await s.w.tick()
        assert not s.access.sent
        assert await s.w.result(ROOM, BOB, sid, "summary") == b"Summary"

    asyncio.run(go())


def test_revocation_does_not_claim_success_after_dispatch_has_started():
    async def go():
        s = setup()
        await s.w.command(ROOM, ALICE, "settings", "auto", True)
        sid = await start(s)
        s.gw.put(sid, "complete", {"01_Executive_Summary.md": ("summary", b"Summary")})
        await finish(s)
        entered, release = asyncio.Event(), asyncio.Event()
        original = s.access.publish

        async def blocked(*args):
            entered.set()
            await release.wait()
            return await original(*args)

        s.access.publish = blocked
        delivery = asyncio.create_task(s.w.collect())
        await entered.wait()
        with pytest.raises(ValueError):
            await s.w.command(ROOM, ALICE, "revoke", "late", sid)
        release.set()
        await delivery
        assert list(s.access.sent) == [sid]

    asyncio.run(go())


def test_empty_meeting_does_not_queue_an_empty_publication():
    async def go():
        s = setup()
        await s.w.command(ROOM, ALICE, "settings", "auto", True)
        sid = await start(s)
        s.gw.put(sid, "complete", {})
        await finish(s)
        await s.w.tick()
        assert not s.access.sent
        assert (await s.w.status(ROOM, BOB))["meetings"][0]["phase"] == "empty"

    asyncio.run(go())


def test_failed_analysis_can_later_replace_raw_transcript_without_early_ack():
    async def go():
        s = setup()
        sid = await start(s)
        s.gw.put(sid, "failed", {"raw.md": ("transcript", b"Raw")})
        await finish(s)
        assert await s.w.result(ROOM, BOB, sid, "transcript") == b"Raw"
        await s.w.tick()
        s.gw.acked.clear()
        s.gw.put(sid, "complete", {"09_Enriched_Transcript.md": ("analysis", b"Enriched")})
        await s.w.tick()
        assert not s.gw.acked
        assert await s.w.result(ROOM, BOB, sid, "transcript") == b"Enriched"
        await s.w.tick()
        assert s.gw.acked

    asyncio.run(go())
