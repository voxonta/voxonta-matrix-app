"""Run against the disposable Synapse described in docs/widget.md, never production."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
import time
import urllib.request
import urllib.error
import urllib.parse

BASE = "http://127.0.0.1:18770"


def req(method, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    r = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(r, timeout=15) as f:
            return f.status, json.load(f)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def ok(method, path, body=None, token=None):
    code, b = req(method, path, body, token)
    assert code < 300, (code, b)
    return b


for _ in range(30):
    try:
        ok("GET", "/_matrix/client/versions")
        break
    except (OSError, AssertionError):
        time.sleep(1)
a = ok(
    "POST",
    "/_matrix/client/v3/register",
    {
        "username": "alice" + str(time.time_ns()),
        "password": "probe-only",
        "auth": {"type": "m.login.dummy"},
    },
)
b = ok(
    "POST",
    "/_matrix/client/v3/register",
    {
        "username": "bob" + str(time.time_ns()),
        "password": "probe-only",
        "auth": {"type": "m.login.dummy"},
    },
)
c = ok(
    "POST",
    "/_matrix/client/v3/register",
    {
        "username": "charlie" + str(time.time_ns()),
        "password": "probe-only",
        "auth": {"type": "m.login.dummy"},
    },
)
at = a["access_token"]
room = ok(
    "POST",
    "/_matrix/client/v3/createRoom",
    {"preset": "private_chat", "invite": [b["user_id"]]},
    at,
)["room_id"]
path = "/_matrix/client/v3/rooms/" + urllib.parse.quote(room, safe="")
ok("POST", path + "/join", {}, b["access_token"])
ADAPTER = "/_synapse/client/voxonta/widget"
T = "local-disposable-adapter-test-token-only"
body = {"action": "snapshot", "room_id": room}
assert req("POST", ADAPTER, body)[0] == 403
s = ok("POST", ADAPTER, body, T)
assert s["widget"]
assert (
    len(
        [
            e
            for e in s["state"]
            if e["type"] == "m.room.member" and e["content"]["membership"] == "join"
        ]
    )
    == 2
)
assert any(e.get("state_key") == "voxonta" for e in ok("GET", path + "/state", token=at))
# Native sticky RTC: the adapter must persist it for snapshots and restarts.
rtc = {
    "slot_id": "m.call#ROOM",
    "sticky_key": "alice-device",
    "application": {"type": "m.call"},
    "member": {"user_id": a["user_id"], "device_id": "LAPTOP", "id": "membership"},
    "transports": {"published": [{"type": "livekit", "url": "https://jwt.example"}]},
}
rtc_event = ok(
    "PUT", path + "/send/m.rtc.member/sticky?org.matrix.msc4354.sticky_duration_ms=3600000", rtc, at
)
for _ in range(20):
    snapshot = ok("POST", ADAPTER, body, T)
    if any(e["event_id"] == rtc_event["event_id"] for e in snapshot["rtc"]):
        break
    time.sleep(0.1)
assert any(e["event_id"] == rtc_event["event_id"] for e in snapshot["rtc"])
publication = {
    "action": "publish",
    "room_id": room,
    "sender": a["user_id"],
    "meeting_id": "test-meeting",
    "links": {
        "summary": "https://voxonta.example/widget/?roomId="
        + room
        + "&meeting=test-meeting&open=summary"
    },
}
one = ok("POST", ADAPTER, publication, T)
two = ok("POST", ADAPTER, publication, T)
assert one["event_id"] == two["event_id"]
with ThreadPoolExecutor(max_workers=4) as workers:
    answers = list(workers.map(lambda _: ok("POST", ADAPTER, publication, T), range(4)))
assert {answer["event_id"] for answer in answers} == {one["event_id"]}

assert (
    req("POST", ADAPTER, {**publication, "sender": c["user_id"], "meeting_id": "other"}, T)[0]
    == 403
)
assert (
    req(
        "POST",
        ADAPTER,
        {**publication, "meeting_id": "third", "links": {"summary": "https://attacker.example/a"}},
        T,
    )[0]
    == 400
)
ok(
    "PUT",
    path + "/state/im.vector.modular.widgets/recorder",
    {"url": "https://recorder.example/"},
    at,
)
ok("PUT", path + "/state/im.vector.modular.widgets/voxonta", {}, at)
assert not ok("POST", ADAPTER, body, T)["widget"]
assert req("POST", ADAPTER, {**publication, "meeting_id": "removed"}, T)[0] == 403
assert (
    ok("GET", path + "/state/im.vector.modular.widgets/recorder", token=at)["url"]
    == "https://recorder.example/"
)
print(
    "Synapse adapter E2E: PASS (auth, auto-install, 2 members without bot, native sticky RTC, idempotent/concurrent publication, member/url checks, widget removal preserves recorder)"
)

if os.environ.get("VOXONTA_E2E_CONTEXT"):
    with open(os.environ["VOXONTA_E2E_CONTEXT"], "w") as output:
        json.dump({"room_id": room, "rtc_event_id": rtc_event["event_id"]}, output)
