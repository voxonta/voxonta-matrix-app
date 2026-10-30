"""What the integration needs to know, all of it from the environment.

Nothing here names a server: the homeserver, the LiveKit deployment and the
database are the installation's, and this repository is public.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit
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
    "GATEWAY_URL",
    "GATEWAY_TOKEN",
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

    # ── The Voxonta gateway ──
    # Its HTTP API, where finished meetings are collected from, and the
    # installation's key — the same one the capture service streams with.
    gateway_url: str = ""
    gateway_token: str = ""
    # How often to ask it about meetings that have ended, seconds.
    collect_interval_s: float = 60.0

    # ── Listening ──
    appservice_port: int = 8080  # transactions from the homeserver
    service_port: int = 8081  # the capture service's API

    # How long a call may have nobody in it before it is over. A reconnect or a
    # switch from laptop to phone leaves the room empty for seconds; ending the
    # call there would cut one meeting in two.
    call_end_grace_s: float = 90.0

    # Standalone widget is opt-in. Bot mode retains its existing defaults.
    bot_mode_enabled: bool = True
    widget_enabled: bool = False
    widget_public_url: str = ""
    widget_session_secret: str = ""
    widget_parent_origins: tuple[str, ...] = ()
    widget_adapter_url: str = ""
    widget_adapter_token: str = ""
    widget_port: int = 8082

    log_level: str = "INFO"

    @property
    def bot_user_id(self) -> str:
        return f"@{self.bot_localpart}:{self.server_name}"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        bot_mode = env.get("BOT_MODE_ENABLED", "true").lower() == "true"
        required = [
            k for k in _REQUIRED if bot_mode or k not in {"MATRIX_AS_TOKEN", "MATRIX_HS_TOKEN"}
        ]
        missing = [name for name in required if not env.get(name)]
        if missing:
            raise SystemExit("required but not set: " + ", ".join(missing))
        enabled = env.get("WIDGET_ENABLED", "false").lower() == "true"
        if enabled:
            required = (
                "WIDGET_PUBLIC_URL",
                "WIDGET_SESSION_SECRET",
                "WIDGET_PARENT_ORIGINS",
                "WIDGET_ADAPTER_URL",
                "WIDGET_ADAPTER_TOKEN",
            )
            if any(not env.get(k) for k in required):
                raise SystemExit("widget mode requires " + ", ".join(required))
            if len(env["WIDGET_SESSION_SECRET"]) < 32 or len(env["WIDGET_ADAPTER_TOKEN"]) < 32:
                raise SystemExit("widget secrets must have at least 32 characters")
            public = urlsplit(env["WIDGET_PUBLIC_URL"])
            if public.scheme != "https" or not public.netloc or public.query or public.fragment:
                raise SystemExit("WIDGET_PUBLIC_URL must be an HTTPS URL without query or fragment")
            if public.path.rstrip("/") != "/widget":
                raise SystemExit("WIDGET_PUBLIC_URL must use /widget")
            for origin in env["WIDGET_PARENT_ORIGINS"].split():
                parsed = urlsplit(origin)
                if (
                    parsed.scheme != "https"
                    or not parsed.netloc
                    or parsed.path
                    or parsed.query
                    or parsed.fragment
                ):
                    raise SystemExit("WIDGET_PARENT_ORIGINS must contain HTTPS origins")
        if not bot_mode and not enabled:
            raise SystemExit("enable bot mode or widget mode")
        return cls(
            bot_mode_enabled=bot_mode,
            widget_enabled=enabled,
            widget_public_url=env.get("WIDGET_PUBLIC_URL", "").rstrip("/"),
            widget_session_secret=env.get("WIDGET_SESSION_SECRET", ""),
            widget_parent_origins=tuple(env.get("WIDGET_PARENT_ORIGINS", "").split()),
            widget_adapter_url=env.get("WIDGET_ADAPTER_URL", ""),
            widget_adapter_token=env.get("WIDGET_ADAPTER_TOKEN", ""),
            widget_port=int(env.get("WIDGET_PORT") or 8082),
            homeserver_url=env["MATRIX_HOMESERVER_URL"].rstrip("/"),
            server_name=env["MATRIX_SERVER_NAME"],
            as_token=env.get("MATRIX_AS_TOKEN", ""),
            hs_token=env.get("MATRIX_HS_TOKEN", ""),
            bot_localpart=env.get("MATRIX_BOT_LOCALPART") or "voxonta-bot",
            bot_displayname=env.get("MATRIX_BOT_DISPLAYNAME") or "Транскрибация",
            livekit_url=env["LIVEKIT_URL"].rstrip("/"),
            livekit_api_key=env["LIVEKIT_API_KEY"],
            livekit_api_secret=env["LIVEKIT_API_SECRET"],
            database_url=env["DATABASE_URL"],
            service_token=env["SERVICE_TOKEN"],
            gateway_url=env["GATEWAY_URL"].rstrip("/"),
            gateway_token=env["GATEWAY_TOKEN"],
            collect_interval_s=float(env.get("COLLECT_INTERVAL_S") or 60),
            appservice_port=int(env.get("APPSERVICE_PORT") or 8080),
            service_port=int(env.get("SERVICE_PORT") or 8081),
            call_end_grace_s=float(env.get("CALL_END_GRACE_S") or 90),
            log_level=(env.get("LOG_LEVEL") or "INFO").upper(),
        )
