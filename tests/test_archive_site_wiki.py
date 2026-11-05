"""The archive site as a wiki: anchors, tables of contents, backlinks, topic pages, author labels
and recent changes. Every string here is invented."""

from __future__ import annotations

import importlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from archive_support import conv, gpt_conversation

from conversation_corpus_engine.archive_database import build_database
from conversation_corpus_engine.archive_ingest import ingest, load_index
from conversation_corpus_engine.archive_limits import Limits
from conversation_corpus_engine.archive_site import build_site, render_page
from conversation_corpus_engine.archive_topics import build_analysis
from conversation_corpus_engine.archive_vault import build_vault

HEADED = (
    "# Alpha heading\nfirst paragraph\n\n## Beta heading\nsecond paragraph\n\n"
    "### Gamma heading\nthird paragraph"
)


class _Page(HTMLParser):
    """What a conversation page holds that a reader (or a link) can land on."""

    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.hrefs: list[str] = []
        self.sections: list[str] = []
        self.navs: list[str] = []
        self.scripts = 0
        self.headings: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(str(values["id"]))
        if tag == "a" and values.get("href"):
            self.hrefs.append(str(values["href"]))
        if tag == "section" and values.get("class"):
            self.sections.append(str(values["class"]))
        if tag == "nav" and values.get("class"):
            self.navs.append(str(values["class"]))
        if tag == "script":
            self.scripts += 1
        if tag in {"h3", "h4", "h5", "h6"} and values.get("id"):
            self.headings.append(str(values["id"]))


def parse(text: str) -> _Page:
    page = _Page()
    page.feed(text)
    return page


def message_ids(page: _Page) -> list[str]:
    return [item for item in page.ids if re.fullmatch(r"message-\d+(-piece-\d+)?", item)]


@pytest.fixture
def built(archive_env):
    env = archive_env
    ingest(export_root=env.export, paths=env.paths, authorization=env.auth)
    build_analysis(env.paths)
    build_vault(env.paths, env.auth)
    build_database(env.paths, env.auth)
    build_site(env.paths, env.auth)
    return env


def wiki(module: str = "archive_wiki") -> Any:
    return importlib.import_module(f"conversation_corpus_engine.{module}")


# --- (1) per-message anchors and the table of contents ----------------------------------------


def test_every_message_gets_a_stable_numbered_anchor() -> None:
    conversation = conv(
        "chatgpt",
        "anchors",
        "Anchors",
        [("user", "one"), ("assistant", "two"), ("user", "three"), ("assistant", "four")],
    )
    pages = render_page(conversation, related=[], topics=[], titles={})
    parsed = parse("\n".join(pages))
    assert message_ids(parsed) == ["message-1", "message-2", "message-3", "message-4"]
    assert len(set(parsed.ids)) == len(parsed.ids), "anchor ids are unique on a page"
    assert render_page(conversation, related=[], topics=[], titles={}) == pages


def test_a_split_message_keeps_one_anchor_per_message_and_unique_ids() -> None:
    long_text = "\n".join(f"line {number} " + "word " * 40 for number in range(400))
    conversation = conv(
        "chatgpt",
        "split",
        "Split",
        [("user", "short"), ("assistant", long_text), ("user", "tail")],
    )
    pages = render_page(conversation, related=[], topics=[], titles={}, note_bytes=8192)
    assert len(pages) > 1
    ids = [item for page in pages for item in message_ids(parse(page))]
    assert len(set(ids)) == len(ids)
    whole = [item for item in ids if re.fullmatch(r"message-\d+", item)]
    assert whole == ["message-1", "message-2", "message-3"]
    assert any(re.fullmatch(r"message-2-piece-\d+", item) for item in ids)


def test_a_table_of_contents_appears_from_three_headings() -> None:
    conversation = conv("chatgpt", "toc", "Toc", [("user", "ask"), ("assistant", HEADED)])
    (page,) = render_page(conversation, related=[], topics=[], titles={})
    parsed = parse(page)
    assert parsed.navs.count("toc") == 1
    assert len(parsed.headings) == 3
    targets = [href[1:] for href in parsed.hrefs if href.startswith("#message-2-h")]
    assert targets == parsed.headings
    for label in ("Alpha heading", "Beta heading", "Gamma heading"):
        assert page.count(label) == 2, f"{label} is in the table of contents and in the body"


def test_two_headings_do_not_make_a_table_of_contents() -> None:
    conversation = conv(
        "chatgpt", "notoc", "No toc", [("user", "ask"), ("assistant", "# One\nx\n\n## Two\ny")]
    )
    (page,) = render_page(conversation, related=[], topics=[], titles={})
    assert "toc" not in parse(page).navs


def test_a_huge_table_of_contents_is_capped_and_the_page_stays_under_the_cap() -> None:
    text = "\n\n".join(f"## Section {number} " + "x" * 50 for number in range(300))
    conversation = conv("chatgpt", "huge", "Huge", [("user", "ask"), ("assistant", text)])
    pages = render_page(conversation, related=[], topics=[], titles={})
    cap = Limits().note_bytes
    assert all(len(page.encode("utf-8")) <= cap for page in pages)
    first = parse(pages[0])
    assert "toc" in first.navs
    assert sum(item.count("Section ") for item in pages) >= 300, "no text is dropped"


def test_the_site_builds_anchors_and_tables_of_contents_into_every_page(built) -> None:
    root = built.paths.site
    total = 0
    for rows in load_index(built.paths):
        page = parse(built.paths.site_page(rows["provider"], rows["uid"]).read_text("utf-8"))
        assert len(message_ids(page)) == rows["message_count"], rows["uid"]
        total += rows["message_count"]
    assert total > 20 and root.is_dir()


def test_pages_still_ship_no_scripts_and_keep_the_page_csp(built) -> None:
    page = built.paths.site_page("chatgpt", "chatgpt-aaaa1111").read_text("utf-8")
    assert parse(page).scripts == 0
    assert "default-src &#x27;none&#x27;" in page and "script-src" not in page


# --- the wiki block of `verify` ---------------------------------------------------------------


def test_verify_reports_a_wiki_block_for_a_sound_site(built) -> None:
    report = wiki().verify_site_wiki(built.paths)
    assert report["problems"] == []
    assert report["pages"] == 24
    assert report["messages_anchored"] == report["messages"] > 0
    assert report["dangling_links"] == 0
    assert report["links_checked"] > 20


def test_verify_fails_on_a_page_missing_a_message_anchor(built) -> None:
    page = built.paths.site_page("chatgpt", "chatgpt-aaaa1111")
    page.write_text(page.read_text("utf-8").replace('id="message-2"', ""), encoding="utf-8")
    problems = wiki().verify_site_wiki(built.paths)["problems"]
    assert any("chatgpt-aaaa1111" in item and "anchor" in item for item in problems)


def test_verify_fails_on_a_dangling_fragment_and_a_missing_page(built) -> None:
    page = built.paths.site_page("chatgpt", "chatgpt-aaaa1111")
    text = page.read_text("utf-8")
    page.write_text(
        text.replace("</body>", '<a href="#message-99">x</a><a href="nope.html">y</a></body>'),
        encoding="utf-8",
    )
    problems = wiki().verify_site_wiki(built.paths)["problems"]
    assert any("#message-99" in item for item in problems)
    assert any("nope.html" in item for item in problems)


def test_verify_fails_on_a_missing_table_of_contents(archive_env) -> None:
    env = archive_env
    page_path = _build_headed_site(env)
    page_path.write_text(
        page_path.read_text("utf-8").replace('class="toc"', 'class="other"'), encoding="utf-8"
    )
    problems = wiki().verify_site_wiki(env.paths)["problems"]
    assert any("table of contents" in item for item in problems)


def _build_headed_site(env: Any) -> Path:
    """A tiny export holding one conversation with three headings, built through every stage."""
    root = env.export.parent / "export-headed"
    (root / "chatgpt").mkdir(parents=True)
    payload = [gpt_conversation("headed", "Headed", [("user", "ask"), ("assistant", HEADED)])]
    (root / "chatgpt" / "conversations-000.json").write_text(json.dumps(payload), encoding="utf-8")
    ingest(export_root=root, paths=env.paths, authorization=env.auth)
    build_analysis(env.paths)
    build_vault(env.paths, env.auth)
    build_site(env.paths, env.auth)
    return env.paths.site_page("chatgpt", "chatgpt-headed")
