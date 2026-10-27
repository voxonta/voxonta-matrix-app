"""What the integration needs to know, all of it from the environment.

Nothing here names a server: the homeserver, the LiveKit deployment and the
database are the installation's, and this repository is public.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# Variables without a sensible default. Checked together so a first start
# names everything that is missing, not one variable per restart.
_REQUIRED = (
    "MATRIX_HOMESERVER_URL",
    "MATRIX_SERVER_NAME",
    "MATRIX_AS_TOKEN",
    "MATRIX_HS_TOKEN",
    "LIVEKIT_URL",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
    "DATABASE_URL",
    "SERVICE_TOKEN",
)


@dataclass
class Config:
    # ── Matrix ──
    # Where the homeserver's client API is, as seen from here (inside the
    # stack's network, usually), and the server name in its user ids.
    homeserver_url: str
    server_name: str
    # From the application service registration. as_token is ours to present
    # to the homeserver; hs_token is the homeserver's to present to us.
    as_token: str
    hs_token: str
    bot_localpart: str = "voxonta-bot"
    bot_displayname: str = "Транскрибация"

    # ── LiveKit ──
    # The address the capture service dials, and a key of this integration's
    # own: tokens for the capture service are signed here, so it never holds
    # the LiveKit secret itself.
    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""

    # ── State ──
    database_url: str = ""

    # ── The capture service ──
    # Shared secret for /apps/voxonta/api/v1/service/*; the same value is the
    # capture service's APP_SERVICE_TOKEN.
    service_token: str = ""

    # ── Listening ──
    appservice_port: int = 8080   # transactions from the homeserver
    service_port: int = 8081      # the capture service's API

    # How long a call may have nobody in it before it is over. A reconnect or a
    # switch from laptop to phone leaves the room empty for seconds; ending the
    # call there would cut one meeting in two.
    call_end_grace_s: float = 90.0

    log_level: str = "INFO"

    @property
    def bot_user_id(self) -> str:
        return f"@{self.bot_localpart}:{self.server_name}"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        missing = [name for name in _REQUIRED if not env.get(name)]
        if missing:
            raise SystemExit("required but not set: " + ", ".join(missing))
        return cls(
            homeserver_url=env["MATRIX_HOMESERVER_URL"].rstrip("/"),
            server_name=env["MATRIX_SERVER_NAME"],
            as_token=env["MATRIX_AS_TOKEN"],
            hs_token=env["MATRIX_HS_TOKEN"],
            bot_localpart=env.get("MATRIX_BOT_LOCALPART") or "voxonta-bot",
            bot_displayname=env.get("MATRIX_BOT_DISPLAYNAME") or "Транскрибация",
            livekit_url=env["LIVEKIT_URL"].rstrip("/"),
            livekit_api_key=env["LIVEKIT_API_KEY"],
            livekit_api_secret=env["LIVEKIT_API_SECRET"],
            database_url=env["DATABASE_URL"],
            service_token=env["SERVICE_TOKEN"],
            appservice_port=int(env.get("APPSERVICE_PORT") or 8080),
            service_port=int(env.get("SERVICE_PORT") or 8081),
            call_end_grace_s=float(env.get("CALL_END_GRACE_S") or 90),
            log_level=(env.get("LOG_LEVEL") or "INFO").upper(),
        )
