"""What a finished meeting looks like in its room, from the files the analyser writes.

The files below are shaped like meeting-analyzer's: a YAML block, then the
"# Executive Summary: <topic> (<date>)" heading it injects. File names follow
the Nextcloud app's rule, so a person who uses both sees the same names.
"""
from __future__ import annotations

from voxonta_matrix.gateway import Artifact
from voxonta_matrix.publication import (
    CUT_NOTE,
    SUMMARY_LIMIT,
    announcement,
    choose,
    file_message,
    file_name,
    front_matter,
)

SUMMARY = """---
meeting_date: '2026-10-27'
meeting_file_stem: 2026-10-27_deyli
---
# Executive Summary: Дейли (Вт, 27 октября 2026)

**45 мин · 2 чел.**

| Участник | ≈ мин |
|---|---|
| Алиса | 25 |
| Боб | 20 |

## Главное
- Выпуск — после этапа 6 <script>alert(1)</script>
""".encode()

ENRICHED = """---
meeting_name: "Планёрка: \\"релиз\\""
meeting_date: '2026-10-27'
---
# Enriched Transcript: Планёрка

**Алиса** [00:01]: Начнём.
""".encode()


def a(name, kind="analysis", sha="0" * 64):
    return Artifact(name, kind, sha)


# ── which files ───────────────────────────────────────────────────────

def test_the_enriched_transcript_is_preferred_over_the_plain_one():
    plain = a("2026-10-27 - Дейли.md", "transcript", "1" * 64)
    enriched = a("analysis/09_Enriched_Transcript.md", sha="2" * 64)
    summary = a("analysis/01_Executive_Summary.md", "summary", "3" * 64)
    rest = [a("analysis/02_Next_Steps.md"), a("stats.json", "stats")]
    assert choose([plain, *rest, enriched, summary]) == (summary, enriched)


def test_without_analysis_the_plain_transcript_is_published():
    plain = a("2026-10-27 - Дейли.md", "transcript")
    assert choose([plain]) == (None, plain)
    assert choose([]) == (None, None)


# ── what the files are called ─────────────────────────────────────────

def test_files_are_named_as_in_nextcloud():
    assert file_name(a("x/01_Executive_Summary.md", "summary"), SUMMARY) == \
        "2026-10-27 Дейли — итоги.md"
    # A colon cannot be in a file name; quotes go too.
    assert file_name(a("x/09_Enriched_Transcript.md"), ENRICHED) == \
        "2026-10-27 Планёрка релиз — расшифровка.md"


def test_a_file_that_names_no_topic_keeps_the_gateways_name():
    assert file_name(a("x/01_Executive_Summary.md"), b"just text") == "01_Executive_Summary.md"
    assert file_name(a("2026-10-27 - Дейли.md", "transcript"), b"# whatever") == \
        "2026-10-27 - Дейли.md"


def test_front_matter_is_read_like_the_nextcloud_app_reads_it():
    # PyYAML folds a long value onto indented lines at eighty columns.
    meta, body = front_matter("---\ntitle: Очень длинная\n  тема\nk: 'it''s'\n"
                              "u: \"\\u0442\\u0435\"\nlist:\n  - one\n---\n\nТекст")
    assert meta == {"title": "Очень длинная тема", "k": "it's", "u": "те", "list": ""}
    assert body == "Текст"
    assert front_matter("no block") == ({}, "no block")


# ── the announcement ──────────────────────────────────────────────────

def test_the_announcement_names_the_meeting_and_carries_the_summary():
    msg = announcement(SUMMARY)
    assert msg["msgtype"] == "m.text"
    assert msg["body"].startswith("Встреча расшифрована: Дейли (Вт, 27 октября 2026)\n\n")
    assert "meeting_file_stem" not in msg["body"]          # the YAML block is not text
    assert "Executive Summary" not in msg["body"]           # folded into the header
    assert "**45 мин · 2 чел.**" in msg["body"]
    html = msg["formatted_body"]
    assert html.startswith("<p><strong>Встреча расшифрована: Дейли (Вт, 27 октября 2026)"
                           "</strong></p>")
    assert "<table>" in html and "<h2>Главное</h2>" in html
    # Whatever looks like markup in what people said stays text.
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_without_a_summary_the_announcement_is_the_header_alone():
    assert announcement(None)["body"] == "Встреча расшифрована"


def test_a_summary_without_the_heading_is_shown_whole():
    assert announcement(b"## Itog\n\ntext")["body"] == "Встреча расшифрована\n\n## Itog\n\ntext"


def test_an_oversized_summary_is_cut_at_a_paragraph_and_says_so():
    para = ("слово " * 100).strip()
    msg = announcement(("\n\n".join([para] * 200)).encode())
    text = msg["body"].split("\n\n", 1)[1]
    assert text.endswith(CUT_NOTE)
    assert len(text.encode()) <= SUMMARY_LIMIT + len(CUT_NOTE.encode()) + 2
    assert text[: -len(CUT_NOTE)].rstrip().endswith("слово")   # whole paragraphs only
    # Both copies of the text together stay well under Matrix's 64 KiB event cap.
    assert len(msg["body"].encode()) + len(msg["formatted_body"].encode()) < 60_000


def test_a_file_hangs_in_the_thread_and_falls_back_to_a_reply():
    msg = file_message("2026-10-27 Дейли — итоги.md", "mxc://s/f", 120, "$root")
    assert msg["msgtype"] == "m.file" and msg["body"] == msg["filename"]
    assert msg["info"] == {"mimetype": "text/markdown", "size": 120}
    assert msg["m.relates_to"] == {"rel_type": "m.thread", "event_id": "$root",
                                   "is_falling_back": True,
                                   "m.in_reply_to": {"event_id": "$root"}}
