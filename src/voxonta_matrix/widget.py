"""Standalone widget lifecycle, durable commands, archive and publication outbox."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import time
import uuid
from urllib.parse import urlencode

from voxonta_matrix.gateway import GatewayUnavailable, UnknownMeeting
from voxonta_matrix.livekit import capture_token
from voxonta_matrix.publication import choose
from voxonta_matrix.room_access import AccessUnavailable
from voxonta_matrix.rtc import livekit_room

LEASE_S = 4
REPORT_TTL_S = 4
MAX_FILE_BYTES = 4 * 1024 * 1024


class Widget:
    def __init__(self, config, store, access, gateway, *, clock=time.time, legacy=None):
        self.config, self.store, self.access, self.gateway = config, store, access, gateway
        self.clock, self.legacy = clock, legacy

    async def authorize(self, room_id, user, *, admin=False):
        room, installed = await self.access.snapshot(room_id, self.clock())
        if not installed or user not in room.members:
            raise PermissionError("Вы больше не участник комнаты или виджет удалён")
        if admin and not room.is_admin(user):
            raise PermissionError("Управлять расшифровкой может администратор комнаты")
        return room

    async def command(self, room_id, user, action, request_id, value=None):
        if action not in {"start", "pause", "resume", "finish", "settings", "publish", "revoke"}:
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
                room_id, {"settings": {}, "meetings": [], "call": None}
            )
            call = record["call"]
            if action == "settings":
                if not isinstance(value, bool):
                    raise ValueError("Настройка должна быть true или false")
                record["settings"][user] = value
            elif action == "start":
                if call is None:
                    if room.encrypted:
                        raise ValueError("Зашифрованные звонки пока не поддерживаются")
                    if not room.active(int(now * 1000)):
                        raise ValueError("Сначала войдите в звонок")
                    call = {
                        "session_id": str(uuid.uuid4()),
                        "room_id": room_id,
                        "started_at": int(now),
                        "revision": 1,
                        "recording": True,
                        "initiator": user,
                        "auto_publish": record["settings"].get(user, False),
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
                    call and call.get("initiator") == user and call["auto_publish"]
                ),
                "auto_publish": record.get("settings", {}).get(user, False),
                "meetings": meetings,
            }

    async def tick(self, *, collect=True):
        now = self.clock()
        async with self.store.transaction() as state:
            room_ids = list(state["rooms"])

        async def snapshot(room_id):
            try:
                result = await asyncio.wait_for(self.access.snapshot(room_id, self.clock()), 1.5)
                return room_id, result
            except (AccessUnavailable, PermissionError, TimeoutError):
                return room_id, None

        snapshots = dict(await asyncio.gather(*(snapshot(r) for r in room_ids)))
        async with self.store.transaction() as state:
            for room_id, record in state["rooms"].items():
                if snapshots.get(room_id) is None:
                    continue
                room, installed = snapshots[room_id]
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
            state["commands"] = {
                k: v for k, v in state["commands"].items() if now - v["at"] < 30 * 86400
            }
        if collect:
            await self.collect()

    async def collect(self):
        # Gateway/download latency must never hold the command/capture lease lock.
        async with self.store.transaction() as state:
            records = copy.deepcopy(state["rooms"])
        for room_id, record in records.items():
            try:
                room, installed = await self.access.snapshot(room_id, self.clock())
            except (AccessUnavailable, PermissionError):
                continue
            call = record.get("call")
            if call:
                await self.gateway.audience(call["session_id"], call["audience"])
            for original in record["meetings"]:
                updated = copy.deepcopy(original)
                await self._collect(updated, room, installed, self.clock())
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

    async def _collect(self, meeting, room, installed, now):
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
                        meeting["publisher"] = meeting["initiator"]
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
            base = self.config.widget_public_url.rstrip("/")
            links = {
                kind: base + "/?" + urlencode({"roomId": room_id, "meeting": sid, "open": kind})
                for kind in meeting["artifacts"]
            }
            meeting["delivery_status"] = "sending"
        try:
            answer = await self.access.publish(room_id, publisher, sid, links)
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
