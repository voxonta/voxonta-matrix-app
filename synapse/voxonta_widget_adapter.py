"""Voxonta adapter for Synapse 1.163.2. Deploy only on the internal listener.

Uses Module API for room state and its own module tables for recent sticky RTC
and publication receipts. Sending uses a version-pinned Synapse handler because
Module API's send method does not expose transaction ids.
"""

import hashlib
import hmac
import json
import time
from urllib.parse import urlsplit

from synapse.api.errors import SynapseError
from synapse.http.server import DirectServeJsonResource
from synapse.http.servlet import parse_json_object_from_request
from synapse.types import create_requester

TYPES = {
    "m.room.create",
    "m.room.member",
    "m.room.power_levels",
    "m.room.name",
    "m.room.canonical_alias",
    "m.room.encryption",
    "org.matrix.msc3401.call.member",
}
RTC = {"m.rtc.member", "org.matrix.msc4143.rtc.member"}


class VoxontaWidgetAdapter:
    @staticmethod
    def parse_config(config):
        for key in ("token", "widget_url"):
            if not isinstance(config.get(key), str) or not config[key]:
                raise ValueError(f"{key} is required")
        if len(config["token"]) < 32:
            raise ValueError("adapter token must have at least 32 characters")
        rooms = config.get("backfill_rooms", [])
        if not isinstance(rooms, list) or any(
            not isinstance(r, str) or not r.startswith("!") for r in rooms
        ):
            raise ValueError("backfill_rooms must be room IDs")
        return config

    def __init__(self, config, api):
        self.api, self.config = api, config
        self.key = config.get("widget_key", "voxonta")
        self.backfilled = False
        api.register_third_party_rules_callbacks(
            on_new_event=self.on_event,
            on_create_room=self.on_create_room,
            check_event_allowed=self.check_event_allowed,
        )
        api.register_web_resource("/_synapse/client/voxonta/widget", Endpoint(self))

    async def schema(self):
        def create(txn):
            txn.execute(
                "CREATE TABLE IF NOT EXISTS voxonta_widget_rtc "
                "(event_id TEXT PRIMARY KEY, room_id TEXT NOT NULL, ts BIGINT NOT NULL, body TEXT NOT NULL)"
            )
            txn.execute(
                "CREATE TABLE IF NOT EXISTS voxonta_widget_delivery "
                "(operation TEXT PRIMARY KEY, created BIGINT NOT NULL, event_id TEXT)"
            )

            txn.execute(
                "CREATE TABLE IF NOT EXISTS voxonta_widget_rooms "
                "(room_id TEXT PRIMARY KEY, sender TEXT NOT NULL, installed_at BIGINT NOT NULL)"
            )

        await self.api.run_db_interaction("voxonta_schema", create)

    def installed(self, state):
        event = state.get(("im.vector.modular.widgets", self.key))
        return bool(event and event.content.get("url") == self.config["widget_url"])

    async def check_event_allowed(self, event, state):
        if event.type == "m.room.message" and event.content.get("org.voxonta.meeting"):
            return self.installed(state), None
        return True, None

    async def on_create_room(self, requester, config, is_requester_admin):
        if not self.config.get("auto_install", False):
            return
        if config.get("creation_content", {}).get("type") or not self.api.is_mine(
            requester.user.to_string()
        ):
            return
        initial = list(config.get("initial_state") or [])
        if not any(
            e.get("type") == "im.vector.modular.widgets" and e.get("state_key") == self.key
            for e in initial
        ):
            initial.append(
                {
                    "type": "im.vector.modular.widgets",
                    "state_key": self.key,
                    "content": {
                        "id": self.key,
                        "type": "m.custom",
                        "name": "Voxonta",
                        "creatorUserId": requester.user.to_string(),
                        "waitForIframeLoad": True,
                        "url": self.config["widget_url"],
                    },
                }
            )
        config["initial_state"] = initial

    async def remember_room(self, room_id, widget):
        await self.schema()

        def save(txn):
            if widget and widget.content.get("url") == self.config["widget_url"]:
                txn.execute(
                    "INSERT INTO voxonta_widget_rooms VALUES (?, ?, ?) "
                    "ON CONFLICT (room_id) DO UPDATE SET sender=excluded.sender, installed_at=excluded.installed_at",
                    (room_id, widget.sender, widget.origin_server_ts),
                )
            else:
                txn.execute("DELETE FROM voxonta_widget_rooms WHERE room_id=?", (room_id,))

        await self.api.run_db_interaction("voxonta_widget_room", save)

    async def rooms(self):
        await self.schema()
        if not self.backfilled:
            for room_id in self.config.get("backfill_rooms", []):
                state = await self.api.get_room_state(room_id)
                await self.remember_room(
                    room_id, state.get(("im.vector.modular.widgets", self.key))
                )
            self.backfilled = True

        def read(txn):
            txn.execute(
                "SELECT room_id, sender, installed_at FROM voxonta_widget_rooms ORDER BY room_id"
            )
            return [{"room_id": r[0], "sender": r[1], "installed_at": r[2]} for r in txn.fetchall()]

        return await self.api.run_db_interaction("voxonta_widget_rooms", read)

    async def on_event(self, event, state):
        if (
            event.type in RTC
            or event.type == "org.matrix.msc3401.call.member"
            or (event.type == "im.vector.modular.widgets" and event.state_key == self.key)
        ):
            await self.remember_room(
                event.room_id, state.get(("im.vector.modular.widgets", self.key))
            )
        if event.type not in RTC or not self.installed(state):
            return
        await self.schema()
        now = int(time.time() * 1000)

        def save(txn):
            txn.execute("DELETE FROM voxonta_widget_rtc WHERE ts < ?", (now - 3600000,))
            txn.execute(
                "INSERT INTO voxonta_widget_rtc VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (
                    event.event_id,
                    event.room_id,
                    event.origin_server_ts,
                    json.dumps(
                        {**event.get_dict(), "event_id": event.event_id, "room_id": event.room_id}
                    ),
                ),
            )

        await self.api.run_db_interaction("voxonta_rtc", save)

    async def snapshot(self, room_id):
        state = await self.api.get_room_state(room_id)
        await self.schema()

        def read(txn):
            txn.execute(
                "SELECT body, event_id FROM voxonta_widget_rtc WHERE room_id=? AND ts>=? ORDER BY ts, event_id",
                (room_id, int(time.time() * 1000) - 3600000),
            )
            return [
                {**json.loads(row[0]), "event_id": row[1], "room_id": room_id}
                for row in txn.fetchall()
            ]

        return state, await self.api.run_db_interaction("voxonta_recent_rtc", read)

    async def publish(self, state, body):
        sender, sid, room_id = body["sender"], body["meeting_id"], body["room_id"]
        member = state.get(("m.room.member", sender))
        if not self.api.is_mine(sender) or not member or member.content.get("membership") != "join":
            raise SynapseError(403, "publisher no longer a local member")
        links = body["links"]
        expected = urlsplit(self.config["widget_url"])
        if not isinstance(links, dict) or not links or set(links) - {"summary", "transcript"}:
            raise SynapseError(400, "invalid result links")
        for link in links.values():
            u = urlsplit(link)
            if (u.scheme, u.netloc, u.path.rstrip("/")) != (
                expected.scheme,
                expected.netloc,
                expected.path.rstrip("/"),
            ):
                raise SynapseError(400, "result must point to this widget")
        op = hashlib.sha256((room_id + "\0" + sid).encode()).hexdigest()
        now = int(time.time())

        def claim(txn):
            txn.execute(
                "INSERT INTO voxonta_widget_delivery VALUES (?, ?, NULL) ON CONFLICT DO NOTHING",
                (op, now),
            )
            txn.execute(
                "SELECT created, event_id FROM voxonta_widget_delivery WHERE operation=?", (op,)
            )
            return txn.fetchone()

        created, event_id = await self.api.run_db_interaction("voxonta_publish_claim", claim)
        if event_id:
            return event_id
        if now - created > 3600:
            # Synapse eventually prunes transaction ids. Never blindly repeat an
            # ambiguous old send once its deduplication guarantee may have expired.
            raise SynapseError(409, "publication needs operator reconciliation")
        labels = {"summary": "Саммари", "transcript": "Расшифровка"}
        text = "Помощник встреч Voxonta: встреча расшифрована\n" + "\n".join(
            f"{labels[k]}: {v}" for k, v in sorted(links.items())
        )
        # Transaction receipts reference a real device row in Synapse. Register
        # a named service device without issuing or retaining an access token.
        await self.api._hs.get_device_handler().check_device_registered(
            sender, "voxonta-widget", "Voxonta: публикация результатов"
        )
        requester = create_requester(
            sender, device_id="voxonta-widget", authenticated_entity=self.api._server_name
        )
        event, _ = await self.api._hs.get_event_creation_handler().create_and_send_nonmember_event(
            requester,
            {
                "type": "m.room.message",
                "room_id": room_id,
                "sender": sender,
                "content": {"msgtype": "m.text", "body": text, "org.voxonta.meeting": sid},
            },
            txn_id="voxonta." + op,
            ratelimit=True,
        )

        def done(txn):
            txn.execute(
                "UPDATE voxonta_widget_delivery SET event_id=? WHERE operation=?",
                (event.event_id, op),
            )

        await self.api.run_db_interaction("voxonta_publish_done", done)
        return event.event_id


class Endpoint(DirectServeJsonResource):
    def __init__(self, adapter):
        super().__init__()
        self.adapter = adapter

    async def _async_render_POST(self, request):
        supplied = request.getHeader("Authorization") or ""
        if not hmac.compare_digest(
            supplied.encode(), ("Bearer " + self.adapter.config["token"]).encode()
        ):
            raise SynapseError(403, "forbidden")
        body = parse_json_object_from_request(request)
        if body.get("action") == "rooms":
            return 200, {"rooms": await self.adapter.rooms()}
        room_id = body.get("room_id")
        if not isinstance(room_id, str) or not room_id.startswith("!") or len(room_id) > 255:
            raise SynapseError(400, "invalid room")
        state, recent = await self.adapter.snapshot(room_id)
        installed = self.adapter.installed(state)
        if body.get("action") == "snapshot":
            return 200, {
                "widget": installed,
                "widget_added_at": (
                    state[("im.vector.modular.widgets", self.adapter.key)].origin_server_ts
                    if installed
                    else 0
                ),
                "state": [e.get_dict() for (t, _), e in state.items() if t in TYPES],
                "rtc": recent,
            }
        if body.get("action") == "publish" and installed:
            return 200, {"event_id": await self.adapter.publish(state, body)}
        raise SynapseError(403, "widget not installed or action not allowed")
