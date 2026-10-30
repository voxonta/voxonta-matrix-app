"""Authenticated metadata adapter; no AS credential or ordinary chat history."""

from __future__ import annotations

import aiohttp

from voxonta_matrix.rooms import Room
from voxonta_matrix.rtc import RTC_MEMBER_TYPES, parse_legacy_member, parse_rtc_member


class AccessUnavailable(Exception):
    pass


class RoomAccess:
    def __init__(self, base_url, token, session):
        self.base = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}
        self.session = session

    async def request(self, action, **body):
        try:
            async with self.session.post(
                self.base,
                json={"action": action, **body},
                headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as response:
                if response.status == 403:
                    raise PermissionError("Доступ к комнате отозван")
                if response.status != 200:
                    raise AccessUnavailable(f"adapter HTTP {response.status}")
                return await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as e:
            raise AccessUnavailable("Адаптер Matrix недоступен") from e

    async def rooms(self):
        data = await self.request("rooms")
        if not isinstance(data.get("rooms"), list):
            raise AccessUnavailable("Неверный список комнат адаптера")
        return data["rooms"]

    async def snapshot(self, room_id, now):
        data = await self.request("snapshot", room_id=room_id)
        room = Room(room_id)
        for event in data["state"]:
            room.apply(event)
        for event in data.get("rtc", []):
            if event.get("type") in RTC_MEMBER_TYPES:
                room.apply_rtc(event, int(now * 1000))
        room.widget_added_at = data.get("widget_added_at", 0) / 1000
        starts = []
        for event in [*data["state"], *data.get("rtc", [])]:
            if event.get("type") == "org.matrix.msc3401.call.member":
                membership = parse_legacy_member(event)
            elif event.get("type") in RTC_MEMBER_TYPES:
                entry = parse_rtc_member(event, int(now * 1000))
                membership = entry.membership if entry else None
            else:
                continue
            if membership and membership.active(int(now * 1000)):
                starts.append(
                    event.get("content", {}).get("created_ts") or event.get("origin_server_ts", 0)
                )
        room.rtc_started_at = min(starts, default=0) / 1000
        return room, data["widget"]

    async def publish(self, room_id, sender, meeting_id, links):
        return await self.request(
            "publish", room_id=room_id, sender=sender, meeting_id=meeting_id, links=links
        )
