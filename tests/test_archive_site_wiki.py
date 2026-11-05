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
from conversation_corpus_engine.archive_model import Message
from conversation_corpus_engine.archive_site import build_site, render_message_html, render_page
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


# --- (2) backlinks from the vault wikilink graph ----------------------------------------------


class _Backlinks(HTMLParser):
    """The `What links here` section of a page: its links, its hub labels and its total."""

    def __init__(self) -> None:
        super().__init__()
        self.sections = 0
        self.inside = False
        self.in_page = False
        self.hrefs: list[str] = []
        self.hubs: list[str] = []
        self.total = -1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "section" and values.get("class") == "backlinks":
            self.sections += 1
            self.inside = True
            self.total = int(values.get("data-total") or -1)
        elif self.inside and tag == "a" and self.in_page and values.get("href"):
            self.hrefs.append(str(values["href"]))
        elif self.inside and tag == "li":
            self.in_page = values.get("class") == "page"
            if values.get("class") == "hub":
                self.hubs.append(str(values.get("data-note")))

    def handle_endtag(self, tag: str) -> None:
        if tag == "li":
            self.in_page = False
        if tag == "section":
            self.inside = False


def backlinks_of(text: str) -> _Backlinks:
    parsed = _Backlinks()
    parsed.feed(text)
    return parsed


def page_uid(href: str) -> str:
    return re.sub(r"^conversation-", "", Path(href).stem)


def test_the_vault_graph_lists_who_links_to_a_conversation(built) -> None:
    incoming = importlib.import_module("conversation_corpus_engine.archive_vault").incoming_links(
        built.paths
    )
    target = "conversation-chatgpt-aaaa1111"
    assert "provider-chatgpt" in incoming[target]
    assert any(name.startswith("month-all-") for name in incoming[target])
    assert target not in incoming[target], "a note is not its own backlink"
    assert all(not name.startswith("part-") for sources in incoming.values() for name in sources)


def test_every_conversation_page_has_one_backlinks_section_with_its_related_sources(built) -> None:
    enrichment = importlib.import_module(
        "conversation_corpus_engine.archive_topics"
    ).load_enrichment(built.paths)
    expected: dict[str, set[str]] = {}
    for uid, row in enrichment.items():
        for neighbour in row.get("similar", []):
            expected.setdefault(neighbour["uid"], set()).add(uid)
    assert expected, "the fixture must hold related conversations for this to prove anything"
    for row in load_index(built.paths):
        text = built.paths.site_page(row["provider"], row["uid"]).read_text("utf-8")
        section = backlinks_of(text)
        assert section.sections == 1, row["uid"]
        listed = {page_uid(href) for href in section.hrefs}
        assert listed == expected.get(row["uid"], set()), row["uid"]
        assert row["uid"] not in listed
        assert f"provider-{row['provider']}" in section.hubs


def test_a_page_with_no_incoming_conversation_link_says_so() -> None:
    conversation = conv("chatgpt", "lonely", "Lonely")
    (page,) = render_page(conversation, related=[], topics=[], titles={})
    section = backlinks_of(page)
    assert section.sections == 1 and section.total == 0
    assert "No other page links here" in page


def test_backlinks_are_capped_and_say_how_many_more() -> None:
    site = importlib.import_module("conversation_corpus_engine.archive_site")
    wikimod = wiki()
    uids = [f"chatgpt-src{number:03d}" for number in range(60)]
    titles = {uid: f"Source {uid}" for uid in uids}
    html_text = site.backlinks_html(
        wikimod.Backlinks(hubs=("topic-all-lantern",), conversations=tuple(uids)),
        titles,
        lambda uid: f"../{uid}.html",
    )
    section = backlinks_of(html_text)
    assert section.total == 60
    assert len(section.hrefs) == wikimod.BACKLINK_LIMIT
    assert section.hubs == ["topic-all-lantern"]
    assert f"and {60 - wikimod.BACKLINK_LIMIT} more" in html_text


def _build_chatgpt(env: Any, payload: list[dict[str, Any]], limits: Limits | None = None) -> None:
    """Build every stage over an invented ChatGPT export, at the given limits."""
    root = env.export.parent / "export-chatgpt"
    (root / "chatgpt").mkdir(parents=True, exist_ok=True)
    (root / "chatgpt" / "conversations-000.json").write_text(json.dumps(payload), encoding="utf-8")
    ingest(export_root=root, paths=env.paths, authorization=env.auth)
    build_analysis(env.paths)
    build_vault(env.paths, env.auth, limits=limits)
    build_site(env.paths, env.auth, limits=limits)


def test_backlinks_fold_continuation_notes_into_their_owner(archive_env) -> None:
    small = Limits.scaled(note_bytes=6144, folder_entries=40, shard_bytes=65536)
    long_text = "\n".join(f"line {number} " + "word " * 30 for number in range(300))
    payload = [
        gpt_conversation("longone", "Long one", [("user", "ask"), ("assistant", long_text)])
    ] + [
        gpt_conversation(
            f"short{number:02d}", f"Short {number}", [("user", "q"), ("assistant", "a")]
        )
        for number in range(150)
    ]
    _build_chatgpt(archive_env, payload, small)
    incoming = importlib.import_module("conversation_corpus_engine.archive_vault").incoming_links(
        archive_env.paths
    )
    assert all(not name.startswith("part-") for sources in incoming.values() for name in sources)
    generated = json.loads(archive_env.paths.vault_manifest.read_text("utf-8"))["files"]
    assert any(Path(rel).name.startswith("part-all-") for rel in generated), "a hub spilled over"
    assert any(Path(rel).parent.name.startswith("conversation-") for rel in generated), "split"
    # the provider hub holds 151 rows at a 6 KiB cap, so it continues on part notes; a row that
    # sits only on a continuation page must still count the provider hub as a source.
    assert all("provider-chatgpt" in sources for sources in incoming.values())
    assert len(incoming) == 151
    long_page = archive_env.paths.site_page("chatgpt", "chatgpt-longone").read_text("utf-8")
    assert "conversation-chatgpt-longone" not in " ".join(backlinks_of(long_page).hrefs)


def test_verify_fails_on_a_page_without_its_backlinks_section(built) -> None:
    page = built.paths.site_page("chatgpt", "chatgpt-aaaa1111")
    page.write_text(
        page.read_text("utf-8").replace('class="backlinks"', 'class="other"'), encoding="utf-8"
    )
    problems = wiki().verify_site_wiki(built.paths)["problems"]
    assert any("chatgpt-aaaa1111" in item and "backlinks" in item for item in problems)


def test_verify_fails_when_backlinks_differ_from_the_vault_graph(built) -> None:
    enrichment = importlib.import_module(
        "conversation_corpus_engine.archive_topics"
    ).load_enrichment(built.paths)
    source, row = next((uid, r) for uid, r in enrichment.items() if r.get("similar"))
    target = row["similar"][0]["uid"]
    page = built.paths.site_page(target.partition("-")[0], target)
    text = page.read_text("utf-8")
    start = text.index('<li class="page">')
    end = text.index("</li>", start) + len("</li>")
    page.write_text(text[:start] + text[end:], encoding="utf-8")
    problems = wiki().verify_site_wiki(built.paths)["problems"]
    assert any(target in item and "backlinks" in item for item in problems), (source, problems)


def test_verify_wiki_block_counts_backlink_pages(built) -> None:
    report = wiki().verify_site_wiki(built.paths)
    assert report["problems"] == []
    assert report["backlink_pages"] == report["pages"] == 24


# --- (3) one static page per topic hub ----------------------------------------------------------


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []
        self.chips: list[tuple[str, str]] = []
        self.hub_links: list[str] = []
        self.scripts = 0
        self.titles: list[str] = []
        self._chip = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script":
            self.scripts += 1
        if tag == "a" and values.get("href"):
            self.hrefs.append(str(values["href"]))
            if values.get("class") == "topic":
                self._chip = str(values["href"])
        if tag == "li" and values.get("class") == "hub":
            self._chip = "hub"

    def handle_data(self, data: str) -> None:
        if self._chip and self._chip != "hub":
            self.chips.append((data, self._chip))
            self._chip = ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._chip = ""


def links_of(text: str) -> _Links:
    parsed = _Links()
    parsed.feed(text)
    return parsed


def vault_topic_hubs(paths: Any) -> set[str]:
    files = json.loads(paths.vault_manifest.read_text("utf-8"))["files"]
    return {
        Path(rel).stem
        for rel in files
        if Path(rel).stem.startswith("topic-all-") and rel.endswith(".md")
    }


def site_topic_pages(paths: Any) -> dict[str, Path]:
    return {
        path.stem: path
        for path in sorted(paths.site.joinpath("hubs").rglob("topic-all-*.html"))
        if paths.site.joinpath("hubs").is_dir()
    }


def test_every_topic_hub_has_exactly_one_static_topic_page(built) -> None:
    hubs = vault_topic_hubs(built.paths)
    assert hubs, "the fixture must carry topics"
    pages = site_topic_pages(built.paths)
    assert set(pages) == hubs
    enrichment = importlib.import_module(
        "conversation_corpus_engine.archive_topics"
    ).load_enrichment(built.paths)
    layout = importlib.import_module("conversation_corpus_engine.archive_layout")
    for name, path in pages.items():
        text = path.read_text("utf-8")
        parsed = links_of(text)
        assert parsed.scripts == 0
        assert "default-src &#x27;none&#x27;" in text and "script-src" not in text
        listed = {page_uid(href) for href in parsed.hrefs if "conversation-" in href}
        want = {
            uid
            for uid, row in enrichment.items()
            if any(layout.topic_note(term) == name for term in row.get("topics", []))
        }
        assert listed == want, name


def test_topic_chips_and_backlinks_link_to_the_topic_pages(built) -> None:
    layout = importlib.import_module("conversation_corpus_engine.archive_layout")
    pages = site_topic_pages(built.paths)
    enrichment = importlib.import_module(
        "conversation_corpus_engine.archive_topics"
    ).load_enrichment(built.paths)
    checked = 0
    for uid, row in enrichment.items():
        page = built.paths.site_page(uid.partition("-")[0], uid)
        parsed = links_of(page.read_text("utf-8"))
        chips = {label: href for label, href in parsed.chips}
        for term in row.get("topics", []):
            name = layout.topic_note(term)
            assert name in pages, term
            assert (page.parent / chips[term]).resolve() == pages[name].resolve()
            assert "#t=" not in chips[term]
            checked += 1
    assert checked > 5


def test_a_term_without_a_hub_name_still_filters_the_home_page() -> None:
    conversation = conv("chatgpt", "chips", "Chips")
    (page,) = render_page(
        conversation,
        related=[],
        topics=["lantern", "two words"],
        titles={},
        topic_page=lambda name: "hubs/" + name + ".html",
    )
    chips = dict(links_of(page).chips)
    assert chips["lantern"].endswith("hubs/topic-all-lantern.html")
    assert "home-all.html#t=two%20words" in chips["two words"]


def test_a_topic_page_that_outgrows_the_cap_continues_on_linked_parts(archive_env) -> None:
    small = Limits.scaled(note_bytes=6144, folder_entries=40, shard_bytes=65536)
    payload = [
        gpt_conversation(
            f"lamp{number:03d}",
            f"Lamp {number}",
            [("user", "lantern wick lantern oil"), ("assistant", "lantern lantern wick")],
        )
        for number in range(40)
    ] + [
        gpt_conversation(
            f"misc{number:03d}",
            f"Misc {number}",
            [("user", f"unrelated{number}x thing"), ("assistant", "short")],
        )
        for number in range(360)
    ]
    _build_chatgpt(archive_env, payload, small)
    pages = site_topic_pages(archive_env.paths)
    assert "topic-all-lantern" in pages
    folder = pages["topic-all-lantern"].parent
    parts = sorted(folder.glob("part-all-*.html"))
    assert parts, "forty rows at a 6 KiB cap must spill onto a continuation page"
    listed: set[str] = set()
    for path in [pages["topic-all-lantern"], *parts]:
        text = path.read_text("utf-8")
        assert len(text.encode("utf-8")) <= small.note_bytes
        listed |= {page_uid(h) for h in links_of(text).hrefs if "conversation-" in h}
    assert listed == {f"chatgpt-lamp{number:03d}" for number in range(40)}
    assert wiki().verify_site_wiki(archive_env.paths)["problems"] == []


def test_verify_fails_on_a_missing_stray_or_wrong_topic_page(built) -> None:
    pages = site_topic_pages(built.paths)
    name, path = next(iter(pages.items()))
    saved = path.read_text("utf-8")
    path.unlink()
    problems = wiki().verify_site_wiki(built.paths)["problems"]
    assert any(name in item and "topic" in item for item in problems)
    path.write_text(saved, encoding="utf-8")
    stray = path.parent / "topic-all-nothingmatches.html"
    stray.write_text(saved, encoding="utf-8")
    problems = wiki().verify_site_wiki(built.paths)["problems"]
    assert any("topic-all-nothingmatches" in item for item in problems)
    stray.unlink()
    links = links_of(saved).hrefs
    victim = next(h for h in links if "conversation-" in h)
    path.write_text(saved.replace(victim, "../../index-nope.html"), encoding="utf-8")
    problems = wiki().verify_site_wiki(built.paths)["problems"]
    assert any(name in item and "topic" in item for item in problems)


def test_verify_reports_topic_counts(built) -> None:
    report = wiki().verify_site_wiki(built.paths)
    assert report["topic_pages"] == report["topic_hubs"] == len(vault_topic_hubs(built.paths)) > 0


def test_topic_pages_keep_every_generated_name_inside_the_naming_contract(built) -> None:
    naming = importlib.import_module("conversation_corpus_engine.archive_naming")
    report = naming.verify_names(built.paths.root)
    assert report["ok"], report
    assert site_topic_pages(built.paths)


# --- (4) per-turn author-class labels -----------------------------------------------------------


class _Articles(HTMLParser):
    """The author class each message article carries (first pieces only: one per message)."""

    def __init__(self) -> None:
        super().__init__()
        self.authors: dict[str, str] = {}
        self.unlabelled = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "article":
            return
        values = dict(attrs)
        classes = str(values.get("class") or "").split()
        found = [item.removeprefix("author-") for item in classes if item.startswith("author-")]
        if len(found) != 1:
            self.unlabelled += 1
        elif re.fullmatch(r"message-\d+", str(values.get("id"))):
            self.authors[str(values["id"])] = found[0]


def authors_of(text: str) -> _Articles:
    parsed = _Articles()
    parsed.feed(text)
    return parsed


def test_a_turn_is_labelled_human_or_machine_from_its_author() -> None:
    typed = Message(role="user", text="hello", channel="typed", ts="2025-01-01T00:00:00Z")
    written = Message(role="user", text="a note", channel="written", ts="2025-01-01T00:00:00Z")
    injected = Message(role="user", text="<harness>", channel="system", ts="2025-01-01T00:00:00Z")
    reply = Message(role="assistant", text="hi", channel="reply", ts="2025-01-01T00:00:00Z")
    out = {
        name: render_message_html(m)
        for name, m in (("t", typed), ("w", written), ("i", injected), ("r", reply))
    }
    assert "author-human" in out["t"] and "User · human" in out["t"]
    assert "author-human" in out["w"]
    assert "author-machine" in out["i"] and "User · machine" in out["i"]
    assert "author-machine" in out["r"] and "Assistant · machine" in out["r"]


def test_a_collapsed_turn_keeps_its_role_first_and_names_its_author() -> None:
    message = Message(
        role="assistant",
        text="inner",
        ts="2025-01-01T00:00:00Z",
        channel="thinking",
        meta={"kind": "reasoning"},
    )
    text = render_message_html(message)
    assert "<details><summary>Assistant · reasoning · machine" in text
    assert "author-machine" in text


def test_every_message_article_on_a_built_site_carries_its_author_class(built) -> None:
    manifest = json.loads(built.paths.manifest.read_text("utf-8"))
    human = machine = 0
    for row in load_index(built.paths):
        parsed = authors_of(built.paths.site_page(row["provider"], row["uid"]).read_text("utf-8"))
        assert parsed.unlabelled == 0, row["uid"]
        human += sum(1 for item in parsed.authors.values() if item == "human")
        machine += sum(1 for item in parsed.authors.values() if item == "machine")
    assert (human, machine) == (
        manifest["totals"]["authors"]["human"],
        manifest["totals"]["authors"]["machine"],
    )
    assert human > 0 and machine > 0


def test_verify_fails_on_a_wrong_or_missing_author_label(built) -> None:
    page = built.paths.site_page("chatgpt", "chatgpt-aaaa1111")
    text = page.read_text("utf-8")
    assert "author-human" in text
    page.write_text(text.replace("author-human", "author-machine", 1), encoding="utf-8")
    problems = wiki().verify_site_wiki(built.paths)["problems"]
    assert any("author" in item for item in problems)
    page.write_text(text.replace("author-human", "other", 1), encoding="utf-8")
    problems = wiki().verify_site_wiki(built.paths)["problems"]
    assert any("author" in item and "chatgpt-aaaa1111" in item for item in problems)


def test_verify_counts_authors_against_the_manifest(built) -> None:
    report = wiki().verify_site_wiki(built.paths)
    manifest = json.loads(built.paths.manifest.read_text("utf-8"))
    assert report["authors"] == manifest["totals"]["authors"]


# --- (5) recent changes and page history from the lineage of generations ----------------------


def lineage_of(paths: Any) -> list[dict[str, Any]]:
    lineage = importlib.import_module("conversation_corpus_engine.archive_lineage")
    return lineage.load_lineage(paths)


def rebuild(env: Any) -> None:
    ingest(export_root=env.export, paths=env.paths, authorization=env.auth)
    build_analysis(env.paths)
    build_vault(env.paths, env.auth)
    build_database(env.paths, env.auth)
    build_site(env.paths, env.auth)


def edit_export(env: Any) -> dict[str, str]:
    """Change one conversation, add one and drop one; returns the uids of each."""
    path = env.export / "chatgpt" / "conversations-000.json"
    payload = json.loads(path.read_text("utf-8"))
    kept = [item for item in payload if item["conversation_id"] != "cccc3333p"]
    for item in kept:
        if item["conversation_id"] == "bbbb2222":
            item["mapping"]["bbbb2222-0"]["message"]["content"]["parts"][0] += " and then some"
    kept.append(
        gpt_conversation("dddd4444", "Fresh arrival", [("user", "brand new"), ("assistant", "ok")])
    )
    path.write_text(json.dumps(kept), encoding="utf-8")
    return {
        "added": "chatgpt-dddd4444",
        "changed": "chatgpt-bbbb2222",
        "removed": "chatgpt-cccc3333p",
    }


class _Changes(HTMLParser):
    """The rows of a recent-changes page: (uid or href, kind), and its generation anchors."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[tuple[str, str]] = []
        self.anchors: list[str] = []
        self._kind = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "tr" and values.get("id"):
            self.anchors.append(str(values["id"]))
        if tag == "li" and values.get("class") == "change":
            self._kind = str(values.get("data-change"))
            self.rows.append((str(values.get("data-uid")), self._kind))


def change_rows(env: Any) -> list[tuple[str, str]]:
    root = env.paths.site
    first = root / "report-all-changes.html"
    found: list[tuple[str, str]] = []
    for path in [first, *sorted(root.glob("part-all-*.html"))]:
        parsed = _Changes()
        parsed.feed(path.read_text("utf-8"))
        found += parsed.rows
    return found


def test_ingest_records_the_first_generation_with_every_conversation_added(built) -> None:
    records = lineage_of(built.paths)
    manifest = json.loads(built.paths.manifest.read_text("utf-8"))
    assert len(records) == 1
    (first,) = records
    assert first["generation"] == 1 and first["parent"] == ""
    assert first["fingerprint"] == manifest["source_fingerprint"]
    assert first["added"] == sorted(row["uid"] for row in load_index(built.paths))
    assert first["changed"] == [] and first["removed"] == []
    assert set(first) == {
        "generation",
        "fingerprint",
        "parent",
        "day",
        "conversations",
        "added",
        "changed",
        "removed",
    }, "no wall-clock field: a generation is named by its fingerprint and the data's own day"


def test_ingesting_an_unchanged_export_again_adds_no_generation(built) -> None:
    before = lineage_of(built.paths)
    ingest(export_root=built.export, paths=built.paths, authorization=built.auth)
    assert lineage_of(built.paths) == before


def test_a_changed_export_records_exactly_what_was_added_changed_and_removed(built) -> None:
    uids = edit_export(built)
    rebuild(built)
    first, second = lineage_of(built.paths)
    assert second["generation"] == 2 and second["parent"] == first["fingerprint"]
    assert second["added"] == [uids["added"]]
    assert second["changed"] == [uids["changed"]]
    assert second["removed"] == [uids["removed"]]
    manifest = json.loads(built.paths.manifest.read_text("utf-8"))
    assert second["fingerprint"] == manifest["source_fingerprint"] != first["fingerprint"]


def test_a_failed_or_untouched_provider_keeps_its_digests_across_a_merge(built) -> None:
    before = lineage_of(built.paths)
    ingest(
        export_root=built.export,
        paths=built.paths,
        authorization=built.auth,
        providers=["chatgpt"],
        merge=True,
    )
    assert lineage_of(built.paths) == before, "claude was not read, so it did not change"


def test_the_recent_changes_page_lists_exactly_the_latest_added_and_changed_uids(built) -> None:
    uids = edit_export(built)
    rebuild(built)
    rows = change_rows(built)
    assert sorted(rows) == sorted(
        [(uids["added"], "added"), (uids["changed"], "changed"), (uids["removed"], "removed")]
    )
    text = (built.paths.site / "report-all-changes.html").read_text("utf-8")
    assert parse(text).scripts == 0 and "default-src &#x27;none&#x27;" in text
    assert "Generation 2" in text and "Generation 1" in text
    assert wiki().verify_site_wiki(built.paths)["problems"] == []


def test_the_first_generation_lists_every_conversation_as_added(built) -> None:
    rows = change_rows(built)
    assert sorted(uid for uid, kind in rows if kind == "added") == sorted(
        row["uid"] for row in load_index(built.paths)
    )
    assert {kind for _, kind in rows} == {"added"}


def test_the_home_page_links_to_recent_changes(built) -> None:
    assert "report-all-changes.html" in built.paths.site_home.read_text("utf-8")


class _History(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sections = 0
        self.entries: list[tuple[int, str]] = []
        self.total = -1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "section" and values.get("class") == "history":
            self.sections += 1
            self.total = int(values.get("data-total") or -1)
        if tag == "li" and values.get("class") == "revision":
            self.entries.append(
                (int(str(values.get("data-generation"))), str(values.get("data-change")))
            )


def history_of(env: Any, uid: str) -> _History:
    parsed = _History()
    parsed.feed(env.paths.site_page(uid.partition("-")[0], uid).read_text("utf-8"))
    return parsed


def test_each_page_shows_its_own_history_newest_first(built) -> None:
    uids = edit_export(built)
    rebuild(built)
    changed = history_of(built, uids["changed"])
    assert changed.sections == 1 and changed.entries == [(2, "changed"), (1, "added")]
    assert history_of(built, uids["added"]).entries == [(2, "added")]
    untouched = history_of(built, "chatgpt-aaaa1111")
    assert untouched.entries == [(1, "added")] and untouched.total == 1


def test_a_page_with_no_recorded_lineage_says_so() -> None:
    (page,) = render_page(conv("chatgpt", "nohist", "No history"), related=[], topics=[], titles={})
    assert 'class="history"' in page and "No generation history recorded" in page


def test_a_generation_changes_page_that_outgrows_the_cap_continues_on_linked_parts(
    archive_env,
) -> None:
    small = Limits.scaled(note_bytes=6144, folder_entries=40, shard_bytes=65536)
    payload = [
        gpt_conversation(f"item{n:03d}", f"Item {n}", [("user", "q"), ("assistant", "a")])
        for n in range(120)
    ]
    _build_chatgpt(archive_env, payload, small)
    root = archive_env.paths.site
    parts = sorted(root.glob("part-all-*.html"))
    assert parts, "120 added conversations at a 6 KiB cap must continue on part pages"
    for path in [root / "report-all-changes.html", *parts]:
        assert len(path.read_bytes()) <= small.note_bytes
    assert len(change_rows(archive_env)) == 120
    assert wiki().verify_site_wiki(archive_env.paths)["problems"] == []


def test_verify_fails_when_the_recent_changes_page_misses_a_uid(built) -> None:
    page = built.paths.site / "report-all-changes.html"
    text = page.read_text("utf-8")
    start = text.index('<li class="change"')
    end = text.index("</li>", start) + len("</li>")
    page.write_text(text[:start] + text[end:], encoding="utf-8")
    problems = wiki().verify_site_wiki(built.paths)["problems"]
    assert any("recent changes" in item for item in problems)


def test_verify_fails_when_a_page_history_differs_from_the_lineage(built) -> None:
    page = built.paths.site_page("chatgpt", "chatgpt-aaaa1111")
    page.write_text(
        page.read_text("utf-8").replace('data-generation="1"', 'data-generation="7"'),
        encoding="utf-8",
    )
    problems = wiki().verify_site_wiki(built.paths)["problems"]
    assert any("chatgpt-aaaa1111" in item and "history" in item for item in problems)


def test_verify_fails_when_the_lineage_is_missing_or_behind_the_manifest(built) -> None:
    lineage_file = built.paths.lineage
    saved = lineage_file.read_text("utf-8")
    fingerprint = lineage_of(built.paths)[0]["fingerprint"]
    lineage_file.unlink()
    problems = wiki().verify_site_wiki(built.paths)["problems"]
    assert any("lineage" in item and "missing" in item for item in problems)
    lineage_file.write_text(saved.replace(fingerprint, "0" * 64), encoding="utf-8")
    problems = wiki().verify_site_wiki(built.paths)["problems"]
    assert any("lineage" in item and "manifest" in item for item in problems)


def test_verify_reports_the_lineage_in_its_wiki_block(built) -> None:
    edit_export(built)
    rebuild(built)
    report = wiki().verify_site_wiki(built.paths)
    assert report["problems"] == []
    assert report["generations"] == 2
    assert report["recent_changes"] == 3


def test_a_merge_on_a_store_that_predates_the_lineage_baselines_every_provider(built) -> None:
    """Stores are built one provider at a time with `--merge`; a provider that is carried over
    with no recorded digests must still be part of the baseline, not 'added' by the next merge."""
    built.paths.lineage.unlink()
    built.paths.digests.unlink()
    ingest(
        export_root=built.export,
        paths=built.paths,
        authorization=built.auth,
        providers=["chatgpt"],
        merge=True,
    )
    (first,) = lineage_of(built.paths)
    everything = sorted(row["uid"] for row in load_index(built.paths))
    assert {uid.partition("-")[0] for uid in everything} > {"chatgpt"}, "claude is carried over"
    assert first["added"] == everything and first["conversations"] == len(everything)
    ingest(
        export_root=built.export,
        paths=built.paths,
        authorization=built.auth,
        providers=["claude"],
        merge=True,
    )
    assert lineage_of(built.paths) == [first], "nothing changed, so the next merge adds nothing"
    manifest = json.loads(built.paths.manifest.read_text("utf-8"))
    digests = importlib.import_module("conversation_corpus_engine.archive_lineage").load_digests(
        built.paths
    )
    assert sum(len(rows) for rows in digests.values()) == manifest["totals"]["conversations"]


def test_verify_fails_when_the_latest_generation_miscounts_the_manifest(built) -> None:
    saved = built.paths.lineage.read_text("utf-8")
    count = lineage_of(built.paths)[0]["conversations"]
    built.paths.lineage.write_text(
        saved.replace(f'"conversations": {count}', '"conversations": 999'), encoding="utf-8"
    )
    problems = wiki().verify_site_wiki(built.paths)["problems"]
    assert any("lineage" in item and "999" in item for item in problems)
