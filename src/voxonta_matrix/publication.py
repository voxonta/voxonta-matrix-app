"""What a finished meeting looks like in its room.

As in Nextcloud, people get two files: the summary and the transcript. Nextcloud
shares them and links them in the chat; a Matrix room has no folder, so here
the summary is the message itself — "Встреча расшифрована" and its text — and
both files go into a thread under it.

The rest of the analysis set and the statistics are not published. They stay
on the gateway until retention takes them, as they would in Nextcloud for
anyone who did not open the folder.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from markdown_it import MarkdownIt

from voxonta_matrix.gateway import Artifact

SUMMARY_FILE = "01_Executive_Summary.md"
ENRICHED_TRANSCRIPT_FILE = "09_Enriched_Transcript.md"

HEADER = "Встреча расшифрована"
# Bytes of summary text in the message. An event is capped at 64 KiB and
# carries the text twice (plain and HTML); a summary is a few kilobytes, so
# this only ever cuts something that went wrong upstream.
SUMMARY_LIMIT = 16_000
CUT_NOTE = "_…полностью — в файле в треде._"

# No raw HTML: the summary is written by a model from what people said, and
# anything in it that looks like markup is text.
_markdown = MarkdownIt("commonmark", {"html": False}).enable("table")
_SUMMARY_HEADING = re.compile(r"^#\s*Executive Summary:\s*(.+?)\s*$", re.MULTILINE)


def basename(artifact: Artifact) -> str:
    return PurePosixPath(artifact.name).name


def choose(artifacts) -> tuple[Artifact | None, Artifact | None]:
    """(summary, transcript) out of everything the meeting produced.

    The transcript is the enriched one when the plan includes analysis — it
    is the one the Nextcloud app shares — and the plain one otherwise.
    """
    summary = next((a for a in artifacts if basename(a) == SUMMARY_FILE), None) \
        or next((a for a in artifacts if a.kind == "summary"), None)
    transcript = next((a for a in artifacts if basename(a) == ENRICHED_TRANSCRIPT_FILE), None) \
        or next((a for a in artifacts if a.kind == "transcript"), None)
    return summary, transcript


# ── reading the analyser's files ──────────────────────────────────────

def front_matter(text: str) -> tuple[dict[str, str], str]:
    """The scalar fields of a leading YAML block, and the text after it.

    The same reading as ArtifactWriter::frontMatter in the Nextcloud app:
    `key: value`, quoted or not, a long value folded onto indented lines. Not a
    YAML parser, and it does not need to be one.
    """
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    fields: dict[str, str] = {}
    key = None
    for line in text[4:end].split("\n"):
        if m := re.match(r"^([A-Za-z_]\w*):\s*(.*)$", line):
            key = m.group(1)
            fields[key] = m.group(2)
        elif key is not None and (m := re.match(r"^\s+(\S.*)$", line)) \
                and not m.group(1).startswith("- "):
            fields[key] += (" " if fields[key] else "") + m.group(1)
        else:
            key = None
    rest = text[end + 4:]
    rest = rest.split("\n", 1)[1] if "\n" in rest else ""
    return {k: _unquote(v) for k, v in fields.items()}, rest.lstrip("\n")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return re.sub(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", _unescape, value[1:-1])
    return value


def _unescape(m: re.Match) -> str:
    seq = m.group(1)
    if seq[0] in "ux" and len(seq) > 1:
        return chr(int(seq[1:], 16))
    return {"n": "\n", "t": "\t"}.get(seq, seq)


def _file_safe(title: str) -> str:
    title = re.sub(r"[\\/:]+", " ", title)
    title = re.sub(r'[*?"<>|\x00-\x1f]+', "", title)
    title = re.sub(r"\s+", " ", title).strip(" .")
    return title[:120].rstrip() + "…" if len(title) > 120 else title


def file_name(artifact: Artifact, content: bytes) -> str:
    """The name a person saves the file under.

    The Nextcloud app's rule (ArtifactWriter::recipientName): the date and the
    topic, then what the file is — "2026-10-27 Дейли — итоги.md". A file that
    states no topic, and the plain transcript, keep the gateway's name, which
    is already "<date> - <room>.md".
    """
    name = basename(artifact)
    is_summary = name.startswith("01_Executive_Summary")
    is_transcript = name.startswith("09_Enriched_Transcript")
    if not is_summary and not is_transcript:
        return name
    text = content.decode("utf-8", errors="replace")
    meta, body = front_matter(text)
    topic = meta.get("title", "")
    if not topic and is_transcript:
        topic = meta.get("meeting_name", "")
    if not topic and is_summary and (h := _SUMMARY_HEADING.search(body)):
        topic = re.sub(r"\s*\([^()]*\)\s*$", "", h.group(1))
    title = _file_safe(topic)
    if not title:
        return name
    date = meta.get("meeting_date", "")[:10]
    date = date + " " if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else ""
    return f"{date}{title} — {'итоги' if is_summary else 'расшифровка'}.md"


# ── the messages ──────────────────────────────────────────────────────

def announcement(summary: bytes | None) -> dict:
    """The message the files hang under: the header, then the summary.

    The summary's own heading — "# Executive Summary: <topic> (<date>)" — is
    folded into the header, so the room reads "Встреча расшифрована: Дейли
    (Пн, 27 октября 2026)" and knows which call it was. A summary without that
    heading is shown whole under the plain header.
    """
    header, body = HEADER, ""
    if summary is not None:
        _, body = front_matter(summary.decode("utf-8", errors="replace"))
        first, _, rest = body.partition("\n")
        if m := _SUMMARY_HEADING.match(first):
            header, body = f"{HEADER}: {m.group(1)}", rest.lstrip("\n")
        body = _cut(body.strip())
    plain = header if not body else f"{header}\n\n{body}"
    html = f"<p><strong>{_escape(header)}</strong></p>"
    if body:
        html += "\n" + _markdown.render(body)
    return {"msgtype": "m.text", "body": plain,
            "format": "org.matrix.custom.html", "formatted_body": html}


def _cut(text: str) -> str:
    if len(text.encode()) <= SUMMARY_LIMIT:
        return text
    head = text.encode()[:SUMMARY_LIMIT].decode("utf-8", errors="ignore")
    paragraph = head.rfind("\n\n")
    return (head[:paragraph] if paragraph > 0 else head).rstrip() + "\n\n" + CUT_NOTE


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def file_message(name: str, url: str, size: int, thread_root: str) -> dict:
    """A file in the thread under the announcement."""
    return {
        "msgtype": "m.file",
        "body": name,
        "filename": name,
        "url": url,
        "info": {"mimetype": "text/markdown", "size": size},
        "m.relates_to": {
            "rel_type": "m.thread",
            "event_id": thread_root,
            # Clients without threads show it as a reply to the announcement.
            "is_falling_back": True,
            "m.in_reply_to": {"event_id": thread_root},
        },
    }
