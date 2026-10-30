import asyncio

import pytest
from conftest import ROOM, device
from test_widget import ALICE, BOB, finish, setup

from voxonta_matrix.widget import Widget


def leave_call(s):
    s.access.room.memberships.clear()
    s.access.room.rtc.clear()


def join_call(s, device_id="NEW"):
    s.access.room.apply(device(ALICE, device_id, created_s=s.clock()))


async def next_call(s):
    leave_call(s)
    await s.w.tick()
    s.clock.now += s.w.config.call_end_grace_s + 1
    await s.w.tick()
    join_call(s)
    await s.w.tick()


def test_room_admin_can_enable_auto_before_call_and_backend_starts_without_panel():
    async def go():
        s = setup()
        leave_call(s)
        await s.w.command(ROOM, ALICE, "auto_transcribe", "enable", True)
        assert (await s.w.status(ROOM, BOB))["auto_transcribe"] is True
        assert not await s.w.calls_listing()
        join_call(s)
        await s.w.tick()
        calls = await s.w.calls_listing()
        assert len(calls) == 1 and calls[0]["recording"]
        sid = calls[0]["session_id"]
        await s.w.tick()
        assert (await s.w.calls_listing())[0]["session_id"] == sid

    asyncio.run(go())


def test_enabling_mid_call_defers_until_next_instance():
    async def go():
        s = setup()
        await s.w.command(ROOM, ALICE, "auto_transcribe", "enable", True)
        await s.w.tick()
        assert not await s.w.calls_listing()
        await next_call(s)
        assert len(await s.w.calls_listing()) == 1

    asyncio.run(go())


def test_finish_and_pause_do_not_cause_automatic_restart_of_same_call():
    async def go():
        s = setup()
        leave_call(s)
        await s.w.command(ROOM, ALICE, "auto_transcribe", "enable", True)
        join_call(s)
        await s.w.tick()
        sid = (await s.w.calls_listing())[0]["session_id"]
        await s.w.command(ROOM, ALICE, "pause", "pause")
        await s.w.tick()
        assert (await s.w.calls_listing())[0]["recording"] is False
        await finish(s)
        await s.w.tick()
        assert not await s.w.calls_listing()
        await next_call(s)
        assert (await s.w.calls_listing())[0]["session_id"] != sid

    asyncio.run(go())


def test_reconnect_and_backend_restart_keep_one_automatic_meeting():
    async def go():
        s = setup()
        leave_call(s)
        await s.w.command(ROOM, ALICE, "auto_transcribe", "enable", True)
        join_call(s)
        await s.w.tick()
        sid = (await s.w.calls_listing())[0]["session_id"]
        leave_call(s)
        await s.w.tick()
        s.clock.now += 2
        join_call(s, "RECONNECT")
        s.w = Widget(s.w.config, s.db, s.access, s.gw, clock=s.clock)
        await s.w.tick()
        assert (await s.w.calls_listing())[0]["session_id"] == sid

    asyncio.run(go())


@pytest.mark.parametrize("action", ["auto_transcribe", "settings"])
def test_only_room_admin_can_change_room_policy(action):
    async def go():
        s = setup()
        with pytest.raises(PermissionError):
            await s.w.command(ROOM, BOB, action, "unauthorized", True)
        with pytest.raises(ValueError):
            await s.w.command(ROOM, ALICE, action, "invalid", "true")

    asyncio.run(go())


def test_installation_defaults_enroll_room_and_publish_without_widget_session():
    async def go():
        s = setup()
        s.w.config.widget_auto_transcribe_default = True
        s.w.config.widget_auto_publish_default = True
        leave_call(s)
        # No command(), status(), login or browser session is called.
        await s.w.tick()
        join_call(s)
        await s.w.tick()
        sid = (await s.w.calls_listing())[0]["session_id"]
        s.gw.put(sid, "complete", {"01_Executive_Summary.md": ("summary", b"Summary")})
        leave_call(s)
        await s.w.tick()
        s.clock.now += s.w.config.call_end_grace_s + 1
        await s.w.tick()
        await s.w.tick()
        assert list(s.access.sent) == [sid]

    asyncio.run(go())


def test_presets_never_overwrite_admin_override_and_publish_setting_is_room_wide():
    async def go():
        s = setup()
        s.w.config.widget_auto_transcribe_default = True
        s.w.config.widget_auto_publish_default = True
        leave_call(s)
        await s.w.tick()
        await s.w.command(ROOM, ALICE, "auto_transcribe", "disable", False)
        await s.w.command(ROOM, ALICE, "settings", "no-publish", False)
        await s.w.tick()
        join_call(s)
        await s.w.tick()
        assert not await s.w.calls_listing()
        status = await s.w.status(ROOM, BOB)
        assert status["auto_transcribe"] is False and status["auto_publish"] is False

    asyncio.run(go())


def test_defaults_are_off_and_low_privilege_installer_cannot_enable_defaults():
    async def go():
        s = setup()
        await s.w.tick()
        assert not await s.w.calls_listing()
        s.w.config.widget_auto_transcribe_default = True
        s.access.room.power_users[ALICE] = 50
        await s.w.tick()
        assert not await s.w.calls_listing()

    asyncio.run(go())


def test_widget_removal_disables_automation_and_does_not_reapply_defaults():
    async def go():
        s = setup()
        s.w.config.widget_auto_transcribe_default = True
        leave_call(s)
        await s.w.tick()
        s.access.installed = False
        await s.w.tick()
        s.access.installed = True
        join_call(s)
        await s.w.tick()
        assert not await s.w.calls_listing()
        assert (await s.w.status(ROOM, ALICE))["auto_transcribe"] is False

    asyncio.run(go())


def test_installation_in_existing_call_waits_for_next_call():
    async def go():
        s = setup()
        s.w.config.widget_auto_transcribe_default = True
        s.access.room.widget_added_at = s.clock()
        s.access.room.rtc_started_at = s.clock() - 10
        await s.w.tick()
        assert not await s.w.calls_listing()
        await next_call(s)
        assert len(await s.w.calls_listing()) == 1

    asyncio.run(go())


def test_automatic_discovery_never_takes_over_a_running_bot_meeting():
    async def go():
        from types import SimpleNamespace

        s = setup()
        s.w.config.widget_auto_transcribe_default = True
        s.w.legacy = SimpleNamespace(calls={ROOM: object()})
        await s.w.tick()
        assert not await s.w.managed_rooms()
        assert not await s.w.calls_listing()

    asyncio.run(go())


def test_first_admin_action_also_applies_presets_without_overwriting_that_action():
    async def go():
        s = setup()
        s.w.config.widget_auto_transcribe_default = True
        s.w.config.widget_auto_publish_default = True
        leave_call(s)
        # The user saves a room override before the discovery timer runs.
        await s.w.command(ROOM, ALICE, "settings", "disable-publish", False)
        join_call(s)
        await s.w.tick()
        [call] = await s.w.calls_listing()
        assert call["recording"] is True
        assert (await s.w.status(ROOM, BOB))["auto_publish"] is False

    asyncio.run(go())
