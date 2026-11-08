"""Standalone widget lifecycle, durable commands, archive and publication outbox."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import logging
import re
import time
import uuid
from datetime import UTC, datetime

from voxonta_matrix.gateway import GatewayUnavailable, UnknownMeeting
from voxonta_matrix.livekit import capture_token
from voxonta_matrix.publication import choose
from voxonta_matrix.room_access import AccessUnavailable
from voxonta_matrix.rtc import livekit_room

logger = logging.getLogger(__name__)

LEASE_S = 4
REPORT_TTL_S = 4
MAX_FILE_BYTES = 4 * 1024 * 1024
SNAPSHOT_CONCURRENCY = 16
ROOM_STATE_TIMEOUT_S = 1.5
CAPTURE_START_GRACE_S = 15


class Widget:
    def __init__(self, config, store, access, gateway, *, clock=time.time, legacy=None):
        self.config, self.store, self.access, self.gateway = config, store, access, gateway
        self.clock, self.legacy = clock, legacy
        self._discovery_warning_at = float("-inf")
        self._snapshot_warning_at = float("-inf")
        self._snapshot_slots = asyncio.Semaphore(SNAPSHOT_CONCURRENCY)
        self._room_tasks: dict[str, asyncio.Task] = {}

    async def authorize(self, room_id, user, *, admin=False):
        room, installed = await self.access.snapshot(room_id, self.clock())
        if not installed or user not in room.members:
            raise PermissionError("Вы больше не участник комнаты или виджет удалён")
        if admin and not room.is_admin(user):
            raise PermissionError("Управлять расшифровкой может администратор комнаты")
        return room

    async def command(self, room_id, user, action, request_id, value=None):
        if action not in {
            "start",
            "pause",
            "resume",
            "finish",
            "settings",
            "auto_transcribe",
            "publish",
            "revoke",
        }:
            raise ValueError("Неизвестное действие")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
            raise ValueError("Нужен request_id")
        room = await self.authorize(room_id, user, admin=action not in {"publish", "revoke"})
        now = self.clock()
        async with self.store.transaction() as state:
            key = hashlib.sha256(f"{room_id}\0{user}\0{request_id}".encode()).hexdigest()
            fingerprint = [action, value]
            previous = state["commands"].get(key)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ValueError("request_id уже использован для другой команды")
                return previous["result"]
            # Do not silently take over a bot-mode room in the middle of a call.
            if self.legacy and room_id in self.legacy.calls:
                raise ValueError("Сначала завершите встречу в режиме бота")
            record = state["rooms"].setdefault(
                room_id,
                {
                    "settings": {},
                    "meetings": [],
                    "call": None,
                    "auto_transcribe": self.config.widget_auto_transcribe_default,
                    "auto_publish": self.config.widget_auto_publish_default,
                    "configured_by": user,
                    "publication_by": user,
                    "rtc_live": bool(room.active(int(now * 1000))),
                    "rtc_empty_since": None,
                },
            )
            call = record["call"]
            if action == "settings":
                if not isinstance(value, bool):
                    raise ValueError("Настройка должна быть true или false")
                record["settings"][user] = value  # Preserve existing personal preferences.
                record["auto_publish"], record["publication_by"] = value, user
            elif action == "auto_transcribe":
                if not isinstance(value, bool):
                    raise ValueError("Настройка должна быть true или false")
                record["auto_transcribe"], record["configured_by"] = value, user
                # A policy change never silently starts the call already on air.
                record["rtc_live"] = bool(room.active(int(now * 1000)))
                record["rtc_empty_since"] = None
            elif action == "start":
                if call is None:
                    if room.encrypted:
                        raise ValueError("Зашифрованные звонки пока не поддерживаются")
                    if not room.active(int(now * 1000)):
                        raise ValueError("Сначала войдите в звонок")
                    call = self._new_call(record, room, user, now)
                    record["rtc_live"] = True
                    record["rtc_empty_since"] = None
            elif action in {"pause", "resume", "finish"}:
                if call is None:
                    raise ValueError("Нет активной расшифровки")
                if call.get("finishing") and action != "finish":
                    raise ValueError("Встреча уже завершается")
                call["revision"] += 1
                call["recording"] = action == "resume"
                call["phase"] = "starting" if action == "resume" else "stopping"
                if action == "finish":
                    call["audience"] = [{"id": u, "name": n} for u, n in room.members.items()]
                    call["finishing"] = True
                    call["finish_after"] = now + LEASE_S + 1
            elif action == "revoke":
                target = (
                    call
                    if call and call["session_id"] == value
                    else next((m for m in record["meetings"] if m["session_id"] == value), None)
                )
                if target is None or user not in {target.get("initiator"), target.get("publisher")}:
                    raise PermissionError("Можно отозвать только своё разрешение")
                if target["delivery_status"] in {"sending", "posted"}:
                    raise ValueError("Отправка уже началась; сначала нужно проверить её результат")
                target["auto_publish"] = False
                if target["delivery_status"] != "posted":
                    target["delivery_status"] = "revoked"
            elif action == "publish":
                meeting = next((m for m in record["meetings"] if m["session_id"] == value), None)
                if meeting is None or not meeting["artifacts"]:
                    raise ValueError("Результат ещё не готов")
                if user not in {p["id"] for p in meeting["audience"]}:
                    raise PermissionError("Встреча недоступна")
                if meeting["delivery_status"] not in {"posted", "sending"}:
                    meeting["publisher"] = user
                    meeting["delivery_status"] = "pending"
            if record["call"]:
                for membership in room.active(int(now * 1000)):
                    for identity in membership.identities():
                        record["call"]["members"][identity] = {
                            "user": membership.user_id,
                            "name": room.name_of(membership.user_id),
                        }
                record["capture"] = self._capture(record["call"], room, now)
            result = {"request_id": request_id, "session_id": call["session_id"] if call else None}
            state["commands"][key] = {"fingerprint": fingerprint, "result": result, "at": now}
        return result

    @staticmethod
    def _new_call(record, room, user, now, *, automatic=False):
        publisher = record.get("publication_by", user)
        call = {
            "session_id": str(uuid.uuid4()),
            "automatic": automatic,
            "room_id": room.room_id,
            "started_at": int(now),
            "revision": 1,
            "recording": True,
            "initiator": user,
            "publisher": publisher,
            "auto_publish": record.get("auto_publish", record["settings"].get(user, False))
            and publisher in room.members,
            "members": {},
            "audience": [{"id": u, "name": n} for u, n in room.members.items()],
            "empty_since": None,
            "ended_at": None,
            "artifacts": {},
            "delivery_status": "off",
            "phase": "starting",
            "name": room.display_name(""),
        }
        record["call"] = call
        logger.info(
            "%s transcription started in %s [%s]",
            "automatic" if automatic else "manual",
            room.room_id,
            call["session_id"][:8],
        )
        return call

    @staticmethod
    def _end(record, now):
        call = record["call"]
        call["ended_at"], call["recording"], call["phase"] = int(now), False, "processing"
        record["meetings"].append(call)
        record["call"] = None

    async def status(self, room_id, user):
        room = await self.authorize(room_id, user)
        async with self.store.transaction() as state:
            record = state["rooms"].get(room_id, {})
            call = record.get("call")
            shown = dict(call) if call else None
            if shown:
                report = state["reports"].get(shown["session_id"], {})
                if (
                    self.clock() - report.get("at", 0) <= REPORT_TTL_S
                    and report.get("revision") == shown["revision"]
                ):
                    shown["phase"] = report.get("phase", "unknown")
                elif shown["phase"] not in {"starting", "stopping"}:
                    shown["phase"] = "unknown"
            meetings = [
                {
                    "session_id": m["session_id"],
                    "phase": m["phase"],
                    "name": m["name"],
                    "delivery_status": m["delivery_status"],
                    "artifacts": list(m["artifacts"]),
                    "can_revoke": (
                        user in {m.get("initiator"), m.get("publisher")}
                        and m["delivery_status"] not in {"sending", "posted", "revoked"}
                    ),
                }
                for m in reversed(record.get("meetings", []))
                if user in {p["id"] for p in m["audience"]}
            ]
            return {
                "room_name": room.display_name(""),
                "can_control": room.is_admin(user),
                "call": None
                if shown is None
                else {k: shown[k] for k in ("session_id", "phase", "revision")},
                "can_revoke_call": bool(
                    call
                    and user in {call.get("initiator"), call.get("publisher")}
                    and call["auto_publish"]
                ),
                "auto_publish": record.get(
                    "auto_publish", record.get("settings", {}).get(user, False)
                ),
                "auto_transcribe": record.get("auto_transcribe", False),
                "meetings": meetings,
            }

    async def tick(self, *, collect=True, wait_for_rooms=True):
        now = self.clock()
        async with self.store.transaction() as state:
            # Keep observed calls until their grace period has elapsed.
            room_ids = {
                room_id
                for room_id, record in state["rooms"].items()
                if record.get("call") or record.get("rtc_live")
            }
            auto_enabled = {
                room_id
                for room_id, record in state["rooms"].items()
                if record.get("auto_transcribe")
            }
            state["commands"] = {
                k: v for k, v in state["commands"].items() if now - v["at"] < 30 * 86400
            }

        # Renew existing leases even while discovery is waiting on Synapse.
        tasks = [self._schedule_room(room_id) for room_id in room_ids]
        try:
            installed, activity = await asyncio.wait_for(
                self.access.activity(), ROOM_STATE_TIMEOUT_S
            )
            candidates = {r["room_id"]: r for r in activity}
            candidates_to_check = (candidates.keys() & auto_enabled) | (auto_enabled - installed)
            defaults = (
                self.config.widget_auto_transcribe_default
                or self.config.widget_auto_publish_default
            )
            if defaults:
                candidates_to_check.update(candidates)
            tasks.extend(
                self._schedule_room(room_id, candidates.get(room_id) if defaults else None)
                for room_id in candidates_to_check - room_ids
            )
        except (AccessUnavailable, PermissionError, TimeoutError) as error:
            if now - self._discovery_warning_at >= 60:
                logger.warning("call activity discovery unavailable: %s", error)
                self._discovery_warning_at = now
        if wait_for_rooms:
            await asyncio.gather(*tasks)
        if collect:
            await self.collect()

    def _schedule_room(self, room_id, metadata=None):
        task = self._room_tasks.get(room_id)
        if task is None:
            task = asyncio.create_task(self._refresh_room(room_id, metadata))
            self._room_tasks[room_id] = task
        return task

    async def stop(self):
        tasks = list(self._room_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _refresh_room(self, room_id, metadata):
        try:
            async with self._snapshot_slots:
                room, installed = await asyncio.wait_for(
                    self.access.snapshot(room_id, self.clock()), ROOM_STATE_TIMEOUT_S
                )
                await self._apply_room(room_id, metadata, room, installed)
        except (AccessUnavailable, PermissionError, TimeoutError) as error:
            now = self.clock()
            if now - self._snapshot_warning_at >= 60:
                logger.warning("call state unavailable in %s: %s", room_id, error)
                self._snapshot_warning_at = now
        except Exception:
            logger.exception("updating call state in %s failed", room_id)
        finally:
            self._room_tasks.pop(room_id, None)

    async def _apply_room(self, room_id, metadata, room, installed):
        async with self.store.transaction() as state:
            # Compute the lease after acquiring the store lock, never from the
            # start of discovery or from another room's response time.
            now = self.clock()
            record = state["rooms"].get(room_id)
            if record is None:
                if metadata is None or (self.legacy and room_id in self.legacy.calls):
                    return
                actor = metadata.get("sender")
                if not installed or actor not in room.members or not room.is_admin(actor):
                    return
                # Installation during an existing call applies on the next call.
                already_live = bool(
                    room.active(int(now * 1000))
                    and getattr(room, "rtc_started_at", now) < getattr(room, "widget_added_at", 0)
                )
                record = state["rooms"][room_id] = {
                    "settings": {},
                    "meetings": [],
                    "call": None,
                    "auto_transcribe": self.config.widget_auto_transcribe_default,
                    "auto_publish": self.config.widget_auto_publish_default,
                    "configured_by": actor,
                    "publication_by": actor,
                    "rtc_live": already_live,
                    "rtc_empty_since": None,
                }
            if not installed:
                record["auto_transcribe"] = False
            active = room.active(int(now * 1000))
            if not active:
                if record.get("rtc_empty_since") is None:
                    record["rtc_empty_since"] = now
                if now - record["rtc_empty_since"] >= self.config.call_end_grace_s:
                    record["rtc_live"] = False
            else:
                record["rtc_empty_since"] = None
                if not record.get("rtc_live", bool(record["call"])):
                    record["rtc_live"] = True
                    if (
                        installed
                        and not room.encrypted
                        and record.get("auto_transcribe")
                        and record["call"] is None
                    ):
                        self._new_call(
                            record, room, record["configured_by"], now, automatic=True
                        )
            call = record["call"]
            if call and call.get("finishing"):
                report = state["reports"].get(call["session_id"], {})
                confirmed = (
                    report.get("revision") == call["revision"]
                    and report.get("phase") == "paused"
                    and now - report.get("at", 0) <= REPORT_TTL_S
                )
                if confirmed:
                    self._end(record, now)
                    call = None
                elif now >= call["finish_after"]:
                    call["phase"] = "unknown"
            if not call:
                record["capture"] = None
            if call:
                call["audience"] = [{"id": u, "name": n} for u, n in room.members.items()]
                active = room.active(int(now * 1000))
                if not installed:
                    record["auto_transcribe"] = False
                    self._end(record, now)
                    record["capture"] = None
                elif not active:
                    if call["empty_since"] is None:
                        call["empty_since"] = now
                    if now - call["empty_since"] >= self.config.call_end_grace_s:
                        self._end(record, now)
                        record["capture"] = None
                else:
                    call["empty_since"] = None
                if record["call"]:
                    for member in active:
                        for identity in member.identities():
                            call["members"][identity] = {
                                "user": member.user_id,
                                "name": room.name_of(member.user_id),
                            }
                    record["capture"] = self._capture(call, room, now)

    async def collect(self):
        # Gateway/download latency must never hold the command/capture lease lock.
        async with self.store.transaction() as state:
            records = copy.deepcopy(state["rooms"])
        for room_id, record in records.items():
            call = record.get("call")
            if call:
                await self.gateway.audience(call["session_id"], call["audience"])
            for original in record["meetings"]:
                updated = copy.deepcopy(original)
                await self._collect(updated, self.clock())
                async with self.store.transaction() as state:
                    meetings = state["rooms"][room_id]["meetings"]
                    for index, current in enumerate(meetings):
                        if current["session_id"] == original["session_id"] and current == original:
                            meetings[index] = updated
                            break
                if original.get("archive_committed"):
                    await self._publish_pending(room_id, original["session_id"])

    def _capture(self, call, room, now):
        return {
            "token": room.room_id,
            "name": call["name"],
            "type": room.kind(""),
            "session_id": call["session_id"],
            "started_at": call["started_at"],
            "platform": "livekit",
            "source": "element-call",
            "recording": call["recording"],
            "revision": call["revision"],
            "lease_until": now + LEASE_S,
            "members": call["members"],
            "livekit": {
                "url": self.config.livekit_url,
                "room": livekit_room(room.room_id),
                "token": capture_token(
                    self.config.livekit_api_key,
                    self.config.livekit_api_secret,
                    identity=f"voxonta:{call['session_id']}",
                    room=livekit_room(room.room_id),
                    now=now,
                ),
            },
        }

    async def calls_listing(self):
        async with self.store.transaction() as state:
            return [r["capture"] for r in state["rooms"].values() if r.get("capture")]

    async def managed_rooms(self):
        async with self.store.transaction() as state:
            return set(state["rooms"])

    async def unconfirmed_calls(self):
        now = self.clock()
        async with self.store.transaction() as state:
            count = 0
            for record in state["rooms"].values():
                call = record.get("call")
                if not call or not call["recording"] or not call.get("automatic"):
                    continue
                report = state["reports"].get(call["session_id"], {})
                if now - call["started_at"] >= CAPTURE_START_GRACE_S and (
                    report.get("revision") != call["revision"]
                    or report.get("phase") != "transcribing"
                    or now - report.get("at", 0) > REPORT_TTL_S
                ):
                    count += 1
            return count

    async def report(self, reports):
        async with self.store.transaction() as state:
            for report in reports[:100]:
                if not isinstance(report, dict):
                    continue
                sid = report.get("session_id")
                if any(
                    r.get("call") and r["call"]["session_id"] == sid
                    for r in state["rooms"].values()
                ):
                    phase = report.get("phase")
                    if phase in {"starting", "transcribing", "paused", "unknown"}:
                        state["reports"][sid] = {
                            "phase": phase,
                            "revision": report.get("revision"),
                            "at": self.clock(),
                        }

    async def result(self, room_id, user, sid, kind):
        await self.authorize(room_id, user)
        async with self.store.transaction() as state:
            record = state["rooms"].get(room_id, {})
            meeting = next((m for m in record.get("meetings", []) if m["session_id"] == sid), None)
            if meeting is None or user not in {p["id"] for p in meeting["audience"]}:
                raise PermissionError("Встреча недоступна")
            artifact = meeting["artifacts"].get(kind)
            if not artifact:
                raise ValueError("Материал ещё не готов")
            if "content" in artifact:
                return base64.b64decode(artifact["content"])
        return await self.store.artifact(room_id, sid, kind)

    async def _collect(self, meeting, now):
        sid = meeting["session_id"]
        try:
            await self.gateway.audience(sid, meeting["audience"])
            if meeting["phase"] not in {"ready", "empty"}:
                status = await self.gateway.meeting(sid)
                if status.final or (
                    status.status == "transcribed" and now - meeting["ended_at"] >= 900
                ):
                    for kind, artifact in zip(("summary", "transcript"), choose(status.artifacts)):
                        if (
                            artifact
                            and meeting["artifacts"].get(kind, {}).get("sha256") != artifact.sha256
                        ):
                            content = await self.gateway.artifact(sid, artifact.sha256)
                            if len(content) > MAX_FILE_BYTES:
                                raise GatewayUnavailable("widget artifact exceeds 4 MiB")
                            meeting["archive_committed"] = False
                            meeting["artifacts"][kind] = {
                                "content": base64.b64encode(content).decode(),
                                "sha256": artifact.sha256,
                            }
                    meeting["phase"] = "failed" if status.status == "failed" else "ready"
                    if not meeting["artifacts"] and meeting["phase"] == "ready":
                        meeting["phase"] = "empty"
                    if (
                        meeting["artifacts"]
                        and meeting["auto_publish"]
                        and meeting["delivery_status"] == "off"
                    ):
                        meeting["publisher"] = meeting.get("publisher", meeting["initiator"])
                        meeting["delivery_status"] = "pending"
        except UnknownMeeting:
            if now - meeting["ended_at"] > 86400:
                meeting["phase"] = "failed"
            return
        except GatewayUnavailable:
            return
        # Acknowledgement is deliberately in a later tick, after the preceding
        # transaction containing the durable bytes has committed.
        if meeting.get("archive_committed"):
            await self.gateway.ack(sid, [a["sha256"] for a in meeting["artifacts"].values()])
        elif meeting["artifacts"]:
            meeting["archive_committed"] = True
            return

    async def _publish_pending(self, room_id, sid):
        # Claim the dispatch atomically against revocation. Once sending begins,
        # a timeout is ambiguous and must be reconciled, not reported as revoked.
        try:
            room, installed = await self.access.snapshot(room_id, self.clock())
        except (AccessUnavailable, PermissionError):
            return
        async with self.store.transaction() as state:
            meeting = next(m for m in state["rooms"][room_id]["meetings"] if m["session_id"] == sid)
            if meeting["delivery_status"] not in {"pending", "sending"}:
                return
            publisher = meeting["publisher"]
            if not installed or publisher not in room.members:
                meeting["delivery_status"] = "revoked"
                return
            files = {}
            for kind in ("summary", "transcript"):
                artifact = meeting["artifacts"].get(kind)
                if artifact is None:
                    continue
                content = artifact.get("content")
                if content is None:
                    content = base64.b64encode(await self.store.artifact(room_id, sid, kind)).decode()
                date = datetime.fromtimestamp(meeting["started_at"], UTC).strftime("%Y-%m-%d")
                name = re.sub(r'[\x00-\x1f\x7f/\\]', ' ', meeting["name"]).strip()[:100]
                label = "Саммари" if kind == "summary" else "Расшифровка"
                files[kind] = {"content": content, "filename": f"{date} {name} — {label}.md"}
            meeting["delivery_status"] = "sending"
        try:
            answer = await self.access.publish(room_id, publisher, sid, files)
            outcome = "posted"
        except PermissionError:
            outcome, answer = "revoked", {}
        except AccessUnavailable:
            return
        async with self.store.transaction() as state:
            meeting = next(m for m in state["rooms"][room_id]["meetings"] if m["session_id"] == sid)
            meeting["delivery_status"] = outcome
            if answer:
                meeting["event_id"] = answer["event_id"]
