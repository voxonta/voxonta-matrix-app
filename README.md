# voxonta-matrix-app

Transcribes Element Call meetings for [Voxonta](https://voxonta.com): invite the
bot into a Matrix room, and the calls held in that room are captured,
transcribed and analysed.

This is the Matrix counterpart of the [Nextcloud app](https://github.com/voxonta/voxonta-nextcloud-app):
it owns everything that is about the platform — which rooms are in scope, when a
call starts and ends, who is who — and hands the call to the
[capture service](https://github.com/voxonta/nextcloud-talk-capture), which joins
it and streams the audio to the Voxonta gateway.

## Three parts, one of them here

```
the homeserver + this app             the capture service                  the cloud
─────────────────────────             ───────────────────                  ─────────
invites, room state, call      ──▶    polls /calls, joins the call    ──▶  transcription
membership events                     (LiveKit, listen-only, hidden)       and analysis
serves the calls and a LiveKit                                                  │
token for each                                                                  │
posts the results in the room  ◀────────────── finished files ──────────────────┘
```

Media never passes through this service. It runs next to the homeserver; the
capture service runs wherever there is CPU to spare and reaches it over HTTPS.

## What works today

| | |
|---|---|
| ✅ | the bot accepts invites from users of its own server, declines others, forgets rooms it is removed from |
| ✅ | a call starts with the first device in the room's call and ends after `CALL_END_GRACE_S` of nobody — a reconnect or a switch of device does not split the meeting |
| ✅ | `/calls` for the capture service: the room, a session id kept across restarts, a listen-only LiveKit token, and every LiveKit identity mapped to its person |
| ✅ | the capture service's LiveKit driver (nextcloud-talk-capture, `[livekit]` extra) |
| ✅ | publishing the summary and the transcript into the room |
| ✅ | the announcement at the start of a call, and `!без-записи` / `!запись` for room admins |
| ✅ | both call formats: the `org.matrix.msc3401.call.member` state events Element Call sends today, and the MatrixRTC `m.rtc.member` sticky events (MSC4143, MSC4354) it is moving to |

Calls in rooms without the bot — direct calls included — are never looked at.
An encrypted room is reported in the log: the bot cannot hear an encrypted call
(and in one, MatrixRTC memberships are encrypted too, so it cannot see it either).

MatrixRTC memberships are not room state. The bot reads them as they arrive and,
when it starts or joins a room, from the last hour of the room's timeline — no
membership lasts longer than that, so nothing older can still be in a call.

## During a call

The bot is not visible in the call, so it says so in the room: **«Идёт
расшифровка звонка. Администратор комнаты может остановить её командой
!без-записи.»** Nothing is recorded before that message is posted — if the bot
may not write in the room, the call is not recorded, and the log says why.

| Command | |
|---|---|
| `!без-записи` (`!no-record`, `!no-transcribe`) | stop recording for the rest of the call |
| `!запись` (`!record`, `!transcribe`) | resume it |

- Only a room administrator — power level 100, or a room creator in room
  version 12 — can use them; anyone else is told so and nothing changes.
- The `/` spellings work too, as in Talk, but Element takes `/…` for its own
  commands, so `!` is what to type.
- Typed when there is no call, `!без-записи` holds the next call in the room;
  that call starts with a message saying it is not transcribed.
- The change is stored before the bot confirms it, and the capture service
  stops sending audio within one poll (`POLL_INTERVAL`, see below).

## Results in the room

Once a meeting is finished on the gateway — the analysis is done, usually an
hour or two after the call — the bot posts **«Встреча расшифрована: <topic>
(<date>)»** with the summary as the message, and two files in a thread under
it: the summary and the transcript, named as the Nextcloud app names them
(`2026-10-27 Дейли — итоги.md`, `… — расшифровка.md`). Only those two, as in
Nextcloud; the rest of the analysis stays on the gateway.

- Without analysis on the plan, the plain transcript is published once the
  meeting has stood at "transcribed" for 15 minutes.
- If the analysis fails, the transcript is published anyway — a room has no
  folder to find it in otherwise — and the meeting is still watched: a re-run's
  files join the same thread.
- Nothing is posted twice: every file is recorded once it is in the room, and a
  restart, a failed upload or a lost acknowledgement resumes from there.
- Meetings this service lost track of are taken back from the gateway
  (`/v1/meetings/undelivered?source=element-call`). The gateway key may be
  shared with the Nextcloud app; each integration takes only its own platform's
  meetings.
- A room that removed the bot is not retried; the files stay on the gateway.

## Running it

### Application service registration

```yaml
id: voxonta
url: http://voxonta-matrix-app:8080   # where the homeserver reaches this service
as_token: <random>
hs_token: <random>
sender_localpart: voxonta-bot
rate_limited: false
namespaces:
  users:
    - exclusive: true
      regex: "@voxonta-bot:your.server"
  rooms: []
  aliases: []
```

Add it to the homeserver's `app_service_config_files` and restart the
homeserver.

### The service

```bash
docker run -d --name voxonta-matrix-app \
  -e MATRIX_HOMESERVER_URL=http://synapse:8008 \
  -e MATRIX_SERVER_NAME=your.server \
  -e MATRIX_AS_TOKEN=... -e MATRIX_HS_TOKEN=... \
  -e LIVEKIT_URL=wss://livekit.your.server \
  -e LIVEKIT_API_KEY=... -e LIVEKIT_API_SECRET=... \
  -e DATABASE_URL=postgresql://voxonta:...@postgres:5432/voxonta \
  -e SERVICE_TOKEN=... \
  -e GATEWAY_URL=https://<gateway address> -e GATEWAY_TOKEN=... \
  ghcr.io/voxonta/voxonta-matrix-app:<version>
```

It creates its tables on start; the database role needs to own its database and
nothing more.

| Variable | Required | What it is |
|---|---|---|
| `MATRIX_HOMESERVER_URL` | yes | the homeserver's client API, as reachable from here |
| `MATRIX_SERVER_NAME` | yes | the server name in user ids |
| `MATRIX_AS_TOKEN`, `MATRIX_HS_TOKEN` | yes | from the registration |
| `LIVEKIT_URL` | yes | the LiveKit address the capture service dials |
| `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | yes | a LiveKit key for this service alone; the capture service only ever gets tokens signed with it |
| `DATABASE_URL` | yes | Postgres |
| `SERVICE_TOKEN` | yes | shared with the capture service, where it is `APP_SERVICE_TOKEN` |
| `GATEWAY_URL` | yes | the Voxonta gateway's HTTP API, where finished meetings are collected |
| `GATEWAY_TOKEN` | yes | the installation's gateway key — the capture service's `GATEWAY_TOKEN` |
| `MATRIX_BOT_LOCALPART` | no | `voxonta-bot` |
| `MATRIX_BOT_DISPLAYNAME` | no | `Транскрибация` |
| `APPSERVICE_PORT` | no | `8080` — for the homeserver only; do not publish it |
| `SERVICE_PORT` | no | `8081` — for the capture service |
| `CALL_END_GRACE_S` | no | `90` |
| `COLLECT_INTERVAL_S` | no | `60` — how often to ask the gateway about ended meetings |
| `LOG_LEVEL` | no | `INFO` |

### Two ports, on purpose

`8080` takes transactions from the homeserver and should stay inside the
homeserver's network. `8081` is the capture service's API, at the same paths the
Nextcloud app uses (`/apps/voxonta/api/v1/service/{config,calls,heartbeat}`), so
one capture build serves both. Publish it behind TLS and allow only the capture
service's address; point the capture service's `APP_BASE_URL` at it.

Set the capture service's `POLL_INTERVAL` to `1`. Nothing is recorded until the
room has been told, and the bot tells it as soon as the homeserver reports the
first device in the call — but the capture service only learns that on its
next poll, so its default of five seconds loses up to five seconds of every
call. A poll is answered from memory and costs next to nothing.

`GET /healthz` on the same port is for monitoring. `capture_seen_s_ago` is the
time since the capture service last polled or checked in; more than a few polls'
worth means it has stopped asking.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest tests -q
```

With `TEST_DATABASE_URL` set, the store tests also run against Postgres. With
the capture package importable (`PYTHONPATH=../nextcloud-talk-capture/src`), one
more test polls this service with the capture service's own client.

## Licence

MIT.
