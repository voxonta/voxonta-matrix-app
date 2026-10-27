"""The integration's own logic: which rooms it serves, when a call starts and ends.

The same job the Nextcloud app does for Talk, on Matrix:

* a room is served once a local user invites the bot into it, and stops being
  served when the bot is removed. Calls in rooms without the bot — direct calls
  included — are none of its business;
* a call starts with the first live device in the room's call, and ends once
  nobody has been in it for `call_end_grace_s`. A reconnect, or someone moving
  from laptop to phone, briefly leaves the room empty; ending the call there
  would cut the meeting in two;
* each call gets a session id when it starts and keeps it until it ends, across
  restarts of this service. The capture service streams under it.

The capture service learns all of this from `calls()`, which it polls.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

import aiohttp

from voxonta_matrix.livekit import capture_token
from voxonta_matrix.matrix import MatrixError
from voxonta_matrix.rooms import Room
from voxonta_matrix.rtc import livekit_room
from voxonta_matrix.store import Call, Meeting

logger = logging.getLogger(__name__)

# Transactions are remembered for a day: the homeserver retries a failed one
# within minutes, never a day later.
TXN_MEMORY_S = 24 * 3600


class TryAgain(Exception):
    """A failure worth another attempt: the transaction is not acknowledged,
    and the homeserver sends it again later. Joining a room the bot was just
    invited to is the case that matters — dropped, the invite is lost for good."""


def _transient(e: Exception) -> bool:
    if isinstance(e, MatrixError):
        return e.status >= 500 or e.status == 429
    return isinstance(e, (aiohttp.ClientError, asyncio.TimeoutError))


class Integration:
    def __init__(self, config, store, matrix, clock=time.time) -> None:
        self.config = config
        self.store = store
        self.matrix = matrix
        self._clock = clock
        self.bot = config.bot_user_id
        self.rooms: dict[str, Room] = {}
        self.calls: dict[str, Call] = {}
        # Transactions and the periodic check both change calls; one at a time.
        self._lock = asyncio.Lock()
        # Set once start() has read the rooms. The listeners are up before
        # that, and a transaction applied to an empty room list would be
        # acknowledged with its events dropped.
        self._ready = asyncio.Event()

    # ── starting up ───────────────────────────────────────────────────

    async def start(self) -> None:
        """Pick up where the last run left off.

        Calls come from the store, rooms from the homeserver. A call that went
        on while this service was down is still running under its old session
        id; one that ended meanwhile is ended after the grace period, as if its
        last device had just left.
        """
        async with self._lock:
            await self.matrix.ensure_bot(self.config.bot_displayname)
            self.calls = {c.room_id: c for c in await self.store.load_calls()}
            for room_id in await self.matrix.joined_rooms():
                try:
                    await self._read_room(room_id)
                except TryAgain as e:
                    # Not fatal at start: the room's next event reads it again.
                    logger.warning("could not read room %s: %s", room_id, e.__cause__)
            logger.info("serving %d room(s), %d call(s) live",
                        len(self.rooms), len(self.calls))
            await self._reconcile()
        self._ready.set()

    async def _read_room(self, room_id: str) -> None:
        try:
            state = await self.matrix.room_state(room_id)
        except Exception as e:
            if _transient(e):
                raise TryAgain() from e
            logger.warning("could not read room %s: %s", room_id, e)
            return
        room = Room(room_id)
        for event in state:
            room.apply(event)
        self.rooms[room_id] = room

    # ── events from the homeserver ────────────────────────────────────

    async def on_transaction(self, txn_id: str, events: list[dict]) -> None:
        """Apply one transaction, once.

        The homeserver repeats a transaction until it is acknowledged, so the
        same one can arrive twice. Applying state twice is harmless; announcing
        twice would not be, so a seen id is skipped.

        Raises TryAgain, leaving the transaction unacknowledged, when something
        failed that a later attempt can fix. The events before it are applied
        again then, which state allows.
        """
        await self._ready.wait()
        async with self._lock:
            if await self.store.txn_seen(txn_id):
                return
            for event in events:
                try:
                    await self._on_event(event)
                except TryAgain:
                    raise
                except Exception:
                    # One bad event must not stall the stream: the homeserver
                    # would retry this transaction forever.
                    logger.exception("could not handle %s in %s",
                                     event.get("type"), event.get("room_id"))
            await self._reconcile()
            now = self._clock()
            await self.store.mark_txn(txn_id, now)
            await self.store.prune_txns(now - TXN_MEMORY_S)

    async def _on_event(self, event: dict) -> None:
        room_id = event.get("room_id")
        if not room_id:
            return
        if event.get("type") == "m.room.member" and event.get("state_key") == self.bot:
            await self._on_bot_membership(room_id, event)
            return
        room = self.rooms.get(room_id)
        if room is not None:
            room.apply(event)

    async def _on_bot_membership(self, room_id: str, event: dict) -> None:
        membership = (event.get("content") or {}).get("membership")
        sender = str(event.get("sender") or "")
        if membership == "invite":
            if not self._is_local(sender):
                # A room on another server is a room whose people never agreed
                # to this installation's terms; refuse rather than sit there.
                logger.warning("declining invite to %s from %s: not a local user",
                               room_id, sender)
                try:
                    await self.matrix.leave(room_id)
                except (TimeoutError, MatrixError, aiohttp.ClientError) as e:
                    logger.warning("could not decline invite to %s: %s", room_id, e)
                return
            try:
                await self.matrix.join(room_id)
            except Exception as e:
                if _transient(e):
                    raise TryAgain() from e
                logger.warning("could not join %s (invited by %s): %s", room_id, sender, e)
                return
            logger.info("joined %s, invited by %s", room_id, sender)
            await self._read_room(room_id)
        elif membership == "join":
            if room_id not in self.rooms:
                await self._read_room(room_id)
        elif membership in ("leave", "ban"):
            if self.rooms.pop(room_id, None) is not None:
                logger.info("no longer in %s (%s by %s)", room_id, membership, sender)

    def _is_local(self, user_id: str) -> bool:
        return user_id.split(":", 1)[-1] == self.config.server_name

    # ── call lifecycle ────────────────────────────────────────────────

    async def reconcile(self) -> None:
        """Start, keep or end calls from what the rooms say right now.

        Run after every transaction and on a timer: a device that disappears
        without leaving only stops counting when its membership expires, and
        nothing pushes an event for that.
        """
        await self._ready.wait()
        async with self._lock:
            await self._reconcile()

    async def _reconcile(self) -> None:
        now = self._clock()
        now_ms = int(now * 1000)
        for room_id, room in self.rooms.items():
            active = room.active(now_ms)
            call = self.calls.get(room_id)
            if not active:
                if call is None:
                    continue
                if call.empty_since is None:
                    call.empty_since = now
                    await self.store.save_call(call)
                elif now - call.empty_since >= self.config.call_end_grace_s:
                    await self._end(call, room, now)
                continue

            changed = False
            if call is None:
                call = Call(room_id, str(uuid.uuid4()), int(now))
                self.calls[room_id] = call
                changed = True
                logger.info("call started in %s (%s) [%s]",
                            room.display_name(self.bot), room_id, call.session_id[:8])
            if call.empty_since is not None:
                call.empty_since = None
                changed = True
            for membership in active:
                entry = {"name": room.name_of(membership.user_id),
                         "user": membership.user_id}
                for identity in membership.identities():
                    if call.members.get(identity) != entry:
                        call.members[identity] = entry
                        changed = True
            if changed:
                await self.store.save_call(call)

        # Rooms the bot has left: whatever was being captured there is over.
        for room_id in [r for r in self.calls if r not in self.rooms]:
            await self._end(self.calls[room_id], None, now)

    async def _end(self, call: Call, room: Room | None, now: float) -> None:
        name = room.display_name(self.bot) if room is not None else call.room_id
        await self.store.end_call(call, Meeting(call.session_id, call.room_id, name,
                                                call.started_at, int(now)))
        del self.calls[call.room_id]
        logger.info("call ended in %s (%s) [%s] after %d min", name, call.room_id,
                    call.session_id[:8], (int(now) - call.started_at) // 60)

    # ── what the capture service sees ─────────────────────────────────

    def calls_listing(self) -> list[dict]:
        """The live calls in the shape the capture service reads from /calls.

        The same fields the Nextcloud app sends, plus what a LiveKit call
        needs: where to connect, a token signed for this call alone, and who
        each LiveKit identity is. A fresh token on every poll, so one that is
        about to expire is never what the capture service connects with.
        """
        now = self._clock()
        out = []
        for call in sorted(self.calls.values(), key=lambda c: c.started_at):
            room = self.rooms.get(call.room_id)
            if room is None:
                continue
            lk_room = livekit_room(call.room_id)
            out.append({
                "token": call.room_id,
                "name": room.display_name(self.bot),
                "type": room.kind(self.bot),
                "started_at": call.started_at,
                "session_id": call.session_id,
                "recording": call.recording,
                "platform": "livekit",
                "livekit": {
                    "url": self.config.livekit_url,
                    "room": lk_room,
                    "token": capture_token(
                        self.config.livekit_api_key, self.config.livekit_api_secret,
                        identity=f"voxonta:{call.session_id}", room=lk_room, now=now),
                },
                "members": dict(call.members),
            })
        return out
