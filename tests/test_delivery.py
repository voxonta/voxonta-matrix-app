"""Results reach the room once, whole, and nothing is posted twice.

The gateway and the homeserver are fakes with the real ones' answers; the clock
is stepped by hand. What is pinned: when a meeting is published, what the room
gets, what happens when either side fails halfway, and how meetings this side
forgot are taken back without announcing anything a second time.
"""
from __future__ import annotations

import asyncio

from conftest import FORBIDDEN, ROOM, T0, Clock, FakeGateway, FakeMatrix

from voxonta_matrix import delivery as d
from voxonta_matrix.delivery import Delivery
from voxonta_matrix.store import Call, Meeting, MemoryStore

SID = "11111111-2222-4333-8444-555555555555"

SUMMARY = ("---\nmeeting_date: '2026-10-27'\n---\n"
           "# Executive Summary: Дейли (Вт, 27 октября 2026)\n\n## Главное\n- Решили.\n").encode()
ENRICHED = ("---\nmeeting_name: Дейли\nmeeting_date: '2026-10-27'\n---\n"
            "# Enriched Transcript: Дейли\n\n**Алиса**: Начнём.\n").encode()
PLAIN = "**Алиса** [00:01]: Начнём.\n".encode()

FREE = {"2026-10-27 - Дейли.md": ("transcript", PLAIN),
        "01_Executive_Summary.md": ("summary", SUMMARY)}
FULL = {**FREE,
        "09_Enriched_Transcript.md": ("analysis", ENRICHED),
        "02_Next_Steps.md": ("analysis", b"steps"),
        "stats.json": ("stats", b"{}")}


class Setup:
    def __init__(self, *, live: set[str] | None = None) -> None:
        self.clock = Clock(T0 + 3600)
        self.store = MemoryStore()
        self.gateway = FakeGateway()
        self.matrix = FakeMatrix()
        # Retries here come later than the homeserver remembers transactions:
        # what keeps them from posting twice has to be this side's own record.
        self.matrix.remember_txns = False
        self.live = live or set()
        self.delivery = Delivery(self.store, self.gateway, self.matrix,
                                 is_live=lambda sid: sid in self.live, clock=self.clock)

    async def ended(self, sid=SID, *, ago: float = 60) -> None:
        end = int(self.clock.now - ago)
        await self.store.end_call(Call(ROOM, sid, end - 1800),
                                  Meeting(sid, ROOM, "Дейли", end - 1800, end))

    async def tick(self, after: float = 0) -> None:
        self.clock.now += after
        await self.delivery.tick()

    async def meeting(self, sid=SID) -> Meeting:
        return await self.store.get_meeting(sid)


def run(coro):
    return asyncio.run(coro)


def digests(files):
    import hashlib
    return sorted(hashlib.sha256(body).hexdigest() for _, body in files.values())


# ── when ──────────────────────────────────────────────────────────────

def test_nothing_is_posted_while_the_analysis_is_running():
    async def go():
        s = Setup()
        await s.ended()
        s.gateway.put(SID, "analyzing", {"2026-10-27 - Дейли.md": ("transcript", PLAIN)})
        await s.tick()
        await s.tick(60)
        return s

    s = run(go())
    assert s.matrix.sent == [] and s.gateway.acked == []
    assert len(s.gateway.asked) == 2   # asked every tick, no backoff: it answered


def test_a_complete_meeting_gets_the_summary_and_two_files_in_a_thread():
    async def go():
        s = Setup()
        await s.ended()
        s.gateway.put(SID, "complete", FULL)
        await s.tick()
        return s, await s.meeting()

    s, meeting = run(go())
    [message] = s.matrix.announcements()
    assert message["body"].startswith("Встреча расшифрована: Дейли (Вт, 27 октября 2026)")
    assert "Решили." in message["body"]
    root = s.matrix.sent[0][3]
    assert s.matrix.files() == [
        ("2026-10-27 Дейли — итоги.md", SUMMARY, root),
        ("2026-10-27 Дейли — расшифровка.md", ENRICHED, root),
    ]
    # Everything is acknowledged, published or not: the rest is not for the room.
    [(acked_sid, acked)] = s.gateway.acked
    assert acked_sid == SID and sorted(acked) == digests(FULL)
    assert meeting.done_at is not None and meeting.thread_root == root


def test_on_the_free_plan_the_plain_transcript_is_published():
    async def go():
        s = Setup()
        await s.ended()
        s.gateway.put(SID, "complete", FREE)
        await s.tick()
        return s

    s = run(go())
    assert [name for name, _, _ in s.matrix.files()] == [
        "2026-10-27 Дейли — итоги.md", "2026-10-27 - Дейли.md"]


def test_a_meeting_without_analysis_is_published_once_it_has_stood_a_while():
    # The gateway moves straight on to "analyzing" when the plan has analysis;
    # "transcribed" that stays is a plan without it, which never goes final.
    async def go():
        s = Setup()
        await s.ended(ago=60)
        s.gateway.put(SID, "transcribed", {"2026-10-27 - Дейли.md": ("transcript", PLAIN)})
        await s.tick()
        before = len(s.matrix.sent)
        await s.tick(d.NO_ANALYSIS_AFTER_S)
        return s, before, await s.meeting()

    s, before, meeting = run(go())
    assert before == 0
    assert [m["body"] for m in s.matrix.announcements()] == ["Встреча расшифрована"]
    assert [name for name, _, _ in s.matrix.files()] == ["2026-10-27 - Дейли.md"]
    assert meeting.done_at is not None


def test_a_failed_analysis_still_brings_the_transcript_and_a_rerun_joins_the_thread():
    async def go():
        s = Setup()
        await s.ended()
        s.gateway.put(SID, "failed", {"2026-10-27 - Дейли.md": ("transcript", PLAIN)},
                      detail="analysis timed out")
        await s.tick()
        first = await s.meeting()
        # The analysis is re-run later; its files appear against the same meeting.
        s.gateway.put(SID, "complete", FULL)
        await s.tick(first.next_try - s.clock.now)
        return s, first, await s.meeting()

    s, first, last = run(go())
    assert first.done_at is None and first.next_try > s.clock.now - 3600  # still watched
    assert len(s.matrix.announcements()) == 1                            # announced once
    root = s.matrix.sent[0][3]
    assert [(name, thread) for name, _, thread in s.matrix.files()] == [
        ("2026-10-27 - Дейли.md", root),
        ("2026-10-27 Дейли — итоги.md", root),
        ("2026-10-27 Дейли — расшифровка.md", root),
    ]
    assert last.done_at is not None


def test_a_meeting_that_produced_nothing_is_closed_quietly():
    async def go():
        s = Setup()
        await s.ended()
        s.gateway.put(SID, "complete", {})
        await s.tick()
        return s, await s.meeting()

    s, meeting = run(go())
    assert s.matrix.sent == [] and meeting.done_at is not None


# ── failing halfway ───────────────────────────────────────────────────

def test_a_failure_halfway_resumes_without_posting_anything_twice():
    async def go():
        s = Setup()
        await s.ended()
        s.gateway.put(SID, "complete", FULL)
        uploads = 0
        real_upload = s.matrix.upload

        async def second_upload_fails(*args):
            nonlocal uploads
            uploads += 1
            if uploads == 2:
                raise ConnectionResetError("homeserver went away")
            return await real_upload(*args)

        s.matrix.upload = second_upload_fails
        await s.tick()
        half = await s.meeting()
        await s.tick(half.next_try - s.clock.now)
        return s, half, await s.meeting()

    s, half, done = run(go())
    assert len(half.published) == 1 and half.done_at is None
    assert len(s.matrix.announcements()) == 1
    assert [name for name, _, _ in s.matrix.files()] == [
        "2026-10-27 Дейли — итоги.md", "2026-10-27 Дейли — расшифровка.md"]
    assert done.done_at is not None


def test_a_lost_answer_to_the_announcement_does_not_post_it_twice():
    # The homeserver made the event but the answer never came back: the same
    # transaction id on the retry returns that event instead of a second one.
    async def go():
        s = Setup()
        s.matrix.remember_txns = True
        await s.ended()
        s.gateway.put(SID, "complete", FREE)
        real_send = s.matrix.send

        async def answer_lost_once(room, content, txn_id):
            s.matrix.send = real_send
            await real_send(room, content, txn_id)
            raise TimeoutError

        s.matrix.send = answer_lost_once
        await s.tick()
        await s.tick((await s.meeting()).next_try - s.clock.now)
        return s

    s = run(go())
    assert len(s.matrix.announcements()) == 1
    assert len(s.matrix.files()) == 2


def test_a_room_that_removed_the_bot_is_not_retried():
    async def go():
        s = Setup()
        await s.ended()
        s.gateway.put(SID, "complete", FREE)
        s.matrix.fail_send = FORBIDDEN
        await s.tick()
        return s, await s.meeting()

    s, meeting = run(go())
    assert meeting.done_at is not None
    assert s.gateway.acked and s.matrix.sent == []


def test_a_damaged_file_is_not_published_and_is_asked_for_again():
    async def go():
        s = Setup()
        await s.ended()
        s.gateway.put(SID, "complete", FREE)
        s.gateway.corrupt = {digests({"x": ("summary", SUMMARY)})[0]}
        await s.tick()
        first = await s.meeting()
        s.gateway.corrupt = set()
        await s.tick(first.next_try - s.clock.now)
        return s, first

    s, first = run(go())
    assert first.misses == 1 and first.published == []
    assert len(s.matrix.announcements()) == 1 and len(s.matrix.files()) == 2


def test_an_unreachable_gateway_is_asked_less_and_less_often():
    async def go():
        s = Setup()
        await s.ended()
        s.gateway.down = True
        waits = []
        for _ in range(12):
            m = await s.meeting()
            await s.tick(max(0.0, m.next_try - s.clock.now))
            waits.append((await s.meeting()).next_try - s.clock.now)
        return waits

    waits = run(go())
    assert waits[:4] == [60, 120, 240, 480]
    assert max(waits) == d.BACKOFF_CAP_S


def test_a_meeting_the_gateway_never_heard_of_is_dropped_after_a_day():
    async def go():
        s = Setup()
        await s.ended()
        await s.tick()
        early = await s.meeting()
        await s.tick(d.UNKNOWN_GRACE_S)
        return early, await s.meeting()

    early, late = run(go())
    assert early.done_at is None and late.done_at is not None


def test_a_meeting_is_given_up_after_a_fortnight():
    async def go():
        s = Setup()
        await s.ended(ago=d.GIVE_UP_AFTER_S + 1)
        s.gateway.put(SID, "analyzing")
        await s.tick()
        return s, await s.meeting()

    s, meeting = run(go())
    assert meeting.done_at is not None and s.gateway.asked == []


def test_a_handful_of_meetings_per_tick():
    async def go():
        s = Setup()
        for n in range(7):
            await s.ended(f"{n}" * 8)
        await s.tick()
        return s

    assert len(run(go()).gateway.asked) == d.BATCH


# ── meetings this side forgot ─────────────────────────────────────────

def test_a_forgotten_meeting_is_taken_back_and_published():
    async def go():
        s = Setup()
        s.gateway.put(SID, "complete", FREE)
        s.gateway.rows = [{"session_id": SID, "source": "element-call",
                           "room_token": ROOM, "room_name": "Дейли"}]
        await s.tick()
        return s

    s = run(go())
    assert s.matrix.sent[0][0] == ROOM
    assert len(s.matrix.files()) == 2


def test_other_platforms_and_live_calls_are_left_alone():
    async def go():
        s = Setup(live={"live-call"})
        s.gateway.rows = [
            # Room-like ids on purpose: the platform alone must keep them out.
            {"session_id": "talk-meeting", "source": "nc-talk", "room_token": "!a:x.org"},
            {"session_id": "old-meeting", "source": "", "room_token": "!b:x.org"},
            {"session_id": "live-call", "source": "element-call", "room_token": ROOM},
            {"session_id": "no-room", "source": "element-call", "room_token": ""},
        ]
        await s.tick()
        return s

    s = run(go())
    assert run(s.store.pending_meetings()) == []


def test_a_finished_meeting_listed_again_is_acknowledged_not_reannounced():
    # The acknowledgement was lost: the gateway still lists the meeting. It
    # must not look new.
    async def go():
        s = Setup()
        await s.ended()
        s.gateway.put(SID, "complete", FREE)
        await s.tick()
        s.gateway.rows = [{"session_id": SID, "source": "element-call", "room_token": ROOM}]
        await s.tick(60)
        return s, await s.meeting()

    s, meeting = run(go())
    assert len(s.matrix.announcements()) == 1 and len(s.matrix.files()) == 2
    assert len(s.gateway.acked) == 2 and meeting.done_at is not None


def test_finished_meetings_are_forgotten_after_a_month():
    async def go():
        s = Setup()
        await s.ended()
        s.gateway.put(SID, "complete", {})
        await s.tick()
        await s.tick(d.DONE_MEMORY_S + 1)
        return await s.meeting()

    assert run(go()) is None
