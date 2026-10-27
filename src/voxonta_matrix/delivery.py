"""Taking a finished meeting's results from the gateway and putting them in its room.

The Nextcloud app's CollectArtifacts, for Matrix. The call ended and its audio
is on the gateway; the transcript is ready within seconds, the analysis an hour
or two later. Each tick asks about the meetings that are due, publishes once
nothing further will come, and backs off from the ones that keep answering
nothing — so a meeting that will never arrive steps aside instead of standing
in front of those that are ready.

Where this differs from Nextcloud, it is because a room has no folder:

* a meeting whose analysis failed still gets its transcript published. In
  Nextcloud the file is written and shared either way; here the room is the
  only place it can go. The meeting is then still watched, because the gateway
  re-runs a failed analysis, and what that produces joins the same thread;
* a meeting left at "transcribed" — the plan has no analysis, the gateway
  moves on to "analyzing" at once otherwise — is published after a while
  rather than never;
* finished meetings are kept for a month instead of being forgotten. The
  gateway lists a meeting until every file is acknowledged; if the
  acknowledgement was lost, a forgotten meeting would look new and be
  announced twice. A remembered one is opened again, finds everything already
  published, and acknowledges.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable

from voxonta_matrix.gateway import GatewayUnavailable, UnknownMeeting
from voxonta_matrix.matrix import MatrixError
from voxonta_matrix.publication import announcement, choose, file_message, file_name
from voxonta_matrix.store import Meeting

logger = logging.getLogger(__name__)

# The platform name this integration's meetings carry on the gateway (the
# capture service sets it from the call's platform). The gateway key may be
# shared with the Nextcloud app; each takes only its own.
SOURCE = "element-call"

# Meetings asked about per tick: one request each, plus the files.
BATCH = 5
# After an answer that brought nothing, wait this long, doubling up to the cap.
BACKOFF_BASE_S = 60
BACKOFF_CAP_S = 6 * 3600
# The gateway keeps a meeting for a week; a fortnight of not managing to
# publish it is something retrying will not fix.
GIVE_UP_AFTER_S = 14 * 24 * 3600
# The gateway records a meeting with the call's first frame. A day later and
# still unknown, its audio never got there.
UNKNOWN_GRACE_S = 24 * 3600
# How long "transcribed" may stand before it is taken to mean "no analysis".
NO_ANALYSIS_AFTER_S = 15 * 60
DONE_MEMORY_S = 30 * 24 * 3600


class Delivery:
    def __init__(self, store, gateway, matrix, *, is_live: Callable[[str], bool],
                 clock=time.time) -> None:
        self.store = store
        self.gateway = gateway
        self.matrix = matrix
        self._is_live = is_live
        self._clock = clock

    async def tick(self) -> None:
        now = self._clock()
        await self._adopt(now)
        for meeting in (await self.store.due_meetings(now))[:BATCH]:
            try:
                await self._deliver(meeting, now)
            except Exception:
                # One meeting's trouble must not hold up the others.
                logger.exception("delivering %s failed", meeting.session_id[:8])
                await self._miss(meeting, now)
        await self.store.prune_meetings(now - DONE_MEMORY_S)

    # ── what the gateway holds that we do not ─────────────────────────

    async def _adopt(self, now: float) -> None:
        """Take on meetings the gateway holds files for and this side forgot,
        and reopen finished ones that have files again."""
        adopted = reopened = 0
        for row in await self.gateway.undelivered([SOURCE]):
            session_id = str(row["session_id"])
            if row.get("source") != SOURCE or self._is_live(session_id):
                # Another platform's (a gateway that ignored the filter), or a
                # call still going on: its meeting is recorded when it ends.
                continue
            known = await self.store.get_meeting(session_id)
            if known is None:
                room_id = str(row.get("room_token") or "")
                if not room_id.startswith("!"):
                    logger.warning("the gateway holds files for %s but no room to "
                                   "publish them in", session_id[:8])
                    continue
                await self.store.add_meeting(Meeting(
                    session_id, room_id, str(row.get("room_name") or ""), 0, int(now)))
                adopted += 1
            elif known.done_at is not None:
                known.done_at, known.misses, known.next_try = None, 0, 0.0
                await self.store.save_meeting(known)
                reopened += 1
        if adopted:
            logger.warning("took back %d meeting(s) the gateway still had files for", adopted)
        if reopened:
            logger.info("%d finished meeting(s) have unacknowledged files again", reopened)

    # ── one meeting ───────────────────────────────────────────────────

    async def _deliver(self, meeting: Meeting, now: float) -> None:
        sid = meeting.session_id
        if now - meeting.ended_at > GIVE_UP_AFTER_S:
            logger.warning("stopped watching %s in %s after a fortnight (%d file(s) "
                           "published)", sid[:8], meeting.room_id, len(meeting.published))
            await self._finish(meeting, now)
            return
        try:
            state = await self.gateway.meeting(sid)
        except UnknownMeeting:
            if now - meeting.ended_at > UNKNOWN_GRACE_S:
                logger.warning("giving up on %s: the gateway has not known it for a day",
                               sid[:8])
                await self._finish(meeting, now)
            else:
                await self._miss(meeting, now)
            return
        except GatewayUnavailable as e:
            logger.info("could not ask the gateway about %s: %s", sid[:8], e)
            await self._miss(meeting, now)
            return

        settled = state.final or (state.status == "transcribed"
                                  and now - meeting.ended_at >= NO_ANALYSIS_AFTER_S)
        if not settled:
            await self._answered(meeting)
            return

        summary, transcript = choose(state.artifacts)
        try:
            await self._publish(meeting, summary, transcript)
        except MatrixError as e:
            if e.status != 403:
                raise
            # Removed from the room, or not allowed to speak in it: the room
            # has said it does not want this. The files stay where they are.
            logger.warning("cannot post the results of %s in %s: %s", sid[:8],
                           meeting.room_id, e)
            await self.gateway.ack(sid, [a.sha256 for a in state.artifacts])
            await self._finish(meeting, now)
            return

        # Everything listed is dealt with: published, or not for the room.
        await self.gateway.ack(sid, [a.sha256 for a in state.artifacts])
        if state.status == "failed":
            logger.warning("%s finished badly, still watching: %s", sid[:8], state.detail)
            await self._miss(meeting, now)
        else:
            await self._finish(meeting, now)

    async def _publish(self, meeting: Meeting, summary, transcript) -> None:
        """Announce once, then each file not yet in the room, into its thread.

        Saved after every step, so a failure halfway resumes where it stopped.
        """
        fresh = [a for a in (summary, transcript)
                 if a is not None and a.sha256 not in meeting.published]
        if not fresh:
            if summary is None and transcript is None:
                logger.info("%s produced nothing to publish", meeting.session_id[:8])
            return
        contents = {a.sha256: await self.gateway.artifact(meeting.session_id, a.sha256)
                    for a in fresh}

        if not meeting.thread_root:
            text = contents.get(summary.sha256) if summary is not None else None
            meeting.thread_root = await self.matrix.send(
                meeting.room_id, announcement(text),
                txn_id=f"voxonta.{meeting.session_id}.announcement")
            await self.store.save_meeting(meeting)

        for artifact in fresh:
            content = contents[artifact.sha256]
            name = file_name(artifact, content)
            url = await self.matrix.upload(content, name, "text/markdown; charset=utf-8")
            await self.matrix.send(
                meeting.room_id, file_message(name, url, len(content), meeting.thread_root),
                txn_id=f"voxonta.{meeting.session_id}.{artifact.sha256[:16]}")
            meeting.published.append(artifact.sha256)
            await self.store.save_meeting(meeting)
        logger.info("published %d file(s) of %s in %s", len(fresh), meeting.session_id[:8],
                    meeting.room_id)

    # ── the queue ─────────────────────────────────────────────────────

    async def _miss(self, meeting: Meeting, now: float) -> None:
        meeting.misses += 1
        wait = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (meeting.misses - 1))
        meeting.next_try = now + wait
        await self.store.save_meeting(meeting)
        if wait >= BACKOFF_CAP_S:
            logger.warning("%s: %d attempts without a result, asking every %dh",
                           meeting.session_id[:8], meeting.misses, wait // 3600)

    async def _answered(self, meeting: Meeting) -> None:
        if meeting.misses or meeting.next_try:
            meeting.misses, meeting.next_try = 0, 0.0
            await self.store.save_meeting(meeting)

    async def _finish(self, meeting: Meeting, now: float) -> None:
        meeting.done_at = now
        await self.store.save_meeting(meeting)
