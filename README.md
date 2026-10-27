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
serves the calls and a LiveKit
token for each
```

Media never passes through this service. It runs next to the homeserver; the
capture service runs wherever there is CPU to spare and reaches it over HTTPS.

## What works today

| | |
|---|---|
| ✅ | the bot accepts invites from users of its own server, declines others, forgets rooms it is removed from |
| ✅ | a call starts with the first device in the room's call and ends after `CALL_END_GRACE_S` of nobody — a reconnect or a switch of device does not split the meeting |
| ✅ | `/calls` for the capture service: the room, a session id kept across restarts, a listen-only LiveKit token, and every LiveKit identity mapped to its person |
| ⏳ | the capture service's LiveKit driver |
| ⏳ | publishing the summary and the transcript into the room |
| ⏳ | the announcement at the start of a call, and `!no-record` / `!record` for room admins |
| ⏳ | `m.rtc.member` (MSC4143) memberships — reported in the log until then |

Calls in rooms without the bot — direct calls included — are never looked at.
An encrypted room is reported in the log: the bot cannot hear an encrypted call.

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
| `MATRIX_BOT_LOCALPART` | no | `voxonta-bot` |
| `MATRIX_BOT_DISPLAYNAME` | no | `Транскрибация` |
| `APPSERVICE_PORT` | no | `8080` — for the homeserver only; do not publish it |
| `SERVICE_PORT` | no | `8081` — for the capture service |
| `CALL_END_GRACE_S` | no | `90` |
| `LOG_LEVEL` | no | `INFO` |

### Two ports, on purpose

`8080` takes transactions from the homeserver and should stay inside the
homeserver's network. `8081` is the capture service's API, at the same paths the
Nextcloud app uses (`/apps/voxonta/api/v1/service/{config,calls,heartbeat}`), so
one capture build serves both. Publish it behind TLS and allow only the capture
service's address; point the capture service's `APP_BASE_URL` at it. `GET
/healthz` on the same port is for monitoring.

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
