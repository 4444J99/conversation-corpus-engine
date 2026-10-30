from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from archive_support import (
    COPILOT_BADGE,
    COPILOT_CARD_URLS,
    COPILOT_CHROME_STRINGS,
    COPILOT_ID,
    COPILOT_PAGE_NAME,
    COPILOT_SAVED,
    COPILOT_SAVED_ISO,
    COPILOT_TITLE,
    COPILOT_URL,
    build_copilot_folder,
    copilot_card,
    copilot_card_list,
    copilot_comment,
    copilot_item,
    copilot_page,
    copilot_pill,
    copilot_small_page,
    copilot_spans,
    foreign_singlefile_page,
    write_copilot_page,
)

from conversation_corpus_engine import archive_read_copilot
from conversation_corpus_engine.archive_commands import add_archive_parser, verify_archive
from conversation_corpus_engine import archive_search
from conversation_corpus_engine.archive_feed import project_feed
from conversation_corpus_engine.archive_ingest import (
    PROVIDER_SPECS,
    ingest,
    iter_store,
    load_index,
    load_manifest,
    resolve_sources,
)
from conversation_corpus_engine.archive_model import (
    Conversation,
    SourceTracker,
    finalize_conversation,
)
from conversation_corpus_engine.archive_naming import (
    conversation_name,
    validate_name,
    verify_names,
)
from conversation_corpus_engine.cli import main

S = copilot_spans
P = copilot_pill
URL0, URL1 = COPILOT_CARD_URLS
CHROME_ECHO = ("Deep Research", COPILOT_TITLE, "+2", "aria-", "button")

# The whole standard page, derived by hand from the rendering rules (spec section 3).
GOLDEN = "\n".join(
    [
        "# Lantern Care Playbook: Wicks, Oil and Glass",
        "",
        "## Overview",
        "",
        "A trimmed wick burns evenly. Keep the oil level high, and the glass clean.",
        "",
        "Trim **twice weekly** when *windy*, once when calm.",
        "",
        "### Trimming schedule",
        "",
        "- Cut the char flat.",
        "- Round the corners lightly.",
        "- Wipe the glass.",
        "",
        "3. Light the lantern.",
        "4. Check the flame height.",
        "",
        "> A steady flame is a quiet flame.",
        "",
        "## Comparison",
        "",
        "| Wick | Burn rate | Notes |",
        "| --- | --- | --- |",
        "| Flat cotton | Slow | Use a \\| b mix |",
        "| Round braid | **Fast** | Needs trimming |",
        "",
        f"1. [Wick Trimming for Oil Lanterns]({URL0}) — lamps.example.org",
        f"2. [Glass Care Handbook]({URL1}) — oil.example.com",
    ]
)
GOLDEN_CITATIONS = [
    {
        "index": 1,
        "site": "lamps.example.org",
        "text": "Wick Trimming for Oil Lanterns",
        "url": URL0,
    },
    {"index": 2, "site": "oil.example.com", "text": "Glass Care Handbook", "url": URL1},
]
PAGE_LINE = (
    "{n} saved Copilot page(s) (SingleFile .html) — report text, headings, tables, lists and "
    "source cards; the capture date is the only timestamp"
)
UNRECOGNIZED_LINE = "{k} .html file(s) — not recognized as saved Copilot pages (not read)"
UNREADABLE_LINE = "{k} .html file(s) — could not be read (permissions or I/O error)"
FOLDER_LINE = "{k} folder(s) — could not be listed (permissions or I/O error)"
OTHER_LINE = "{k} {suffix} file(s) — not saved Copilot pages (names and bytes not read)"
MARKER = "Page saved with SingleFile"
# Elements in a row for the linear-time tests, per case. Each size is chosen so that the quadratic
# form of the pill pattern fails the bound within seconds instead of hanging a run for minutes:
# 30,000 pills took about 11 s there and 200,000 blank spans about 8.5 s, against well under a
# second for the linear form on a slow runner.
PILL_RUN = 30_000
BLANK_RUN = 200_000
SCALE_SECONDS = 5.0


def _read(root: Path) -> tuple[list[Conversation], SourceTracker]:
    tracker = SourceTracker(root, archive_read_copilot.READER)
    return list(archive_read_copilot.iter_conversations(root, tracker)), tracker


def _probe(
    tmp_path: Path,
    body: str,
    *,
    role: str = "ai",
    title: str | None = "Probe Title",
    **page_kwargs: Any,
) -> tuple[Conversation, SourceTracker]:
    """One page holding one item with `body`; returns its single conversation."""
    root = tmp_path / "probe"
    write_copilot_page(root / "page.html", [copilot_item(role, body, title=title)], **page_kwargs)
    conversations, tracker = _read(root)
    assert len(conversations) == 1
    return conversations[0], tracker


def _page_only(tmp_path: Path, page: str) -> tuple[list[Conversation], SourceTracker]:
    root = tmp_path / "raw"
    root.mkdir()
    (root / "page.html").write_text(page, encoding="utf-8")
    return _read(root)


def _header_block(title: str, badge: str = COPILOT_BADGE) -> str:
    """The session header as `copilot_item` writes it (a badge div beside a `text-3xl` title),
    for tests that must put something around it: `copilot_item` cannot wrap header and body."""
    return (
        '<div class="flex flex-col gap-y-6"><div class="flex items-center gap-2">'
        f'<div class="bg-current forced-color-adjust-none">{badge}</div></div>'
        f"<div class=text-3xl>{title}</div></div>"
    )


def _page_with_marker_at(offset: int) -> str:
    """A Copilot page whose SingleFile marker starts exactly `offset` bytes in (ASCII only): a
    leading comment pads the page; its `<!--` and `-->\\n` take 8 of the bytes."""
    page = copilot_page([copilot_item("ai", "<p>" + S("Edge text."), title="Edge Session")])
    pad = offset - page.index(MARKER) - len("<!---->\n")
    return "<!--" + "x" * pad + "-->\n" + page


def _cce(monkeypatch, capsys, *argv: str) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["cce", *argv])
    code = 0
    try:
        main()
    except SystemExit as exit_:
        code = int(exit_.code or 0)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _ingest_copilot(env, folder: Path) -> dict[str, Any]:
    return ingest(
        export_root=env.export,
        paths=env.paths,
        authorization=env.auth,
        providers=["copilot"],
        overrides={"copilot": folder},
    )


# --- the module's two constants and detect -------------------------------------------------


def test_constants_name_the_provider_and_the_reader() -> None:
    assert archive_read_copilot.PROVIDER == "copilot"
    assert archive_read_copilot.READER == "archive_read_copilot@1"


def test_detect_accepts_a_folder_holding_a_copilot_page(tmp_path: Path) -> None:
    assert archive_read_copilot.detect(build_copilot_folder(tmp_path / "caps")) is True


def test_detect_finds_a_page_in_a_subfolder(tmp_path: Path) -> None:
    root = tmp_path / "caps"
    write_copilot_page(root / "deep" / "er" / "page.html", [copilot_item("ai", "<p>x")])
    assert archive_read_copilot.detect(root) is True


def test_detect_refuses_a_missing_root_a_file_and_an_empty_folder(tmp_path: Path) -> None:
    assert archive_read_copilot.detect(tmp_path / "missing") is False
    single = write_copilot_page(tmp_path / "single.html", [copilot_item("ai", "<p>x")])
    assert archive_read_copilot.detect(single) is False
    empty = tmp_path / "empty"
    empty.mkdir()
    assert archive_read_copilot.detect(empty) is False


def test_detect_refuses_folders_with_no_copilot_page(tmp_path: Path) -> None:
    only_other = tmp_path / "other"
    only_other.mkdir()
    (only_other / "a.docx").write_bytes(b"stub")
    (only_other / "b.txt").write_text("stub", encoding="utf-8")
    (only_other / "c.html").write_text(foreign_singlefile_page(), encoding="utf-8")
    assert archive_read_copilot.detect(only_other) is False


def test_detect_ignores_pages_under_dot_components(tmp_path: Path) -> None:
    root = tmp_path / "caps"
    write_copilot_page(root / ".hidden" / "page.html", [copilot_item("ai", "<p>x")])
    write_copilot_page(root / ".dot-page.html", [copilot_item("ai", "<p>x")])
    write_copilot_page(root / "visible" / ".nested" / "page.html", [copilot_item("ai", "<p>x")])
    assert archive_read_copilot.detect(root) is False
    conversations, tracker = _read(root)
    assert conversations == [] and not tracker.counters


@pytest.mark.parametrize(
    "url",
    [
        "https://copilot.microsoft.com.evil.example/research/Zq8Lw3Nx-Tp_4RmKe7Vh2",
        "https://notcopilot.microsoft.com/research/Zq8Lw3Nx-Tp_4RmKe7Vh2",
        "https://example.org/copilot.microsoft.com/research/Zq8Lw3Nx-Tp_4RmKe7Vh2",
        "https://microsoft.com/research/Zq8Lw3Nx-Tp_4RmKe7Vh2",
        "https://evil.example/copilot.microsoft.com",
        f"ftp://copilot.microsoft.com/research/{COPILOT_ID}",
        # the host is whatever follows the last `@`, so this one names evil.example
        f"https://copilot.microsoft.com@evil.example/research/{COPILOT_ID}",
        # an unparsable URL (urlsplit raises ValueError) is a foreign page, not a crash
        f"https://[copilot.microsoft.com/research/{COPILOT_ID}",
    ],
)
def test_only_the_exact_host_makes_a_copilot_page(tmp_path: Path, url: str) -> None:
    root = tmp_path / "caps"
    write_copilot_page(root / "page.html", [copilot_item("ai", "<p>x")], url=url)
    assert archive_read_copilot.detect(root) is False
    conversations, tracker = _read(root)
    assert conversations == []
    assert dict(tracker.counters) == {"pages_skipped_not_copilot": 1}
    assert tracker.hashes() == {} and tracker.files_read == []


@pytest.mark.parametrize(
    "url",
    [
        f"https://copilot.microsoft.com:443/research/{COPILOT_ID}",
        f"HTTPS://COPILOT.MICROSOFT.COM/research/{COPILOT_ID}",
        f"https://user@copilot.microsoft.com/research/{COPILOT_ID}",
    ],
    ids=["explicit-port", "upper-case-scheme-and-host", "userinfo"],
)
def test_the_host_is_the_parsed_hostname_not_the_url_text(tmp_path: Path, url: str) -> None:
    root = tmp_path / "caps"
    write_copilot_page(root / "page.html", [copilot_item("ai", "<p>" + S("Found."))], url=url)
    assert archive_read_copilot.detect(root) is True
    (conversation,), tracker = _read(root)
    assert (conversation.source_id, conversation.collection) == (COPILOT_ID, "research")
    assert conversation.messages[0].text == "Found."
    assert dict(tracker.counters) == {"pages_read": 1}


def test_a_page_without_the_singlefile_marker_is_not_a_copilot_page(tmp_path: Path) -> None:
    root = tmp_path / "caps"
    root.mkdir()
    (root / "page.html").write_text(
        f"<!--\n url: {COPILOT_URL}\n-->\n<html><body><p>text</body></html>\n", encoding="utf-8"
    )
    assert archive_read_copilot.detect(root) is False


def test_a_url_line_after_the_comment_closes_does_not_make_a_copilot_page(tmp_path: Path) -> None:
    root = tmp_path / "caps"
    root.mkdir()
    page = (
        copilot_comment(url=None)  # the marker and the date, but no `url:` line inside
        + f"\nurl: {COPILOT_URL}\n<html><body><p>text</body></html>\n"
    )
    (root / "page.html").write_text(page, encoding="utf-8")
    assert archive_read_copilot.detect(root) is False
    conversations, tracker = _read(root)
    assert conversations == []
    assert dict(tracker.counters) == {"pages_skipped_not_copilot": 1}


def test_the_marker_must_sit_in_the_first_8192_bytes(tmp_path: Path) -> None:
    near = tmp_path / "near"
    far = tmp_path / "far"
    near.mkdir()
    far.mkdir()
    body = copilot_page([copilot_item("ai", "<p>" + S("Near enough."))])
    (near / "page.html").write_text("<!--" + "pad " * 900 + "-->\n" + body, encoding="utf-8")
    (far / "page.html").write_text("<!--" + "pad " * 3000 + "-->\n" + body, encoding="utf-8")
    assert archive_read_copilot.detect(near) is True
    assert archive_read_copilot.detect(far) is False
    (conversation,) = _read(near)[0]
    assert conversation.messages[0].text == "Near enough."
    assert _read(far)[0] == []


@pytest.mark.parametrize(
    ("offset", "detected"),
    [(7604, True), (8204, False)],
    ids=["whole-header-inside-the-window", "marker-starts-past-the-window"],
)
def test_the_detection_window_is_exercised_on_both_sides_of_8192(
    tmp_path: Path, offset: int, detected: bool
) -> None:
    root = tmp_path / "caps"
    root.mkdir()
    (root / "page.html").write_text(_page_with_marker_at(offset), encoding="utf-8")
    raw = (root / "page.html").read_bytes()
    assert archive_read_copilot.HEAD_BYTES == 8192
    assert raw.index(MARKER.encode()) == offset
    assert (raw.index(b"-->", offset) + 3 <= archive_read_copilot.HEAD_BYTES) is detected
    assert archive_read_copilot.detect(root) is detected
    conversations, tracker = _read(root)
    if detected:
        (conversation,) = conversations
        assert conversation.messages[0].text == "Edge text."
        assert conversation.updated == COPILOT_SAVED_ISO
        assert dict(tracker.counters) == {"pages_read": 1}
    else:
        assert conversations == []
        assert dict(tracker.counters) == {"pages_skipped_not_copilot": 1}


# --- channels ----------------------------------------------------------------------------


def test_channels_count_pages_and_group_everything_else_by_extension(tmp_path: Path) -> None:
    root = build_copilot_folder(tmp_path / "caps")
    assert archive_read_copilot.channels(root) == {
        "read": [PAGE_LINE.format(n=1)],
        "not_read": [
            UNRECOGNIZED_LINE.format(k=1),
            OTHER_LINE.format(k=2, suffix=".docx"),
            OTHER_LINE.format(k=1, suffix=".txt"),
        ],
    }


def test_channels_lowercase_suffixes_skip_dot_components_and_name_suffixless_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "caps"
    write_copilot_page(root / "page.html", [copilot_item("ai", "<p>x")])
    (root / "a.docx").write_bytes(b"x")
    (root / "b.DOCX").write_bytes(b"x")
    (root / "README").write_text("x", encoding="utf-8")
    (root / "notes.txt").write_text("x", encoding="utf-8")
    (root / ".DS_Store").write_bytes(b"x")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "c.docx").write_bytes(b"x")
    (root / ".hidden-file.html").write_text(foreign_singlefile_page(), encoding="utf-8")
    (root / "x.htm").write_text(foreign_singlefile_page(), encoding="utf-8")
    (root / "y.html").write_text(foreign_singlefile_page(), encoding="utf-8")
    assert archive_read_copilot.channels(root) == {
        "read": [PAGE_LINE.format(n=1)],
        "not_read": [
            UNRECOGNIZED_LINE.format(k=2),
            OTHER_LINE.format(k=1, suffix="(none)"),
            OTHER_LINE.format(k=2, suffix=".docx"),
            OTHER_LINE.format(k=1, suffix=".txt"),
        ],
    }


def test_channels_always_emit_the_page_line_even_for_zero_pages(tmp_path: Path) -> None:
    assert archive_read_copilot.channels(tmp_path / "missing") == {
        "read": [PAGE_LINE.format(n=0)],
        "not_read": [],
    }
    empty = tmp_path / "empty"
    empty.mkdir()
    assert archive_read_copilot.channels(empty) == {"read": [PAGE_LINE.format(n=0)], "not_read": []}


def test_channels_count_every_copilot_page(tmp_path: Path) -> None:
    root = tmp_path / "caps"
    for index, identifier in enumerate(("Aa1Bb2Cc3Dd4Ee5Ff6Gg7", "Hh1Ii2Jj3Kk4Ll5Mm6Nn7")):
        page = copilot_small_page(identifier, f"Session {index}", "Some text.")
        (root / f"page-{index}.html").parent.mkdir(parents=True, exist_ok=True)
        (root / f"page-{index}.html").write_text(page, encoding="utf-8")
    assert archive_read_copilot.channels(root)["read"] == [PAGE_LINE.format(n=2)]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes, so nothing would be proven")
def test_non_html_files_are_never_opened(tmp_path: Path) -> None:
    root = build_copilot_folder(tmp_path / "caps")
    locked = [root / "attachments" / "report-draft.docx", root / "summary.docx", root / "notes.txt"]
    for path in locked:
        path.chmod(0)
    try:
        assert archive_read_copilot.detect(root) is True
        info = archive_read_copilot.channels(root)
        conversations, tracker = _read(root)
    finally:
        for path in locked:
            path.chmod(0o644)
    # the whole report: a file that could not be opened would show up as an unreadable .html
    # line or change a count, so nothing else may be in the list
    assert info == {
        "read": [PAGE_LINE.format(n=1)],
        "not_read": [
            UNRECOGNIZED_LINE.format(k=1),
            OTHER_LINE.format(k=2, suffix=".docx"),
            OTHER_LINE.format(k=1, suffix=".txt"),
        ],
    }
    assert len(conversations) == 1
    assert tracker.files_read == [COPILOT_PAGE_NAME]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes, so nothing would be proven")
def test_an_unreadable_page_is_counted_and_not_called_foreign(tmp_path: Path) -> None:
    root = tmp_path / "caps"
    write_copilot_page(root / "a-ok.html", [copilot_item("ai", "<p>" + S("Readable."))])
    locked = write_copilot_page(root / "b-locked.html", [copilot_item("ai", "<p>" + S("Sealed."))])
    locked.chmod(0)
    try:
        conversations, tracker = _read(root)
        info = archive_read_copilot.channels(root)
    finally:
        locked.chmod(0o644)
    assert [c.messages[0].text for c in conversations] == ["Readable."]
    assert dict(tracker.counters) == {"pages_read": 1, "pages_unreadable": 1}
    assert "pages_skipped_not_copilot" not in tracker.counters
    assert info == {"read": [PAGE_LINE.format(n=1)], "not_read": [UNREADABLE_LINE.format(k=1)]}


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores folder modes, so nothing would be proven"
)
def test_a_folder_that_cannot_be_listed_is_counted_and_the_rest_is_still_read(
    tmp_path: Path,
) -> None:
    root = tmp_path / "caps"
    write_copilot_page(root / "a-ok.html", [copilot_item("ai", "<p>" + S("Readable."))])
    write_copilot_page(root / "sealed" / "hidden.html", [copilot_item("ai", "<p>" + S("Hidden."))])
    (root / "sealed").chmod(0)
    try:
        conversations, tracker = _read(root)
        info = archive_read_copilot.channels(root)
    finally:
        (root / "sealed").chmod(0o755)
    assert [c.messages[0].text for c in conversations] == ["Readable."]
    assert dict(tracker.counters) == {"folders_unreadable": 1, "pages_read": 1}
    assert tracker.files_read == ["a-ok.html"]
    assert info == {"read": [PAGE_LINE.format(n=1)], "not_read": [FOLDER_LINE.format(k=1)]}


# --- field mapping (spec 2.3, 2.4, 2.5) -----------------------------------------------------


def test_the_standard_page_maps_every_conversation_field(tmp_path: Path) -> None:
    root = build_copilot_folder(tmp_path / "caps")
    (conversation,) = _read(root)[0]
    assert conversation.provider == "copilot"
    assert conversation.source_id == COPILOT_ID
    assert conversation.title == COPILOT_TITLE
    assert conversation.created == ""
    assert conversation.updated == COPILOT_SAVED_ISO
    assert conversation.kind == "search"
    assert conversation.collection == "research"
    assert conversation.model == ""
    assert conversation.archived is False
    assert conversation.extra == {
        "capture": "singlefile",
        "captured_at": COPILOT_SAVED_ISO,
        "mode": COPILOT_BADGE,
        "source_url": COPILOT_URL,
        "sources": 2,
        "tables": 1,
    }
    page = root / COPILOT_PAGE_NAME
    assert conversation.provenance == {
        "source_file": COPILOT_PAGE_NAME,
        "source_sha256": hashlib.sha256(page.read_bytes()).hexdigest(),
        "reader": archive_read_copilot.READER,
    }


def test_the_standard_page_yields_one_assistant_message_with_citations(tmp_path: Path) -> None:
    root = build_copilot_folder(tmp_path / "caps")
    (conversation,) = _read(root)[0]
    (message,) = conversation.messages
    assert message.role == "assistant"
    assert message.ts == ""
    assert message.attachments == []
    assert message.meta == {"kind": "text", "citations": GOLDEN_CITATIONS}


def test_the_standard_page_renders_to_the_exact_golden_markdown(tmp_path: Path) -> None:
    root = build_copilot_folder(tmp_path / "caps")
    (conversation,) = _read(root)[0]
    assert conversation.messages[0].text == GOLDEN


def test_the_golden_text_is_clean(tmp_path: Path) -> None:
    root = build_copilot_folder(tmp_path / "caps")
    text = _read(root)[0][0].messages[0].text
    assert text == text.strip()
    assert "\n\n\n" not in text
    assert all(line == line.rstrip() for line in text.split("\n"))


def test_counters_record_exactly_what_happened(tmp_path: Path) -> None:
    root = build_copilot_folder(tmp_path / "caps")
    _, tracker = _read(root)
    assert dict(tracker.counters) == {
        "pages_read": 1,
        "pages_skipped_not_copilot": 1,
        "citation_pills_stripped": 4,
        "source_cards": 2,
        "tables": 1,
    }


def test_only_copilot_pages_are_hashed_into_provenance(tmp_path: Path) -> None:
    root = build_copilot_folder(tmp_path / "caps")
    _, tracker = _read(root)
    page = root / COPILOT_PAGE_NAME
    assert tracker.files_read == [COPILOT_PAGE_NAME]
    assert tracker.hashes() == {COPILOT_PAGE_NAME: hashlib.sha256(page.read_bytes()).hexdigest()}


def test_page_chrome_never_reaches_the_text_or_the_title(tmp_path: Path) -> None:
    root = build_copilot_folder(tmp_path / "caps")
    (conversation,) = _read(root)[0]
    everything = conversation.title + "\n" + "\n".join(m.text for m in conversation.messages)
    for chrome in COPILOT_CHROME_STRINGS:
        assert chrome not in everything
    # the header block (badge + session title) is metadata, not body
    assert conversation.title == COPILOT_TITLE
    text = conversation.messages[0].text
    for echo in CHROME_ECHO:
        assert echo not in text
    assert "<" not in text


def test_a_plain_item_carries_no_citations_key(tmp_path: Path) -> None:
    conversation, _ = _probe(tmp_path, "<p>" + S("Just words."))
    assert conversation.messages[0].meta == {"kind": "text"}
    assert conversation.extra["sources"] == 0 and conversation.extra["tables"] == 0


def test_no_prompt_is_ever_invented(tmp_path: Path) -> None:
    conversation, _ = _probe(tmp_path, "<p>" + S("Only the report."), title=COPILOT_TITLE)
    assert [m.role for m in conversation.messages] == ["assistant"]
    assert COPILOT_TITLE not in conversation.messages[0].text


# --- items: ai, user, unknown -----------------------------------------------------------


def test_items_become_messages_in_document_order_with_their_roles(tmp_path: Path) -> None:
    spacer = "<div class=spacer>BETWEEN-ITEMS-CHROME</div>"
    page = copilot_page(
        [
            copilot_item("ai", "<p>" + S("Assistant answer."), title="Mixed Roles Session"),
            spacer,
            copilot_item(
                "user",
                "<div class=wrap><div class=inner><p>" + S("User question text.") + "</div></div>",
            ),
            spacer,
            copilot_item("tool", "<p>" + S("Tool note.")),
            copilot_item("bot", "<p>" + S("Bot note.")),
            "<div>AFTER-LAST-ITEM-CHROME</div>",
        ]
    )
    (conversation,), tracker = _page_only(tmp_path, page)
    assert conversation.title == "Mixed Roles Session"
    assert [(m.role, m.text) for m in conversation.messages] == [
        ("assistant", "Assistant answer."),
        ("user", "User question text."),
        ("system", "Tool note."),
        ("system", "Bot note."),
    ]
    assert all(m.meta == {"kind": "text"} and m.ts == "" for m in conversation.messages)
    assert dict(tracker.counters) == {
        "pages_read": 1,
        "unknown_item_roles:tool": 1,
        "unknown_item_roles:bot": 1,
    }
    everything = "\n".join(m.text for m in conversation.messages)
    assert "CHROME" not in everything


def test_unknown_roles_are_counted_once_per_item(tmp_path: Path) -> None:
    page = copilot_page(
        [
            copilot_item("tool", "<p>" + S("One.")),
            copilot_item("tool", "<p>" + S("Two.")),
            copilot_item("ai", "<p>" + S("Three.")),
        ]
    )
    (conversation,), tracker = _page_only(tmp_path, page)
    assert [m.role for m in conversation.messages] == ["system", "system", "assistant"]
    assert tracker.counters["unknown_item_roles:tool"] == 2


def test_a_user_only_page_stays_user_only(tmp_path: Path) -> None:
    conversation, _ = _probe(tmp_path, "<p>" + S("A typed question."), role="user", title=None)
    assert [(m.role, m.text) for m in conversation.messages] == [("user", "A typed question.")]


def test_an_item_that_never_closes_runs_to_the_end_of_the_page(tmp_path: Path) -> None:
    page = copilot_page(
        [
            copilot_item("ai", "<p>" + S("First text."), title="Unclosed Session"),
            copilot_item("user", "<p>" + S("Second text."), closed=False),
        ],
        wrap=False,
    )
    (conversation,), tracker = _page_only(tmp_path, page)
    assert [(m.role, m.text) for m in conversation.messages] == [
        ("assistant", "First text."),
        ("user", "Second text."),
    ]
    assert dict(tracker.counters) == {"pages_read": 1, "items_unterminated": 1}


def test_a_lone_unterminated_item_still_yields_its_message(tmp_path: Path) -> None:
    page = copilot_page([copilot_item("ai", "<p>" + S("Alpha text."), closed=False)], wrap=False)
    (conversation,), tracker = _page_only(tmp_path, page)
    assert [m.text for m in conversation.messages] == ["Alpha text."]
    assert tracker.counters["items_unterminated"] == 1


def test_a_page_with_no_items_still_yields_a_conversation(tmp_path: Path) -> None:
    (conversation,), tracker = _page_only(tmp_path, copilot_page("<div>nothing to read</div>"))
    assert conversation.messages == []
    assert conversation.title == "Untitled"
    assert conversation.source_id == COPILOT_ID
    assert conversation.updated == COPILOT_SAVED_ISO
    assert conversation.extra["mode"] == ""
    assert dict(tracker.counters) == {"pages_read": 1, "pages_without_messages": 1}


def test_an_item_with_no_text_still_yields_an_empty_message(tmp_path: Path) -> None:
    conversation, _ = _probe(tmp_path, "<div></div>")
    assert [(m.role, m.text) for m in conversation.messages] == [("assistant", "")]
    assert conversation.messages[0].meta == {"kind": "text"}


def test_a_pill_only_item_is_an_empty_message_and_the_pill_is_counted(tmp_path: Path) -> None:
    conversation, tracker = _probe(tmp_path, P())
    assert [m.text for m in conversation.messages] == [""]
    assert tracker.counters["citation_pills_stripped"] == 1


def test_uppercase_tags_and_unquoted_attributes_read_the_same(tmp_path: Path) -> None:
    item = (
        "<DIV CLASS=group/ai-message-item><DIV CLASS=text-3xl>Upper Title</DIV>"
        "<H2>Heading</H2><P>Upper para<UL><LI CLASS=ps-2>one<LI CLASS=ps-2>two</UL>"
        "<TABLE><TR><TH>c1<TH>c2<TR><TD>v1<TD>v2</TABLE></DIV>"
    )
    (conversation,), _ = _page_only(tmp_path, copilot_page(item))
    assert conversation.title == "Upper Title"
    assert [m.text for m in conversation.messages] == [
        "## Heading\n\nUpper para\n\n- one\n- two\n\n| c1 | c2 |\n| --- | --- |\n| v1 | v2 |"
    ]


# --- title and mode ---------------------------------------------------------------------


def test_the_title_is_the_collapsed_session_title(tmp_path: Path) -> None:
    conversation, _ = _probe(tmp_path, "<p>" + S("Body."), title="  Spaced \n   Session   Title ")
    assert conversation.title == "Spaced Session Title"
    assert conversation.messages[0].text == "Body."


def test_the_title_falls_back_to_the_first_heading_then_untitled(tmp_path: Path) -> None:
    body = "<h1>" + S("  Fallback   Report  Title ") + "</h1><p>" + S("Text.")
    conversation, _ = _probe(tmp_path / "a", body, title=None)
    assert conversation.title == "Fallback Report Title"
    assert conversation.messages[0].text == "# Fallback Report Title\n\nText."
    bare, _ = _probe(tmp_path / "b", "<p>" + S("Text."), title=None)
    assert bare.title == "Untitled"


def test_the_mode_is_the_badge_text(tmp_path: Path) -> None:
    root = tmp_path / "probe"
    item = copilot_item("ai", "<p>" + S("Body."), title="Mode Session", badge="Study Mode")
    write_copilot_page(root / "page.html", [item])
    (conversation,) = _read(root)[0]
    assert conversation.extra["mode"] == "Study Mode"
    assert "Study Mode" not in conversation.messages[0].text
    plain, _ = _probe(tmp_path / "other", "<p>" + S("Body."), title=None)
    assert plain.extra["mode"] == ""


# The header is the OUTERMOST div around the item's first `text-3xl` element that holds no body
# block (h1-h6, p, ul, ol, table, blockquote, pre, hr); a wrapper that also holds the report is
# refused and the next div inward is tried. Each case: (item content, title, mode, body text).
HEADER_CASES = [
    pytest.param(
        "<div class=wrap>"
        + _header_block("Wrapped Title")
        + "<h1>Report</h1><p>Body text</p></div>",
        "Wrapped Title",
        COPILOT_BADGE,
        "# Report\n\nBody text",
        id="wrapper-around-header-and-body-is-refused",
    ),
    pytest.param(
        "<div class=top><div class=chip>CHIP-TEXT </div>"
        + _header_block("Outer Title")
        + "</div><p>Body</p>",
        "Outer Title",
        f"CHIP-TEXT {COPILOT_BADGE}",
        "Body",
        id="the-outermost-clean-div-is-the-header",
    ),
    pytest.param(
        "<div class=text-3xl>Lone Title</div><p>Body text",
        "Lone Title",
        "",
        "Body text",
        id="text-3xl-div-alone-under-the-item",
    ),
    pytest.param(
        "<div class=prose><h1>Report</h1><p>Body</p><p class=text-3xl>42%</p></div>",
        "Report",
        "",
        "# Report\n\nBody\n\n42%",
        id="text-3xl-paragraph-in-the-body-is-no-header",
    ),
    pytest.param(
        "<div class=prose><h1>Report</h1><p>Body</p><span class=text-3xl>42%</span></div>",
        "Report",
        "",
        "# Report\n\nBody\n\n42%",
        id="text-3xl-span-in-the-body-is-no-header",
    ),
]


@pytest.mark.parametrize(("content", "title", "mode", "text"), HEADER_CASES)
def test_the_header_block_is_the_outermost_div_without_body_blocks(
    tmp_path: Path, content: str, title: str, mode: str, text: str
) -> None:
    conversation, _ = _probe(tmp_path, content, title=None)
    assert conversation.title == title
    assert conversation.extra["mode"] == mode
    assert [m.text for m in conversation.messages] == [text]


# A `text-3xl` div that comes after body blocks is content (a figure, a callout), not a header: a
# header opens the item. Found by the independent test author as a disagreement between the spec
# and the first form of the rule (a lone text-3xl div was its own innermost candidate, so its text
# left the body and became the title); the rule now also requires no body block before the header.
def test_a_text_3xl_div_inside_the_body_is_not_a_header(tmp_path: Path) -> None:
    body = "<div class=prose><h1>Report</h1><p>Body</p><div class=text-3xl>42%</div></div>"
    conversation, _ = _probe(tmp_path, body, title=None)
    assert conversation.title == "Report"
    assert conversation.extra["mode"] == ""
    assert [m.text for m in conversation.messages] == ["# Report\n\nBody\n\n42%"]


# --- url, id and date (spec 2.3) ----------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "source_id", "collection"),
    [
        (COPILOT_URL, COPILOT_ID, "research"),
        (
            "https://copilot.microsoft.com/chats/AbCdEfGhIjKlMnOpQrStU",
            "AbCdEfGhIjKlMnOpQrStU",
            "chats",
        ),
        (
            "https://copilot.microsoft.com/research/sub/Qw3Er4Ty5Ui6Op7As8Df9",
            "Qw3Er4Ty5Ui6Op7As8Df9",
            "research",
        ),
        (f"{COPILOT_URL}?src=share#top", COPILOT_ID, "research"),
        (f"{COPILOT_URL}?src=share", COPILOT_ID, "research"),
        (f"{COPILOT_URL}#top", COPILOT_ID, "research"),
        (f"{COPILOT_URL}/", COPILOT_ID, "research"),
        (
            "https://copilot.microsoft.com/a/b/Qw3Er4Ty5Ui6Op7As8Df9",
            "Qw3Er4Ty5Ui6Op7As8Df9",
            "a",
        ),
        (
            "https://copilot.microsoft.com/a/b/Qw3Er4Ty5Ui6Op7As8Df9?src=share#top",
            "Qw3Er4Ty5Ui6Op7As8Df9",
            "a",
        ),
    ],
)
def test_the_id_is_the_last_path_segment_and_the_collection_the_first(
    tmp_path: Path, url: str, source_id: str, collection: str
) -> None:
    conversation, tracker = _probe(tmp_path, "<p>x", url=url)
    assert conversation.source_id == source_id
    assert conversation.collection == collection
    assert "pages_without_url" not in tracker.counters


@pytest.mark.parametrize(
    "url",
    [
        "https://copilot.microsoft.com/",
        "https://copilot.microsoft.com",
        # a lone segment could as well be the id or the surface, so it is neither
        f"https://copilot.microsoft.com/{COPILOT_ID}",
        f"https://copilot.microsoft.com/{COPILOT_ID}?src=share#top",
    ],
)
def test_a_url_without_an_id_degrades_visibly(tmp_path: Path, url: str) -> None:
    conversation, tracker = _probe(tmp_path, "<p>" + S("Some text."), url=url)
    assert conversation.source_id == ""
    assert conversation.collection == ""
    assert conversation.messages[0].text == "Some text."
    assert tracker.counters["pages_without_url"] == 1
    reason = finalize_conversation(conversation)
    assert reason == "empty"
    assert conversation.source_id.startswith("derived-")
    assert conversation.extra["source_id_derived"] == "empty"


@pytest.mark.parametrize(
    ("saved", "iso"),
    [
        (COPILOT_SAVED, "2026-03-10T13:15:00Z"),
        ("Sun Jan 04 2026 23:30:00 GMT+0530 (India Standard Time)", "2026-01-04T18:00:00Z"),
        ("Wed Dec 31 2025 22:00:00 GMT-0500 (Eastern Standard Time)", "2026-01-01T03:00:00Z"),
        ("Thu Jul 02 2026 00:05:09 GMT+0000 (Coordinated Universal Time)", "2026-07-02T00:05:09Z"),
        (
            "Mon Feb 02 2026 01:00:00 GMT+0200 (Central European Summer Time)",
            "2026-02-01T23:00:00Z",
        ),
    ],
)
def test_the_saved_date_becomes_utc(tmp_path: Path, saved: str, iso: str) -> None:
    conversation, tracker = _probe(tmp_path, "<p>x", saved=saved)
    assert conversation.updated == iso
    assert conversation.extra["captured_at"] == iso
    assert conversation.created == ""
    assert "saved_date_unparsed" not in tracker.counters


@pytest.mark.parametrize(
    "saved",
    [
        None,
        "not a date at all",
        "Tue Feb 30 2026 09:15:00 GMT-0400 (Eastern Daylight Time)",
        # the local time is a valid datetime but its UTC moment is not: OverflowError, not a crash
        "Mon Jan 01 0001 00:30:00 GMT+0100 (Zone)",
        "Fri Dec 31 9999 23:30:00 GMT-0100 (Zone)",
        "Mon Jan 01 0001 00:59:59 GMT+0100 (Zone)",
        "Fri Dec 31 9999 23:00:00 GMT-0100 (Zone)",
    ],
    ids=[
        "absent",
        "garbage",
        "impossible-day",
        "year-one-ahead-of-utc",
        "year-9999-behind-utc",
        "one-second-before-year-one-in-utc",
        "one-hour-into-year-10000-in-utc",
    ],
)
def test_a_missing_or_unparsable_date_degrades_visibly(tmp_path: Path, saved: str | None) -> None:
    conversation, tracker = _probe(tmp_path, "<p>" + S("Kept."), saved=saved)
    assert conversation.updated == ""
    assert conversation.extra["captured_at"] == ""
    assert conversation.messages[0].text == "Kept."
    assert conversation.source_id == COPILOT_ID
    assert tracker.counters["saved_date_unparsed"] == 1


@pytest.mark.parametrize(
    ("saved", "iso"),
    [
        ("Mon Jan 01 0001 01:00:00 GMT+0100 (Zone)", "0001-01-01T00:00:00Z"),
        ("Mon Jan 01 0001 00:30:00 GMT-0100 (Zone)", "0001-01-01T01:30:00Z"),
        ("Fri Dec 31 9999 22:59:59 GMT-0100 (Zone)", "9999-12-31T23:59:59Z"),
        ("Fri Dec 31 9999 23:30:00 GMT+0100 (Zone)", "9999-12-31T22:30:00Z"),
    ],
    ids=["first-utc-second", "year-one-behind-utc", "last-utc-second", "year-9999-ahead-of-utc"],
)
def test_a_date_at_the_ends_of_the_calendar_still_parses_when_its_utc_moment_fits(
    tmp_path: Path, saved: str, iso: str
) -> None:
    conversation, tracker = _probe(tmp_path, "<p>x", saved=saved)
    assert conversation.updated == iso
    assert conversation.extra["captured_at"] == iso
    assert "saved_date_unparsed" not in tracker.counters


# --- pills ---------------------------------------------------------------------------------


def test_pills_are_counted_per_button(tmp_path: Path) -> None:
    body = "<p>" + S("One ") + P() + S("two ") + P() + P() + S("three.")
    conversation, tracker = _probe(tmp_path, body)
    assert tracker.counters["citation_pills_stripped"] == 3
    assert conversation.messages[0].text == "One two three."


# A pill right before a CLOSING mark leaves no space behind it: `claim <pill>.` -> `claim.`.
# Marks that could as well open something (straight quotes, dashes, opening brackets) keep it.
CLOSING_MARKS = {
    "period": ".",
    "comma": ",",
    "semicolon": ";",
    "colon": ":",
    "exclamation": "!",
    "question": "?",
    "paren": ")",
    "bracket": "]",
    "brace": "}",
    "curly-double-quote": "”",
    "curly-single-quote": "’",
    "guillemet": "»",
    "ellipsis": "…",
}
KEPT_SPACE_MARKS = {
    "straight-double-quote": '"',
    "straight-single-quote": "'",
    "em-dash": "—",
    "en-dash": "–",
    "curly-opening-quote": "“",
    "opening-paren": "(",
}


@pytest.mark.parametrize("mark", list(CLOSING_MARKS.values()), ids=list(CLOSING_MARKS))
def test_a_pill_before_a_closing_mark_takes_the_space_before_it_along(
    tmp_path: Path, mark: str
) -> None:
    conversation, tracker = _probe(tmp_path, "<p>" + S("a ") + P() + S(f"{mark} b"))
    assert conversation.messages[0].text == f"a{mark} b"
    assert tracker.counters["citation_pills_stripped"] == 1


@pytest.mark.parametrize("mark", list(KEPT_SPACE_MARKS.values()), ids=list(KEPT_SPACE_MARKS))
def test_a_pill_before_a_mark_that_could_open_something_keeps_the_space(
    tmp_path: Path, mark: str
) -> None:
    conversation, _ = _probe(tmp_path, "<p>" + S("a ") + P() + S(f"{mark} b"))
    assert conversation.messages[0].text == f"a {mark} b"


@pytest.mark.parametrize(
    ("unit", "count", "pills"),
    [
        ("<button>p</button><span> </span>", PILL_RUN, PILL_RUN),
        ("<span> </span>", BLANK_RUN, 0),
    ],
    ids=["pills-each-followed-by-a-blank-span", "blank-spans"],
)
def test_a_very_long_run_of_pills_or_blank_spans_reads_in_linear_time(
    tmp_path: Path, unit: str, count: int, pills: int
) -> None:
    body = "<p>" + S("a ") + unit * count + "<span>b</span>"
    root = tmp_path / "caps"
    write_copilot_page(root / "page.html", [copilot_item("ai", body, title="Scale Session")])
    started = time.perf_counter()
    (conversation,), tracker = _read(root)
    elapsed = time.perf_counter() - started
    assert conversation.messages[0].text == "a b"
    assert tracker.counters["citation_pills_stripped"] == pills
    assert elapsed < SCALE_SECONDS, f"{count} elements took {elapsed:.1f}s"


def test_an_item_that_ends_inside_a_skipped_subtree_keeps_its_text_and_is_counted(
    tmp_path: Path,
) -> None:
    page = copilot_page(
        [
            copilot_item("ai", "<p>before<button>pill<p>AFTER", title="Skip Session"),
            copilot_item("user", "<p>" + S("Next item.")),
        ]
    )
    (conversation,), tracker = _page_only(tmp_path, page)
    assert [(m.role, m.text) for m in conversation.messages] == [
        ("assistant", "before"),
        ("user", "Next item."),
    ]
    assert tracker.counters["skips_unterminated"] == 1
    assert tracker.counters["citation_pills_stripped"] == 1


# --- cards ---------------------------------------------------------------------------------

LAMPS = "lamps.example.org"
CARD_CASES = [
    pytest.param(
        [copilot_card(1, LAMPS, "Wick Guide", None)],
        True,
        f"1. Wick Guide — {LAMPS}",
        [{"index": 1, "site": LAMPS, "text": "Wick Guide", "url": ""}],
        id="no-url",
    ),
    pytest.param(
        [copilot_card(3, LAMPS, None, URL0)],
        True,
        f"3. {LAMPS}",
        [{"index": 3, "site": LAMPS, "text": "", "url": URL0}],
        id="no-title",
    ),
    pytest.param(
        [copilot_card(2, None, "Just A Title", URL0)],
        True,
        f"2. [Just A Title]({URL0})",
        [{"index": 2, "site": "", "text": "Just A Title", "url": URL0}],
        id="no-site",
    ),
    pytest.param(
        [copilot_card(2, None, "Just A Title", None)],
        True,
        "2. Just A Title",
        [{"index": 2, "site": "", "text": "Just A Title", "url": ""}],
        id="no-site-no-url",
    ),
    pytest.param(
        [copilot_card(None, LAMPS, "Title", URL0)],
        True,
        f"1. [Title]({URL0}) — {LAMPS}",
        [{"index": None, "site": LAMPS, "text": "Title", "url": URL0}],
        id="no-index",
    ),
    pytest.param(
        [copilot_card(4, LAMPS, ("Part one", "Part two"), URL0)],
        True,
        f"4. [Part one Part two]({URL0}) — {LAMPS}",
        [{"index": 4, "site": LAMPS, "text": "Part one Part two", "url": URL0}],
        id="title-in-two-segments",
    ),
    pytest.param(
        [copilot_card(12, LAMPS, "Twelve", URL0)],
        True,
        f"12. [Twelve]({URL0}) — {LAMPS}",
        [{"index": 12, "site": LAMPS, "text": "Twelve", "url": URL0}],
        id="two-digit-index",
    ),
    pytest.param(
        [copilot_card(1, "localhost", "Title", URL0)],
        True,
        f"1. [localhost Title]({URL0})",
        [{"index": 1, "site": "", "text": "localhost Title", "url": URL0}],
        id="dotless-site-stays-in-the-title",
    ),
    pytest.param(
        [copilot_card(1, LAMPS, "Title", ("javascript:void(0)", "/relative", URL1))],
        True,
        f"1. [Title]({URL1}) — {LAMPS}",
        [{"index": 1, "site": LAMPS, "text": "Title", "url": URL1}],
        id="first-http-anchor-wins",
    ),
    pytest.param(
        [
            copilot_card(5, LAMPS, "T5", URL0),
            copilot_card(None, LAMPS, "Tpos", URL1),
            copilot_card(7, LAMPS, "T7", URL0),
        ],
        True,
        "\n".join(
            [
                f"5. [T5]({URL0}) — {LAMPS}",
                f"2. [Tpos]({URL1}) — {LAMPS}",
                f"7. [T7]({URL0}) — {LAMPS}",
            ]
        ),
        [
            {"index": 5, "site": LAMPS, "text": "T5", "url": URL0},
            {"index": None, "site": LAMPS, "text": "Tpos", "url": URL1},
            {"index": 7, "site": LAMPS, "text": "T7", "url": URL0},
        ],
        id="position-numbering-for-a-card-without-index",
    ),
    pytest.param(
        [copilot_card(1, LAMPS, "Last", URL0)],
        False,
        f"1. [Last]({URL0}) — {LAMPS}",
        [{"index": 1, "site": LAMPS, "text": "Last", "url": URL0}],
        id="list-never-closes",
    ),
    pytest.param(
        [copilot_card("1234567890" * 4, "s.example.org", "T", URL0)],
        True,
        f"1. [T]({URL0}) — s.example.org",
        [{"index": None, "site": "s.example.org", "text": "T", "url": URL0}],
        id="forty-digit-badge-is-consumed-and-the-site-still-follows",
    ),
    # the digits are spelled out, not read from `INDEX_DIGITS`, so a changed cap fails here
    pytest.param(
        [copilot_card("123456789", LAMPS, "T", URL0)],
        True,
        f"123456789. [T]({URL0}) — {LAMPS}",
        [{"index": 123456789, "site": LAMPS, "text": "T", "url": URL0}],
        id="nine-digit-badge-is-the-longest-trusted-index",
    ),
    pytest.param(
        [copilot_card("1234567890", LAMPS, "T", URL0)],
        True,
        f"1. [T]({URL0}) — {LAMPS}",
        [{"index": None, "site": LAMPS, "text": "T", "url": URL0}],
        id="ten-digit-badge-is-consumed-but-not-trusted",
    ),
    pytest.param(
        [copilot_card(1, LAMPS, "Part 1] notes", URL0)],
        True,
        f"1. [Part 1\\] notes]({URL0}) — {LAMPS}",
        [{"index": 1, "site": LAMPS, "text": "Part 1] notes", "url": URL0}],
        id="bracket-in-the-title-is-escaped",
    ),
    pytest.param(
        [copilot_card(1, LAMPS, "Title", "https://lamps.example.org/a b(c)")],
        True,
        f"1. [Title](https://lamps.example.org/a%20b%28c%29) — {LAMPS}",
        [{"index": 1, "site": LAMPS, "text": "Title", "url": "https://lamps.example.org/a b(c)"}],
        id="space-and-parentheses-are-encoded-in-the-link-not-in-the-citation",
    ),
    pytest.param(
        [copilot_card(1, LAMPS, "Title", (URL0, URL1))],
        True,
        f"1. [Title]({URL0}) — {LAMPS}",
        [{"index": 1, "site": LAMPS, "text": "Title", "url": URL0}],
        id="the-first-of-two-http-anchors-wins",
    ),
    pytest.param(
        [copilot_card(1, None, "Report v2.0", URL0)],
        True,
        f"1. [Report v2.0]({URL0})",
        [{"index": 1, "site": "", "text": "Report v2.0", "url": URL0}],
        id="a-segment-with-a-dot-and-a-space-is-a-title-not-a-site",
    ),
    pytest.param(
        [copilot_card(1, LAMPS, "Title", "httpfoo://x.example.org")],
        True,
        f"1. Title — {LAMPS}",
        [{"index": 1, "site": LAMPS, "text": "Title", "url": ""}],
        id="httpfoo-scheme-is-no-card-url",
    ),
    pytest.param(
        [copilot_card(1, LAMPS, "Title", "ftp://x.example.org")],
        True,
        f"1. Title — {LAMPS}",
        [{"index": 1, "site": LAMPS, "text": "Title", "url": ""}],
        id="ftp-scheme-is-no-card-url",
    ),
    pytest.param(
        [copilot_card(1, LAMPS, "Title", ("httpfoo://x.example.org", "ftp://x.example.org", URL1))],
        True,
        f"1. [Title]({URL1}) — {LAMPS}",
        [{"index": 1, "site": LAMPS, "text": "Title", "url": URL1}],
        id="near-miss-schemes-are-passed-over-for-a-real-http-anchor",
    ),
]


@pytest.mark.parametrize(("cards", "closed", "expected", "citations"), CARD_CASES)
def test_source_cards_render_in_place_and_fill_the_citations(
    tmp_path: Path, cards: list[str], closed: bool, expected: str, citations: list[dict[str, Any]]
) -> None:
    conversation, tracker = _probe(tmp_path, copilot_card_list(*cards, closed=closed))
    (message,) = conversation.messages
    assert message.text == expected
    assert message.meta == {"kind": "text", "citations": citations}
    assert tracker.counters["source_cards"] == len(cards)
    assert conversation.extra["sources"] == len(cards)


def test_cards_sit_in_place_between_the_blocks_around_them(tmp_path: Path) -> None:
    body = (
        "<p>"
        + S("Before the list.")
        + copilot_card_list(copilot_card(1, LAMPS, "Wick Guide", URL0))
        + "<p>"
        + S("After the list.")
    )
    conversation, _ = _probe(tmp_path, body)
    assert conversation.messages[0].text == (
        f"Before the list.\n\n1. [Wick Guide]({URL0}) — {LAMPS}\n\nAfter the list."
    )


def test_citations_belong_to_the_item_that_held_the_cards(tmp_path: Path) -> None:
    cards = copilot_card_list(copilot_card(None, LAMPS, "Only card", URL0))
    page = copilot_page(
        [
            copilot_item("user", "<p>" + S("Question.")),
            copilot_item("ai", "<p>" + S("Answer.") + cards, title="Cited Session"),
            copilot_item("ai", "<p>" + S("Follow-up with no cards.")),
        ]
    )
    (conversation,), _ = _page_only(tmp_path, page)
    assert "citations" not in conversation.messages[0].meta
    assert conversation.messages[1].meta["citations"] == [
        {"index": None, "site": LAMPS, "text": "Only card", "url": URL0}
    ]
    assert "citations" not in conversation.messages[2].meta
    assert conversation.messages[1].text == f"Answer.\n\n1. [Only card]({URL0}) — {LAMPS}"


def test_a_source_list_whose_end_tag_never_comes_does_not_swallow_what_follows(
    tmp_path: Path,
) -> None:
    cards = copilot_card_list(
        copilot_card(1, LAMPS, "Wick Guide", URL0),
        copilot_card(2, "oil.example.com", "Glass Care", URL1),
        closed=False,
    )
    conversation, tracker = _probe(tmp_path, cards + "<h2>Next Heading</h2><p>AFTER TEXT</p>")
    (message,) = conversation.messages
    assert message.text == "\n\n".join(
        [
            f"1. [Wick Guide]({URL0}) — {LAMPS}\n2. [Glass Care]({URL1}) — oil.example.com",
            "## Next Heading",
            "AFTER TEXT",
        ]
    )
    assert message.meta["citations"] == [
        {"index": 1, "site": LAMPS, "text": "Wick Guide", "url": URL0},
        {"index": 2, "site": "oil.example.com", "text": "Glass Care", "url": URL1},
    ]
    assert tracker.counters["source_cards"] == 2


def test_an_empty_source_card_renders_nothing_and_is_not_counted(tmp_path: Path) -> None:
    conversation, tracker = _probe(tmp_path, '<ul><li class="list-none w-full"><span></span></ul>')
    (message,) = conversation.messages
    assert message.text == ""
    assert message.meta == {"kind": "text"}
    assert "source_cards" not in tracker.counters
    assert conversation.extra["sources"] == 0


# --- rendering rules (spec section 3) -----------------------------------------------------

RENDER_CASES = [
    pytest.param(
        "<h1>One</h1><h2>Two</h2><h3>Three</h3><h4>Four</h4><h5>Five</h5><h6>Six</h6>",
        "# One\n\n## Two\n\n### Three\n\n#### Four\n\n##### Five\n\n###### Six",
        id="headings-one-to-six",
    ),
    pytest.param(
        "<h2><span>  Spaced  </span><span> out   heading </span></h2>",
        "## Spaced out heading",
        id="heading-text-is-collapsed",
    ),
    pytest.param(
        "<div><span>Alpha</span></div><section><span>Beta</span></section>"
        "<article><span>Gamma</span><p><span>Delta</span></article>",
        "Alpha\n\nBeta\n\nGamma\n\nDelta",
        id="div-section-article-and-p-are-paragraph-boundaries",
    ),
    pytest.param(
        "<span>before </span><h2>Mid</h2><span> after</span>",
        "before\n\n## Mid\n\nafter",
        id="a-block-splits-the-text-around-it",
    ),
    pytest.param(
        "<p>" + S("line one ") + "<br>" + S("line two"),
        "line one\nline two",
        id="br-is-a-newline-without-trailing-space",
    ),
    pytest.param(
        "<p><span>a&nbsp;&nbsp;b</span><span>  c\n\td </span></p>",
        "a b c d",
        id="whitespace-and-nbsp-runs-collapse",
    ),
    pytest.param(
        "<p><strong>bold </strong>word",
        "**bold** word",
        id="strong-edge-space-moves-outside",
    ),
    pytest.param(
        "<p><span>a</span><em> lead and trail </em><span>b</span>",
        "a *lead and trail* b",
        id="em-edge-spaces-move-outside",
    ),
    pytest.param(
        "<p><b>bb</b><span> </span><i>ii</i>",
        "**bb** *ii*",
        id="b-and-i-are-strong-and-em",
    ),
    pytest.param(
        "<p><span>x </span><strong> </strong><em></em><span>y</span>",
        "x y",
        id="empty-emphasis-is-dropped",
    ),
    pytest.param(
        "<p><span>run </span><code>wickctl --trim</code><span> now</span>",
        "run `wickctl --trim` now",
        id="inline-code",
    ),
    pytest.param(
        '<p><span>See </span><a href="https://docs.example.org/x"><span>the </span>'
        '<span>docs</span></a><span> and </span><a href="mailto:a@example.invalid">mail us</a>'
        '<span> or </span><a href="/relative">here</a><span>.</span>'
        '<a href="https://empty.example.org"></a>',
        "See [the docs](https://docs.example.org/x) and mail us or here.",
        id="links-http-only-and-empty-text-vanishes",
    ),
    pytest.param(
        "<p>" + S("claim ") + P() + S("."),
        "claim.",
        id="pill-before-a-period",
    ),
    pytest.param(
        "<p>" + S("a ") + P() + S(", b"),
        "a, b",
        id="pill-before-a-comma",
    ),
    pytest.param(
        "<p>" + S("one ") + P() + S(" two"),
        "one two",
        id="pill-between-words-never-doubles-the-space",
    ),
    pytest.param(
        "<p>" + P() + S("Start here."),
        "Start here.",
        id="pill-at-the-start",
    ),
    pytest.param(
        "<p>" + S("end ") + P(),
        "end",
        id="pill-at-the-end",
    ),
    pytest.param(
        "<p>" + S("x ") + P() + P() + S(". y"),
        "x. y",
        id="two-pills-in-a-row-before-punctuation",
    ),
    pytest.param(
        "<p><strong>" + S("key ") + P() + "</strong>" + S(" rest"),
        "**key** rest",
        id="pill-inside-strong",
    ),
    pytest.param(
        "<ul><li class=ps-2><span>one</span><li class=ps-2><span>two</span></li>"
        "<li class=ps-2><span>three</span></ul>",
        "- one\n- two\n- three",
        id="ul-with-and-without-li-end-tags",
    ),
    pytest.param(
        "<ol><li>one<li>two</ol>",
        "1. one\n2. two",
        id="ol-starts-at-one",
    ),
    pytest.param(
        "<ol start=7><li>a<li>b<li>c</ol>",
        "7. a\n8. b\n9. c",
        id="ol-start-attribute",
    ),
    pytest.param(
        "<ul><li><span>Outer one</span><ul><li><span>Inner one</span>"
        "<li><span>Inner two</span></ul><li><span>Outer two</span></ul>",
        "- Outer one\n  - Inner one\n  - Inner two\n- Outer two",
        id="nested-ul-indents-two-spaces",
    ),
    pytest.param(
        "<ol><li>One<ul><li>Sub a<ol start=5><li>Deep x<li>Deep y</ol><li>Sub b</ul><li>Two</ol>",
        "1. One\n  - Sub a\n    5. Deep x\n    6. Deep y\n  - Sub b\n2. Two",
        id="mixed-nesting-counts-per-list",
    ),
    pytest.param(
        '<ul><li><strong>Key</strong><span>: value</span><li><a href="https://x.example.org">'
        "site</a></ul>",
        "- **Key**: value\n- [site](https://x.example.org)",
        id="list-items-keep-inline-formatting",
    ),
    pytest.param(
        "<p>Intro<ul><li>a<li>b</ul><p>Outro",
        "Intro\n\n- a\n- b\n\nOutro",
        id="a-list-is-one-block-between-paragraphs",
    ),
    pytest.param(
        "<blockquote><p>First quoted.<p>Second quoted.<blockquote><p>Inner quote.</blockquote>"
        "</blockquote>",
        "> First quoted.\n>\n> Second quoted.\n>\n> > Inner quote.",
        id="blockquote-blank-separators-and-nesting",
    ),
    pytest.param(
        "<blockquote><p>Intro<ul><li>a<li>b</ul></blockquote>",
        "> Intro\n>\n> - a\n> - b",
        id="blockquote-holding-a-list",
    ),
    pytest.param(
        "<p>Before<blockquote><p>Quoted</blockquote><p>After",
        "Before\n\n> Quoted\n\nAfter",
        id="blockquote-is-one-block",
    ),
    pytest.param(
        "<blockquote><p>one<br>two</blockquote>",
        "> one\n> two",
        id="every-quoted-line-gets-the-prefix",
    ),
    pytest.param(
        "<table><tr><td>H1<td>H2<tr><td>a<td>b</table>",
        "| H1 | H2 |\n| --- | --- |\n| a | b |",
        id="table-without-th-uses-the-first-row-as-header",
    ),
    pytest.param(
        "<table><thead><tr><th>H1<th>H2<tbody><tr><td>a<td>b<td>c<tr><td>x</table>",
        "| H1 | H2 |  |\n| --- | --- | --- |\n| a | b | c |\n| x |  |  |",
        id="ragged-rows-are-padded-to-the-widest",
    ),
    pytest.param(
        "<table><tr><td><div>alpha</div><div>beta</div><td>gamma<tr><td>1<td>2</table>",
        "| alpha beta | gamma |\n| --- | --- |\n| 1 | 2 |",
        id="div-inside-a-cell-becomes-one-space",
    ),
    pytest.param(
        "<table><tr><th><p>one<p>two<th>three<tr><td>1<td>2</table>",
        "| one two | three |\n| --- | --- |\n| 1 | 2 |",
        id="paragraphs-inside-a-cell-become-one-space",
    ),
    pytest.param(
        "<p>Before<table><tr><th>H<tr><td>v</table><p>After",
        "Before\n\n| H |\n| --- |\n| v |\n\nAfter",
        id="a-table-is-one-block",
    ),
    pytest.param(
        "<p>Before<table></table><p>After",
        "Before\n\nAfter",
        id="a-table-with-no-rows-is-dropped",
    ),
    pytest.param(
        "<table><tr><th>a|b<tr><td>c</table>",
        "| a\\|b |\n| --- |\n| c |",
        id="pipe-in-a-header-cell",
    ),
    pytest.param(
        '<pre><code class="language-python">\nx = 1\n  y = 2\n</code></pre>',
        "```python\nx = 1\n  y = 2\n```",
        id="pre-with-language",
    ),
    pytest.param(
        "<pre>a &lt; b &amp;&amp; c\n\n  indented</pre>",
        "```\na < b && c\n\n  indented\n```",
        id="pre-keeps-whitespace-verbatim",
    ),
    pytest.param(
        "<p>Before<pre>code</pre><p>After",
        "Before\n\n```\ncode\n```\n\nAfter",
        id="pre-is-one-block",
    ),
    pytest.param(
        "<pre><code>plain</code></pre>",
        "```\nplain\n```",
        id="pre-code-without-language",
    ),
    pytest.param(
        "<p>Above<hr><p>Below",
        "Above\n\n---\n\nBelow",
        id="hr-is-a-block",
    ),
    pytest.param(
        '<p><span>Before </span><img src="data:image/png;base64,AAAA" alt=IMG-ALT-GONE>'
        "<span>after</span><p><img src=x><p>Kept",
        "Before after\n\nKept",
        id="img-renders-nothing",
    ),
    pytest.param(
        '<p><span>Keep</span><svg viewBox="0 0 1 1"><title>SVG-TITLE</title><path d="M0"/></svg>'
        "<script>var SCRIPT_TEXT=1;</script><noscript>NOSCRIPT-TEXT</noscript>"
        "<style>.x{color:red}</style><template>TEMPLATE-TEXT</template>"
        "<dialog>DIALOG-INSIDE-ITEM</dialog><span> this</span>",
        "Keep this",
        id="skipped-subtrees",
    ),
    pytest.param(
        "<p>Alpha</p><p></p><div> </div><h3></h3><p> </p><section><span> </span></section>"
        "<p>Beta</p>",
        "Alpha\n\nBeta",
        id="empty-blocks-are-dropped",
    ),
    pytest.param(
        "<p> </p><div></div><p>Kept</p>",
        "Kept",
        id="no-leading-blank-blocks",
    ),
    # containers kept as inline text must not let their end tags close a real container
    pytest.param(
        "<blockquote><ul><li>a<blockquote>b</blockquote><li>c</ul></blockquote><p>after",
        "> - a b\n> - c\n\nafter",
        id="quote-kept-as-text-in-a-list-item-cannot-close-the-real-quote",
    ),
    pytest.param(
        "<table><tr><td>x<ul><li>a<li>b</ul> y<td>z</table><ul><li>one<li>two</ul><p>after",
        "| x a b y | z |\n| --- | --- |\n\n- one\n- two\n\nafter",
        id="list-kept-as-text-in-a-cell-leaves-the-next-real-list-alone",
    ),
    pytest.param(
        "<blockquote>" * 10 + "<p>deep text</p>" + "</blockquote>" * 10,
        "> " * 10 + "deep text",
        id="ten-nested-quotes-get-ten-prefixes",
    ),
    pytest.param(
        "<ul><li>run<pre>x = 1\n  y = 2</pre><li>next</ul>",
        "- run x = 1 y = 2\n- next",
        id="pre-inside-a-list-item-is-flattened-to-inline-text",
    ),
    # pills before closing marks
    pytest.param(
        "<p>" + S("he said “claim ") + P() + S("”."),
        "he said “claim”.",
        id="pill-before-a-closing-curly-quote",
    ),
    # the start attribute of a list
    pytest.param("<ol start=3><li>a<li>b</ol>", "3. a\n4. b", id="ol-plain-start"),
    pytest.param(
        '<ol start=" 7 "><li>a<li>b</ol>', "7. a\n8. b", id="ol-start-is-stripped-before-parsing"
    ),
    pytest.param(
        "<ol start=1_000><li>a<li>b</ol>", "1. a\n2. b", id="ol-start-with-an-underscore-is-ignored"
    ),
    pytest.param("<ol start=-3><li>a<li>b</ol>", "1. a\n2. b", id="ol-negative-start-is-ignored"),
    pytest.param(
        "<ol start=12345678901><li>a<li>b</ol>",
        "1. a\n2. b",
        id="ol-eleven-digit-start-is-ignored",
    ),
    # escaping in links and cells
    pytest.param(
        '<p><a href="https://x.example.org">a]b</a>',
        "[a\\]b](https://x.example.org)",
        id="bracket-in-link-text-is-escaped",
    ),
    pytest.param(
        '<p><a href="https://x.example.org/a b(c)">t</a>',
        "[t](https://x.example.org/a%20b%28c%29)",
        id="space-and-parentheses-in-an-href-are-encoded",
    ),
    pytest.param(
        '<p><a href="httpfoo://x.example.org">t</a><span> and </span>'
        '<a href="ftp://x.example.org">u</a>',
        "t and u",
        id="near-miss-and-foreign-schemes-make-no-inline-link",
    ),
    pytest.param(
        r"<table><tr><th>k<th>v<th>w<tr><td>a|b<td>a\|b<td>c\d</table>",
        "| k | v | w |\n| --- | --- | --- |\n" + r"| a\|b | a\\\|b | c\d |",
        id="pipe-is-escaped-and-a-backslash-before-one-is-doubled",
    ),
    # line breaks
    pytest.param(
        "<table><tr><th>a<br>b<th>c<tr><td>1<td>2</table>",
        "| a b | c |\n| --- | --- |\n| 1 | 2 |",
        id="br-in-a-table-cell-is-a-space",
    ),
    pytest.param("<h2>a<br>b</h2>", "## a b", id="br-in-a-heading-is-a-space"),
    pytest.param("<p>a<br><br><br><br>b", "a\n\nb", id="four-br-in-a-row-make-one-blank-line"),
    pytest.param(
        "<p><strong>x<br></strong>y",
        "**x**\ny",
        id="br-at-the-end-of-strong-stays-outside-the-delimiters",
    ),
    # nested inline marks
    pytest.param(
        '<p><a href="https://x.example.org"><strong>bold</strong> link</a>',
        "[**bold** link](https://x.example.org)",
        id="strong-inside-a-link",
    ),
    pytest.param("<p><strong><em>both</em></strong>", "***both***", id="em-inside-strong"),
    # a heading inside a list item is flattened into the item's inline text
    pytest.param(
        "<ul><li>one<h3>Head</h3> two</ul>",
        "- one Head two",
        id="heading-inside-a-list-item-is-flattened-to-inline-text",
    ),
    # a flattened heading ends as it began: with no whitespace in the source, what follows is still
    # a separate word (the reader used to add the leading space only and rendered `- Headtext`)
    pytest.param(
        "<ul><li><h3>Head</h3>text</ul>",
        "- Head text",
        id="heading-inside-a-list-item-with-no-source-whitespace",
    ),
    # tables
    pytest.param(
        "<table><tr><th>a<th>b<tr><td>x <table><tr><td>in1<td>in2</table> y<td>z"
        "<tr><td>p<td>q</table><p>after",
        "| a | b |\n| --- | --- |\n| x in1 in2 y | z |\n| p | q |\n\nafter",
        id="table-in-a-cell-is-flattened-and-the-outer-table-stays-intact",
    ),
    pytest.param(
        "<table><tr><th>a<th>b</tr><tr><td>x <table><tr><td>in1</td><td>in2</td></tr></table> y"
        "</td><td>z</td></tr></table><p>after",
        "| a | b |\n| --- | --- |\n| x in1 in2 y | z |\n\nafter",
        id="table-in-a-cell-with-all-end-tags-leaves-the-outer-end-tags-working",
    ),
    pytest.param(
        "<table><tr>stray<td>x</table>",
        "| stray | x |\n| --- | --- |",
        id="text-in-a-row-outside-any-cell-opens-an-implicit-cell",
    ),
]


@pytest.mark.parametrize(("body", "expected"), RENDER_CASES)
def test_markdown_rendering_rules(tmp_path: Path, body: str, expected: str) -> None:
    conversation, _ = _probe(tmp_path, body)
    assert [m.text for m in conversation.messages] == [expected]


@pytest.mark.parametrize(("body", "expected"), RENDER_CASES)
def test_markdown_rendering_rules_without_a_header_block(
    tmp_path: Path, body: str, expected: str
) -> None:
    """The same cases with no header block: the header only moves metadata, never the body."""
    conversation, _ = _probe(tmp_path, body, title=None)
    assert [m.text for m in conversation.messages] == [expected]


# --- omitted end tags must not nest ---------------------------------------------------------


def test_thirty_unclosed_list_items_stay_flat(tmp_path: Path) -> None:
    items = "".join(f"<li class=ps-2><span>Step {i} text</span>" for i in range(1, 31))
    conversation, _ = _probe(tmp_path / "ul", f"<ul class=x>{items}</ul><p>Tail paragraph")
    expected = "\n".join(f"- Step {i} text" for i in range(1, 31))
    assert conversation.messages[0].text == expected + "\n\nTail paragraph"
    ordered, _ = _probe(tmp_path / "ol", f"<ol class=x>{items}</ol>")
    assert ordered.messages[0].text == "\n".join(f"{i}. Step {i} text" for i in range(1, 31))


def test_a_ten_by_four_table_with_no_cell_or_row_end_tags_stays_flat(tmp_path: Path) -> None:
    head = "<thead><tr>" + "".join(f"<th>H{c}" for c in range(1, 5)) + "<tbody>"
    rows = "".join("<tr>" + "".join(f"<td>r{r}c{c}" for c in range(1, 5)) for r in range(1, 10))
    conversation, tracker = _probe(tmp_path, f"<table>{head}{rows}</table><p>After table")
    expected = [
        "| H1 | H2 | H3 | H4 |",
        "| --- | --- | --- | --- |",
        *(f"| r{r}c1 | r{r}c2 | r{r}c3 | r{r}c4 |" for r in range(1, 10)),
    ]
    assert conversation.messages[0].text == "\n".join(expected) + "\n\nAfter table"
    assert tracker.counters["tables"] == 1


def test_two_hundred_unclosed_paragraphs_stay_flat(tmp_path: Path) -> None:
    body = "".join(f"<p><span>Para {i}</span>" for i in range(200))
    conversation, _ = _probe(tmp_path, body)
    assert conversation.messages[0].text == "\n\n".join(f"Para {i}" for i in range(200))


def test_tables_are_counted_per_page(tmp_path: Path) -> None:
    table = "<table><tr><th>H<tr><td>v</table>"
    conversation, tracker = _probe(tmp_path, f"<p>One{table}<p>Two{table}")
    assert tracker.counters["tables"] == 2
    assert conversation.extra["tables"] == 2


def test_a_table_inside_a_cell_is_not_a_second_table(tmp_path: Path) -> None:
    body = "<table><tr><th>a<tr><td>x <table><tr><td>in1<td>in2</table> y</table><p>After"
    conversation, tracker = _probe(tmp_path, body)
    assert conversation.messages[0].text == "| a |\n| --- |\n| x in1 in2 y |\n\nAfter"
    assert tracker.counters["tables"] == 1
    assert conversation.extra["tables"] == 1


def test_quotes_nested_past_the_depth_cap_are_kept_as_text_and_closed_once(
    tmp_path: Path,
) -> None:
    body = "<blockquote>" * 40 + "<p>deep text</p>" + "</blockquote>" * 40
    conversation, _ = _probe(tmp_path, body)
    (text,) = [m.text for m in conversation.messages]
    capped = archive_read_copilot.MAX_DEPTH - 1  # the page root holds the first of the slots
    assert text == "> " * capped + "deep text"
    assert text.count("deep text") == 1
    assert text.count("> ") == capped
    assert capped < 40


def test_lists_nested_past_the_depth_cap_are_kept_as_text_and_closed_once(tmp_path: Path) -> None:
    # 40 nested lists; the ones past the cap stay inline text, so their 9 end tags must be
    # swallowed instead of closing real lists early. Text between the end tags then still belongs
    # to the deepest REAL item (one line with `deep` and `mid`), and what follows every list is
    # its own paragraph. Without the demotion record the first end tags close real lists and
    # `mid` falls into a shallower item on a line of its own.
    levels, past_cap = 40, 9
    body = (
        "<ul><li>x " * levels
        + "deep "
        + "</ul>" * past_cap
        + " mid "
        + "</ul>" * (levels - past_cap)
        + "<p>after"
    )
    conversation, _ = _probe(tmp_path, body)
    (text,) = [m.text for m in conversation.messages]
    lines = text.split("\n")
    assert sum("deep" in line for line in lines) == 1
    assert any("deep" in line and "mid" in line for line in lines)
    assert text.endswith("\n\nafter")


# --- determinism and registration of the three public functions --------------------------


def _dump(conversations: list[Conversation]) -> list[dict[str, Any]]:
    return [c.to_dict() for c in conversations]


def test_two_runs_and_a_moved_copy_give_identical_conversations(tmp_path: Path) -> None:
    root = build_copilot_folder(tmp_path / "one")
    before = sorted(p.relative_to(root) for p in root.rglob("*"))
    first, _ = _read(root)
    second, _ = _read(root)
    moved = tmp_path / "deeper" / "two"
    shutil.copytree(root, moved)
    third, _ = _read(moved)
    assert _dump(first) == _dump(second) == _dump(third)
    assert sorted(p.relative_to(root) for p in root.rglob("*")) == before, "reading wrote nothing"


def test_pages_are_read_in_sorted_relative_path_order(tmp_path: Path) -> None:
    root = tmp_path / "caps"
    ids = {
        "a-first.html": "Aa1Bb2Cc3Dd4Ee5Ff6Gg7",
        "m/second.HTM": "Hh1Ii2Jj3Kk4Ll5Mm6Nn7",
        "z-third.html": "Oo1Pp2Qq3Rr4Ss5Tt6Uu7",
        ".hidden/skipped.html": "Vv1Ww2Xx3Yy4Zz5Ab6Cd7",
    }
    for name in ("z-third.html", "m/second.HTM", ".hidden/skipped.html", "a-first.html"):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(copilot_small_page(ids[name], f"Page {name}", "Body."), encoding="utf-8")
    conversations, tracker = _read(root)
    assert [c.source_id for c in conversations] == [
        ids["a-first.html"],
        ids["m/second.HTM"],
        ids["z-third.html"],
    ]
    assert [c.provenance["source_file"] for c in conversations] == [
        "a-first.html",
        "m/second.HTM",
        "z-third.html",
    ]
    assert tracker.files_read == ["a-first.html", "m/second.HTM", "z-third.html"]
    assert tracker.counters["pages_read"] == 3
    assert archive_read_copilot.channels(root)["read"] == [PAGE_LINE.format(n=3)]


def test_an_id_with_uppercase_underscore_and_hyphen_survives_finalize_and_naming(
    tmp_path: Path,
) -> None:
    root = build_copilot_folder(tmp_path / "caps")
    (conversation,) = _read(root)[0]
    assert (conversation.source_id, len(conversation.source_id)) == (COPILOT_ID, 21)
    assert finalize_conversation(conversation) == ""
    assert conversation.source_id == COPILOT_ID
    assert "source_id_derived" not in conversation.extra
    assert not validate_name(conversation_name("copilot", conversation.uid))
    (message,) = conversation.messages
    assert message.id.startswith("message-copilot-") and message.channel == "reply"


# --- registration --------------------------------------------------------------------------


def test_copilot_is_registered_last(tmp_path: Path) -> None:
    assert list(PROVIDER_SPECS) == [
        "chatgpt",
        "claude",
        "gemini",
        "notebooklm",
        "grok",
        "perplexity",
        "copilot",
    ]
    spec = PROVIDER_SPECS["copilot"]
    assert (spec.name, spec.display, spec.default_rel) == ("copilot", "Copilot", "copilot")
    assert spec.module is archive_read_copilot
    root = build_copilot_folder(tmp_path / "caps")
    tracker = SourceTracker(root, spec.module.READER)
    assert [c.source_id for c in spec.open_reader(root, tracker, {})] == [COPILOT_ID]


def _archive_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cce")
    add_archive_parser(parser.add_subparsers(dest="group", required=True))
    return parser


@pytest.mark.parametrize(
    "argv",
    [
        ["archive", "ingest", "--export-root", "x"],
        ["archive", "build", "--export-root", "x"],
        ["archive", "search", "wick"],
        ["archive", "project", "feed", "--root", "x", "--out", "y"],
        ["archive", "project", "corpus", "--root", "x", "--out", "y"],
    ],
    ids=["ingest", "build", "search", "project-feed", "project-corpus"],
)
def test_the_archive_commands_accept_copilot_as_a_provider(argv: list[str]) -> None:
    args = _archive_parser().parse_args([*argv, "--provider", "copilot"])
    assert args.provider == ["copilot"]


def test_the_archive_commands_still_refuse_an_unknown_provider(capsys) -> None:
    with pytest.raises(SystemExit):
        _archive_parser().parse_args(
            ["archive", "ingest", "--export-root", "x", "--provider", "nonesuch"]
        )
    assert "nonesuch" in capsys.readouterr().err


def test_a_source_override_for_copilot_parses_and_resolves_outside_the_export(
    tmp_path: Path,
) -> None:
    args = _archive_parser().parse_args(
        ["archive", "ingest", "--export-root", "x", "--source", "copilot=/somewhere/else"]
    )
    assert args.source == ["copilot=/somewhere/else"]
    export = tmp_path / "export"
    assert resolve_sources(export, ["copilot"])["copilot"] == (export / "copilot").resolve()
    elsewhere = tmp_path / "elsewhere"
    assert resolve_sources(export, ["copilot"], {"copilot": elsewhere})["copilot"] == (
        elsewhere.resolve()
    )


# --- ingest ---------------------------------------------------------------------------------


def test_ingest_reads_a_folder_outside_the_export_root(archive_env, tmp_path: Path) -> None:
    folder = build_copilot_folder(tmp_path / "captures")
    assert not folder.resolve().is_relative_to(archive_env.export.resolve())
    manifest = _ingest_copilot(archive_env, folder)
    info = manifest["providers"]["copilot"]
    assert info["status"] == "ok"
    assert (info["conversations"], info["messages"], info["empty_conversations"]) == (1, 1, 0)
    assert (info["derived_ids"], info["duplicates_folded"]) == (0, 0)
    assert info["kinds"] == {"search": 1} and info["roles"] == {"assistant": 1}
    assert info["created_min"] == "" and info["last_activity_max"] == COPILOT_SAVED_ISO
    assert info["reader"] == archive_read_copilot.READER
    assert info["reader_counters"] == {
        "pages_read": 1,
        "pages_skipped_not_copilot": 1,
        "citation_pills_stripped": 4,
        "source_cards": 2,
        "tables": 1,
    }
    assert info["channels"] == archive_read_copilot.channels(folder)
    page = folder / COPILOT_PAGE_NAME
    assert info["source_files"] == {
        COPILOT_PAGE_NAME: hashlib.sha256(page.read_bytes()).hexdigest()
    }
    assert manifest["reader_versions"] == {"copilot": archive_read_copilot.READER}
    stored = list(iter_store(archive_env.paths))
    assert [(c.provider, c.source_id, c.title) for c in stored] == [
        ("copilot", COPILOT_ID, COPILOT_TITLE)
    ]
    assert stored[0].messages[0].text == GOLDEN
    assert stored[0].messages[0].meta["citations"] == GOLDEN_CITATIONS
    (row,) = load_index(archive_env.paths)
    assert row["last_activity"] == COPILOT_SAVED_ISO and row["source_file"] == COPILOT_PAGE_NAME
    assert load_manifest(archive_env.paths) == manifest


def test_ingest_finds_the_copilot_folder_of_the_export_by_default(archive_env) -> None:
    build_copilot_folder(archive_env.export / "copilot")
    manifest = ingest(
        export_root=archive_env.export,
        paths=archive_env.paths,
        authorization=archive_env.auth,
        providers=["copilot"],
    )
    assert manifest["providers"]["copilot"]["status"] == "ok"
    assert manifest["providers"]["copilot"]["conversations"] == 1


def test_ingest_calls_a_folder_with_no_copilot_page_absent(archive_env, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / "a.docx").write_bytes(b"stub")
    (other / "b.html").write_text(foreign_singlefile_page(), encoding="utf-8")
    manifest = _ingest_copilot(archive_env, other)
    assert manifest["providers"]["copilot"]["status"] == "absent"
    assert manifest["totals"]["conversations"] == 0
    missing = _ingest_copilot(archive_env, tmp_path / "missing")
    assert missing["providers"]["copilot"]["status"] == "absent"


def test_a_duplicate_page_is_folded_by_ingest(archive_env, tmp_path: Path) -> None:
    folder = tmp_path / "captures"
    other_saved = "Wed Mar 11 2026 10:00:00 GMT-0400 (Eastern Daylight Time)"
    body = [copilot_item("ai", "<p>" + S("Same words."), title="Twin Session")]
    write_copilot_page(folder / "a.html", body)
    write_copilot_page(folder / "b.html", body)
    write_copilot_page(folder / "c.html", body, saved=other_saved)
    manifest = _ingest_copilot(archive_env, folder)
    info = manifest["providers"]["copilot"]
    assert info["conversations"] == 1 and info["duplicates_folded"] == 2
    assert (info["duplicates_folded_identical"], info["duplicates_folded_differing"]) == (1, 1)
    assert sorted(info["source_files"]) == ["a.html", "b.html", "c.html"]
    (kept,) = iter_store(archive_env.paths)
    assert kept.updated == COPILOT_SAVED_ISO and kept.provenance["source_file"] == "a.html"


# --- end to end -----------------------------------------------------------------------------


def test_command_line_build_ingest_verify_feed_site_and_vault(
    archive_env, tmp_path: Path, monkeypatch, capsys
) -> None:
    folder = build_copilot_folder(tmp_path / "captures")
    code, out, err = _cce(
        monkeypatch,
        capsys,
        "archive",
        "build",
        "--project-root",
        str(archive_env.project),
        "--corpus-store-root",
        str(archive_env.store),
        "--export-root",
        str(archive_env.export),
        "--provider",
        "copilot",
        "--source",
        f"copilot={folder}",
    )
    payload = json.loads(out)
    assert code == 0, err
    assert payload["ok"] is True and payload["problems"] == []
    assert payload["stages"]["ingest"]["providers"] == {
        "copilot": {"status": "ok", "conversations": 1, "messages": 1, "error": None}
    }
    paths = archive_env.paths
    assert verify_archive(paths) == []
    assert verify_names(paths.root)["ok"]
    (conversation,) = iter_store(paths)
    uid = conversation.uid

    # search database
    hits = archive_search.search(paths, "wick", providers=["copilot"], limit=5)
    assert hits and {hit["provider"] for hit in hits} == {"copilot"}

    # the vault: one note, tagged, with the Copilot colour group in the graph view
    (note,) = paths.vault_conversations.rglob("*.md")
    text = note.read_text(encoding="utf-8")
    head = text.partition("\n---\n")[0]
    assert head.startswith(f'---\nuid: "{uid}"')
    assert "tags: [provider/copilot, kind/search, year/2026" in head
    assert "Glass Care Handbook" in text
    for chrome in COPILOT_CHROME_STRINGS:
        assert chrome not in text
    graph = json.loads((paths.vault / ".obsidian" / "graph.json").read_text(encoding="utf-8"))
    colours = {group["query"]: group["color"]["rgb"] for group in graph["colorGroups"]}
    assert colours["tag:#provider/copilot"] == 0xD6409F

    # the site: one page for the conversation and the colour in both the stylesheet and the graph
    # script (the provider colour map lives in graph-all.js; home-all.js carries no colours)
    page = paths.site_page("copilot", uid).read_text(encoding="utf-8")
    assert "Lantern Care Playbook" in page and "Copilot" in page
    for chrome in COPILOT_CHROME_STRINGS:
        assert chrome not in page
    assert "#d6409f" in (paths.site / "site-all.css").read_text(encoding="utf-8").lower()
    assert "copilot: '#d6409f'" in (paths.site / "graph-all.js").read_text(encoding="utf-8")

    # the prompt feed: a Copilot page holds no typed prompt, so the feed is empty beside a receipt
    feeds = tmp_path / "feeds"
    summary = project_feed(paths.root, feeds, ["copilot"])
    assert summary["copilot"]["rows"] == 0 and summary["copilot"]["alternate_rows"] == 0
    assert summary["copilot"]["messages_seen"] == 1
    assert summary["copilot"]["excluded"] == {"role-assistant": 1}
    assert (feeds / "feed-copilot-prompts.jsonl").read_bytes() == b""
    manifest = load_manifest(paths)
    receipt_path = feeds / paths.receipt("copilot", manifest["source_fingerprint"], "20260310").name
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["rows"] == 0 and receipt["conversations_seen"] == 1
    assert receipt["conversations_by_kind"] == {"search": 1}
    assert receipt["reader_versions"] == {"copilot": archive_read_copilot.READER}
    assert receipt["feed_sha256"] == hashlib.sha256(b"").hexdigest()
    assert verify_names(feeds)["ok"]


def test_command_line_ingest_with_a_source_override_outside_the_export(
    archive_env, tmp_path: Path, monkeypatch, capsys
) -> None:
    folder = build_copilot_folder(tmp_path / "captures")
    code, out, err = _cce(
        monkeypatch,
        capsys,
        "archive",
        "ingest",
        "--project-root",
        str(archive_env.project),
        "--corpus-store-root",
        str(archive_env.store),
        "--export-root",
        str(archive_env.export),
        "--provider",
        "copilot",
        "--source",
        f"copilot={folder}",
    )
    assert code == 0, err
    providers = json.loads(out)["stages"]["ingest"]["providers"]
    assert providers["copilot"]["status"] == "ok" and providers["copilot"]["conversations"] == 1
    assert archive_env.paths.provider_present("copilot")
