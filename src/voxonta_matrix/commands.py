"""What the bot says in a room, and the two commands it listens for.

The texts are the Nextcloud app's translations with `!` for `/`: Element runs
anything that starts with `/` as one of its own commands, so `!без-записи` is
what people type. The `/` spellings are accepted too, for whoever sends them as
a message anyway, and so are the English ones, as in Talk.
"""
from __future__ import annotations

STOP = "stop"
START = "start"

_WORDS = {"без-записи": STOP, "no-record": STOP, "no-transcribe": STOP,
          "запись": START, "record": START, "transcribe": START}

# ── at the start of a call ──
ANNOUNCE = ("Идёт расшифровка звонка. Администратор комнаты может остановить её "
            "командой !без-записи.")
# The room said !без-записи before the call began.
ANNOUNCE_OPTED_OUT = ("Звонок не расшифровывается: запись остановлена командой !без-записи. "
                      "Администратор комнаты может включить её командой !запись.")

# ── answers to the commands ──
STOPPED = ("Запись остановлена. Всё сказанное дальше в этом звонке расшифровываться "
           "не будет. Наберите !запись, чтобы возобновить.")
RESUMED = "Запись этого звонка возобновлена."
# Outside a call the command holds the next one, as in Talk; saying "this call"
# there would be wrong.
STOPPED_NEXT = ("Звонка сейчас нет. Следующий звонок в этой комнате расшифровываться "
                "не будет. Наберите !запись, чтобы отменить.")
RESUMED_NEXT = "Звонка сейчас нет. Следующий звонок будет расшифровываться."
ONLY_ADMIN_STOPS = "Остановить запись может только администратор комнаты."
ONLY_ADMIN_RESUMES = "Возобновить запись может только администратор комнаты."
COULD_NOT_STOP = ("Не удалось остановить запись. Возможно, она продолжается — обратитесь "
                  "к администратору.")
COULD_NOT_RESUME = "Не удалось возобновить запись. Попробуйте ещё раз."


def command_in(event: dict) -> str | None:
    """STOP, START or None for a timeline event.

    Matched exactly, not by prefix: "!записать протокол" is not "!запись".
    Edits are ignored — the original was read when it arrived — and so is
    anything but plain text: notices are what bots post.
    """
    if event.get("type") != "m.room.message":
        return None
    content = event.get("content") or {}
    if content.get("msgtype") != "m.text":
        return None
    if (content.get("m.relates_to") or {}).get("rel_type") == "m.replace":
        return None
    text = _without_reply_fallback(str(content.get("body") or "")).strip().lower()
    if len(text) < 2 or text[0] not in "!/":
        return None
    return _WORDS.get(text[1:])


def _without_reply_fallback(body: str) -> str:
    """Older clients put the message replied to in front, as "> " lines."""
    lines = body.split("\n")
    if not lines or not lines[0].startswith("> "):
        return body
    while lines and lines[0].startswith(">"):
        lines.pop(0)
    return "\n".join(lines)


def notice(text: str, reply_to: str | None = None) -> dict:
    """A message from the bot. A notice, as Matrix asks of bots, so that no
    other bot answers it."""
    content: dict = {"msgtype": "m.notice", "body": text}
    if reply_to:
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
    return content
