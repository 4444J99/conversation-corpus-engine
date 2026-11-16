"""Reviewed Zettels rendered as small linked pages in the library export (synthetic data only)."""

from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path

import pytest
from markdown_it import MarkdownIt

from conversation_corpus_engine import library_commands
from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.attributed_knowledge import ZETTEL_SCHEMA
from conversation_corpus_engine.cli import build_parser
from conversation_corpus_engine.library_export import _inert_lines, export_library, verify_export
from conversation_corpus_engine.library_store import Library

PAGE_LIMIT = 16 * 1024
# Lines Markdown reads as structure: "=" or "-" underlines turn the line above into a
# heading, and runs of "-", "*" or "_" (spaces allowed) are thematic breaks.
RULE_LIKE_LINES = [
    "===",
    "---",
    "=",
    "-",
    "======",
    "------",
    "  ===  ",
    "   ---",
    "\t---",
    "- - -",
    "-- --",
    "  - - -  ",
    "= = =",
    "-=-",
    "***",
    "___",
    "* * *",
    "_ _ _",
]
FRONTMATTER_KEYS = [
    "id",
    "type",
    "title",
    "summary",
    "created",
    "updated",
    "author",
    "authorship",
    "status",
    "sources",
    "links",
    "aliases",
    "tags",
]
TUPLE = re.compile(r"cce:[a-z0-9-]+@[0-9.]+#[ma]\d+:\d+-\d+ sha256:[0-9a-f]{64}")
# Digest of every export file except the two that embed the interpreter path, captured from
# the unmodified code at the base commit for the fixture built by baseline_library().
BASELINE_TREE_DIGEST = "571e1bbccad147dc97f5a4c7ced9925f8dc396f118b0374b154329fcfb84410f"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def rule_like(line: str) -> bool:
    """True when the line holds only setext/thematic-break marker characters."""
    return bool(line.strip(" \t")) and set(line) <= set("-=*_ \t")


def annotation(text: str, author: str = "owner", author_id: str = "rob") -> dict:
    spans = (
        [
            {
                "start": 0,
                "end": len(text),
                "author": author,
                "author_id": author_id,
                "evidence": ["synthetic review receipt"],
                "assertion_status": "asserted",
                "profile_eligible": author == "owner",
            }
        ]
        if text
        else []
    )
    return {"schema": "cce.span-attribution.v1", "text_digest": sha(text), "spans": spans}


def tree_digest(destination: Path) -> str:
    skipped = {"agent-entry.md", "notion-import.zip"}
    outer = hashlib.sha256()
    for path in sorted(destination.rglob("*")):
        if path.is_file() and path.name not in skipped:
            outer.update(path.relative_to(destination).as_posix().encode())
            outer.update(b"\0")
            outer.update(hashlib.sha256(path.read_bytes()).digest())
    return outer.hexdigest()


def baseline_library(tmp_path: Path) -> Library:
    library = Library.create(tmp_path / "baseline.sqlite")
    shared = "A shared paragraph about a recurring synthetic theme, long enough to count as a passage that repeats in several records of this fixture."
    for index in range(3):
        library.put(
            Conversation(
                provider="chatgpt",
                source_id=f"base{index}",
                title=f"Baseline {index}",
                messages=[
                    Message(role="user", text=shared + f"\n\nUnique line {index}.", id="b0"),
                    Message(role="assistant", text=f"Reply {index}.", id="b1"),
                ],
                extra={"wing": "ai" if index else "authored", "lane": "conversations"},
            )
        )
    library.index_blocks()
    return library


def populate(tmp_path: Path, records: int = 6) -> Library:
    library = Library.create(tmp_path / "library.sqlite")
    for index in range(records):
        library.put(
            Conversation(
                provider="chatgpt",
                source_id=f"rec{index}",
                title=f"Record {index}",
                created="2026-01-01",
                updated="2026-01-02",
                messages=[
                    Message(
                        role="user" if j % 2 == 0 else "assistant",
                        text=f"Record {index} message {j}: " + "alpha beta gamma delta " * 8,
                        id=f"r{index}m{j}",
                        ts="2026-01-01",
                    )
                    for j in range(4)
                ],
                extra={
                    "alternate_messages": [
                        {"id": f"r{index}a{j}", "text": f"Alternate {index}.{j} " + "epsilon " * 9}
                        for j in range(2)
                    ]
                },
            )
        )
    library.index_blocks()
    return library


def reference(
    library: Library,
    uid: str,
    *,
    message: int = 0,
    start: int = 0,
    end: int = 30,
    sequence: str = "main",
    version: str | None = None,
) -> dict:
    rows = library.history(uid)
    entry = rows[-1] if version is None else next(r for r in rows if r["version"] == version)
    payload = library.record(uid, version=entry["version"])
    messages = payload["messages"] if sequence == "main" else payload["extra"]["alternate_messages"]
    item = messages[message]
    return {
        "uid": uid,
        "version": entry["version"],
        "revision_digest": entry["digest"],
        "sequence": sequence,
        "message": message,
        "message_id": item["id"],
        "text_digest": sha(item["text"]),
        "start": start,
        "end": min(end, len(item["text"])),
    }


def source(library: Library, ref: dict, author: str = "owner") -> dict:
    payload = library.record(ref["uid"], version=ref["version"])
    messages = (
        payload["messages"] if ref["sequence"] == "main" else payload["extra"]["alternate_messages"]
    )
    text = messages[ref["message"]]["text"]
    return {"source": ref, "attribution": annotation(text, author)}


def zettel(library: Library, number: int, sources: list[dict], **changes) -> dict:
    body = f"Reviewed synthetic idea number {number}."
    return {
        "id": f"idea-{number:03d}",
        "title": f"Idea {number}",
        "body": body,
        "review": {"status": "reviewed", "reviewer": "rob", "evidence": ["synthetic receipt"]},
        "relationships": [],
        "attribution": annotation(body, "agent", "model"),
        "sources": [source(library, ref) for ref in sources],
        **changes,
    }


def envelope(zettels: list[dict]) -> dict:
    return {"schema": ZETTEL_SCHEMA, "zettels": zettels}


def acceptance_envelope(library: Library, count: int = 24) -> dict:
    items = []
    for number in range(count):
        uid = f"chatgpt-rec{number % 6}"
        refs = [reference(library, uid, message=1 + number % 3, start=2, end=40)]
        if number % 5 == 0:
            refs.append(reference(library, uid, sequence="alternate", message=1))
        relationships = (
            [{"type": "supports", "target": f"idea-{number - 1:03d}"}] if number % 4 == 1 else []
        )
        if number == 3:
            relationships.append({"type": "extends", "target": "idea-outside-this-export"})
        items.append(zettel(library, number, refs, relationships=relationships))
    return envelope(items)


def page_hash(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def page(destination: Path, identity: str) -> Path:
    return destination / "vault" / "zettels" / f"{page_hash(identity)}.md"


def frontmatter(path: Path) -> tuple[dict[str, list[str]], str]:
    lines = path.read_text(encoding="utf-8").split("\n")
    assert lines[0] == "---"
    close = lines.index("---", 1)
    result: dict[str, list[str]] = {}
    current = ""
    for line in lines[1:close]:
        if re.match(r"^[a-z]+:", line):
            current, _, rest = line.partition(":")
            result[current] = [rest.strip()] if rest.strip() else []
        else:
            result[current].append(line)
    return result, "\n".join(lines[close + 1 :])


def test_omitted_option_is_byte_identical_to_base_behavior(tmp_path: Path) -> None:
    with baseline_library(tmp_path) as library:
        first = tmp_path / "first"
        second = tmp_path / "second"
        export_library(library, first)
        export_library(library, second, zettels=None)
        assert tree_digest(first) == tree_digest(second) == BASELINE_TREE_DIGEST
        assert not (first / "vault" / "zettels").exists()
        manifest = json.loads((first / "export-manifest.json").read_text())
        assert "zettels" not in manifest
        assert verify_export(first) == []


def test_acceptance_envelope_exports_one_valid_small_page_per_zettel(tmp_path: Path) -> None:
    with populate(tmp_path) as library:
        payload = acceptance_envelope(library)
        assert len(payload["zettels"]) >= 20
        assert len({z["sources"][0]["source"]["uid"] for z in payload["zettels"]}) >= 5
        destination = tmp_path / "export"
        result = export_library(library, destination, zettels=payload)
        assert result["zettels"] == 24
        pages = sorted((destination / "vault" / "zettels").glob("*.md"))
        assert len(pages) == 24
        assert {p.name for p in pages} == {f"{page_hash(z['id'])}.md" for z in payload["zettels"]}
        assert max(p.stat().st_size for p in pages) <= PAGE_LIMIT
        manifest = json.loads((destination / "export-manifest.json").read_text())
        assert manifest["zettels"] == 24
        assert verify_export(destination) == []
        assert verify_export(destination, expected_zettels=24) == []
        assert any(
            "zettel" in problem for problem in verify_export(destination, expected_zettels=23)
        )
        assert library.verify() == []


def test_page_has_wiki_standard_frontmatter_exact_tuples_and_blockquote(tmp_path: Path) -> None:
    with populate(tmp_path) as library:
        payload = acceptance_envelope(library)
        destination = tmp_path / "export"
        export_library(library, destination, zettels=payload)
        item = payload["zettels"][5]  # also cites an alternate message
        meta, body = frontmatter(page(destination, item["id"]))
        assert list(meta) == FRONTMATTER_KEYS
        assert meta["type"] == ['"zettel"']
        assert meta["id"] == [json.dumps(item["id"])]
        assert meta["status"] == ['"reviewed"']
        assert meta["created"] == ['""'] and meta["updated"] == ['""']
        assert meta["author"] == ['"agent"']
        tuples = [line.strip()[2:].strip('"') for line in meta["sources"]]
        assert len(tuples) == 2 and all(TUPLE.fullmatch(t) for t in tuples)
        main_ref = item["sources"][0]["source"]
        text = library.record(main_ref["uid"])["messages"][main_ref["message"]]["text"]
        assert tuples[0] == (
            f"cce:{main_ref['uid']}@{main_ref['version']}#m{main_ref['message']}:"
            f"{main_ref['start']}-{main_ref['end']} sha256:{sha(text)}"
        )
        assert "#a1:" in tuples[1]
        quote = text[main_ref["start"] : main_ref["end"]]
        assert f"\n> {quote}\n" in body
        assert body.count("\n> ") >= 2


def test_source_links_resolve_message_alternate_and_historical_anchors(tmp_path: Path) -> None:
    with populate(tmp_path) as library:
        uid = "chatgpt-rec1"
        old = library.history(uid)[-1]["version"]
        record = library.record(uid)
        edited = Conversation(
            provider="chatgpt",
            source_id="rec1",
            title="Record 1",
            created="2026-01-01",
            updated="2026-01-03",
            messages=[Message(role="user", text="Edited opening", id="r1m0")],
            extra={"alternate_messages": record["extra"]["alternate_messages"]},
        )
        library.put(edited)
        assert library.history(uid)[-1]["version"] != old
        current = reference(library, "chatgpt-rec0", message=2)
        alternate = reference(library, "chatgpt-rec0", sequence="alternate", message=1)
        historical = reference(library, uid, message=3, version=old)
        payload = envelope(
            [
                zettel(library, 1, [current, alternate]),
                zettel(library, 2, [historical]),
            ]
        )
        destination = tmp_path / "export"
        export_library(library, destination, zettels=payload)
        first = page(destination, "idea-001").read_text()
        assert "../records/chatgpt-rec0.md#message-3)" in first
        assert "../records/chatgpt-rec0.md#alternate-2)" in first
        second = page(destination, "idea-002").read_text()
        assert "../records/chatgpt-rec1.md#" not in second
        assert "../records/chatgpt-rec1.md)" in second
        assert "historical revision" in second.lower()
        note = (destination / "vault/records/chatgpt-rec1.md").read_text()
        assert "historical" in note.lower() and "Idea 2" in note
        assert verify_export(destination) == []


def test_record_notes_backlink_every_citing_zettel(tmp_path: Path) -> None:
    with populate(tmp_path) as library:
        payload = acceptance_envelope(library)
        destination = tmp_path / "export"
        export_library(library, destination, zettels=payload)
        for item in payload["zettels"]:
            for entry in item["sources"]:
                uid = entry["source"]["uid"]
                note = (destination / "vault/records" / f"{uid}.md").read_text()
                assert f"../zettels/{page_hash(item['id'])}.md" in note
        note = (destination / "vault/records/chatgpt-rec0.md").read_text()
        assert "## Zettels citing this record" in note


def test_zettel_hubs_are_paginated_at_one_hundred_entries(tmp_path: Path) -> None:
    with populate(tmp_path, records=1) as library:
        ref = reference(library, "chatgpt-rec0", message=1)
        payload = envelope([zettel(library, n, [ref]) for n in range(101)])
        destination = tmp_path / "export"
        export_library(library, destination, zettels=payload)
        hubs = sorted((destination / "vault" / "hubs").glob("zettels-*.md"))
        assert [h.name for h in hubs] == ["zettels-1.md", "zettels-2.md"]
        counts = [h.read_text().count("](../zettels/") for h in hubs]
        assert counts == [100, 1]
        assert "hubs/zettels-1.md" in (destination / "vault/Home.md").read_text()
        assert verify_export(destination, expected_zettels=101) == []


def test_graph_gets_zettel_nodes_and_edges(tmp_path: Path) -> None:
    with populate(tmp_path) as library:
        payload = acceptance_envelope(library)
        destination = tmp_path / "export"
        export_library(library, destination, zettels=payload)
        rows = [json.loads(line) for line in (destination / "graph.jsonl").read_text().splitlines()]
        nodes = [r for r in rows if r["type"] == "node" and r["kind"] == "zettel"]
        assert len(nodes) == 24
        assert {n["path"] for n in nodes} == {
            f"vault/zettels/{page_hash(z['id'])}.md" for z in payload["zettels"]
        }
        derived = [r for r in rows if r["type"] == "edge" and r["kind"] == "derived_from"]
        assert len(derived) == sum(len(z["sources"]) for z in payload["zettels"])
        assert {r["target"] for r in derived} <= {f"chatgpt-rec{i}" for i in range(6)}
        relations = [r for r in rows if r["type"] == "edge" and r["kind"] == "relation:supports"]
        assert relations and all(r["target"].startswith("zettel:") for r in relations)
        external = [r for r in rows if r.get("kind") == "relation:extends"]
        assert len(external) == 1
        assert verify_export(destination) == []


def test_relationships_link_inside_the_batch_and_invert_into_related_lists(tmp_path: Path) -> None:
    with populate(tmp_path) as library:
        payload = acceptance_envelope(library)
        destination = tmp_path / "export"
        export_library(library, destination, zettels=payload)
        text = page(destination, "idea-001").read_text()
        assert f"({page_hash('idea-000')}.md)" in text
        target = page(destination, "idea-000").read_text()
        assert f"{page_hash('idea-001')}.md" in target
        outside = page(destination, "idea-003").read_text()
        assert "idea-outside-this-export" in outside
        assert "not present in this export" in outside


def test_verify_fails_on_unresolved_message_anchor_and_page_count(tmp_path: Path) -> None:
    with populate(tmp_path) as library:
        payload = acceptance_envelope(library)
        destination = tmp_path / "export"
        export_library(library, destination, zettels=payload)
        note = destination / "vault/records/chatgpt-rec0.md"
        note.write_text(note.read_text().replace("## Message 2", "## Msg 2"))
        assert any("unresolved source anchor" in p for p in verify_export(destination))
    with populate(tmp_path / "again") as library:
        payload = acceptance_envelope(library)
        destination = tmp_path / "again" / "export"
        export_library(library, destination, zettels=payload)
        note = destination / "vault/records/chatgpt-rec0.md"
        note.write_text(note.read_text().replace("### Alternate 2", "### Branch 2"))
        assert any("unresolved source anchor" in p for p in verify_export(destination))
        page(destination, "idea-007").unlink()
        problems = verify_export(destination, expected_zettels=24)
        assert any("missing export file" in p for p in problems)
        assert any("zettel page count" in p for p in problems)


def test_hostile_text_never_becomes_a_live_link_or_heading(tmp_path: Path) -> None:
    with populate(tmp_path, records=1) as library:
        hostile = "[x](https://example.invalid) [[inj]] <script>bad</script> `c` ![p](https://e.invalid/p)"
        library.put(
            Conversation(
                provider="chatgpt",
                source_id="hostile",
                title="Hostile",
                messages=[
                    Message(role="user", text=hostile + "\n# Heading\n```\n[k](u)\n", id="h")
                ],
            )
        )
        ref = reference(library, "chatgpt-hostile", end=300)
        item = zettel(
            library,
            1,
            [ref],
            title=hostile,
            body=hostile + "\n\n# Not a heading\n```\n[fence](https://example.invalid)",
        )
        item["attribution"] = annotation(item["body"], "agent", "model")
        destination = tmp_path / "export"
        export_library(library, destination, zettels=envelope([item]))
        text = page(destination, item["id"]).read_text()
        assert "](https://example.invalid" not in text.replace("&#93;(", "")
        assert "<script>" not in text
        assert "\n# Not a heading" not in text
        assert "[[" not in text
        assert verify_export(destination) == []


@pytest.mark.parametrize("marker", RULE_LIKE_LINES)
def test_rule_like_lines_are_inert_whatever_surrounds_them(marker: str) -> None:
    for text in (f"Looks like text\n{marker}\nAnother", f"{marker}\nAfter", f"Before\n\n{marker}"):
        inert = _inert_lines(text)
        assert [line for line in inert.split("\n") if rule_like(line)] == [], (marker, inert)
        # Quoted the way a source quotation is, no quoted line is a marker line either.
        quoted = "\n".join("> " + line for line in inert.split("\n"))
        assert [line for line in quoted.split("\n") if rule_like(line.removeprefix("> "))] == []
        assert html.unescape(inert) == text


def test_setext_underlines_are_escaped_and_read_the_same() -> None:
    hostile = "Looks like text\n===\nAnother\n---"
    inert = _inert_lines(hostile)
    assert inert == "Looks like text\n&#61;==\nAnother\n&#45;--"
    # Reversible in meaning: a reader (or an unescape) gets the original text back.
    assert html.unescape(inert) == hostile
    assert _inert_lines("  === \n\t- - -") == "  &#61;== \n\t&#45; - -"


def test_ordinary_lines_with_hyphens_and_equals_are_left_alone() -> None:
    text = "well-known = fine\n-1\n- item\n== note\na---b\n\n  indented - text"
    assert _inert_lines(text) == text


def test_inert_lines_stay_reversible_for_hostile_mixed_text() -> None:
    hostile = "<b>x</b> & [y](z)\n***\n___\n- - -\n===\n\n---\n# h\n`c` *e* _u_ |t| !i"
    assert html.unescape(_inert_lines(hostile)) == hostile
    assert all(not rule_like(line) for line in _inert_lines(hostile).split("\n"))


def test_setext_and_break_lines_never_become_headings_or_rules_on_a_page(tmp_path: Path) -> None:
    with populate(tmp_path, records=1) as library:
        hostile = "Looks like text\n===\nAnother\n---\nThird\n- - -\n***\n___\nLast\n  ==="
        library.put(
            Conversation(
                provider="chatgpt",
                source_id="setext",
                title="Setext",
                messages=[Message(role="user", text=hostile, id="s")],
            )
        )
        ref = reference(library, "chatgpt-setext", end=300)
        item = zettel(library, 1, [ref], body=hostile)
        item["attribution"] = annotation(item["body"], "agent", "model")
        destination = tmp_path / "export"
        export_library(library, destination, zettels=envelope([item]))
        _, body = frontmatter(page(destination, item["id"]))
        lines = body.split("\n")
        # Both the body paragraph and the blockquoted source text carry the hostile lines.
        assert sum(1 for line in lines if line == "Looks like text") == 1
        assert sum(1 for line in lines if line == "> Looks like text") == 1
        for line in lines:
            assert not rule_like(line.removeprefix("> ")), line
        unescaped = html.unescape(body)
        assert unescaped.count("Looks like text\n===\nAnother\n---\nThird\n- - -\n***\n___") == 1
        assert unescaped.count("> Looks like text\n> ===\n> Another\n> ---") == 1
        # A CommonMark parser sees only the page's own headings and no thematic break.
        tokens = MarkdownIt("commonmark").parse(body)
        assert [t.type for t in tokens if t.type == "hr"] == []
        headings = [
            tokens[index + 1].content
            for index, token in enumerate(tokens)
            if token.type == "heading_open"
        ]
        assert headings == ["Idea 1", "Authorship", "Review", "Sources", "Source 1"]
        assert verify_export(destination) == []


def test_adversarial_zettel_never_exceeds_the_page_limit(tmp_path: Path) -> None:
    with populate(tmp_path, records=1) as library:
        heavy = "[" * 3000
        library.put(
            Conversation(
                provider="chatgpt",
                source_id="heavy",
                title="Heavy",
                messages=[Message(role="user", text=heavy, id=f"h{i}") for i in range(45)],
            )
        )
        refs = [reference(library, "chatgpt-heavy", message=i, end=3000) for i in range(45)]
        body = "[&<" * 7000
        item = zettel(
            library,
            1,
            refs,
            title="[" * 600,
            body=body,
            relationships=[{"type": "t" * 80, "target": "x" * 400 + str(i)} for i in range(60)],
        )
        item["attribution"] = {
            **annotation(body, "agent", "model"),
            "spans": [
                {
                    "start": i * 100,
                    "end": i * 100 + 50,
                    "author": "agent",
                    "author_id": "m" * 100,
                    "evidence": ["e" * 200],
                    "assertion_status": "asserted",
                    "profile_eligible": False,
                }
                for i in range(100)
            ],
        }
        destination = tmp_path / "export"
        export_library(library, destination, zettels=envelope([item]))
        path = page(destination, item["id"])
        assert path.stat().st_size <= PAGE_LIMIT
        text = path.read_text()
        assert "truncated" in text
        assert verify_export(destination, expected_zettels=1) == []


def test_every_caller_supplied_field_is_bounded_so_the_export_completes(tmp_path: Path) -> None:
    with populate(tmp_path, records=1) as library:
        huge = "[x" * 4000
        library.put(
            Conversation(
                provider="chatgpt",
                source_id="wide",
                title="Wide",
                messages=[
                    Message(role="r" * 4000, text="plain source text", id=f"w{i}", ts=huge)
                    for i in range(40)
                ],
            )
        )
        refs = [reference(library, "chatgpt-wide", message=i, end=10) for i in range(40)]
        item = zettel(library, 1, refs, created=huge, updated=huge)
        for entry in item["sources"]:
            entry["attribution"]["spans"] = [
                {**entry["attribution"]["spans"][0], "author_id": huge} for _ in range(1)
            ]
        body = item["body"]
        item["attribution"]["spans"] = [
            {**item["attribution"]["spans"][0], "author_id": huge, "evidence": [huge]}
        ]
        assert len(body) < 100
        destination = tmp_path / "export"
        export_library(library, destination, zettels=envelope([item]))
        assert page(destination, item["id"]).stat().st_size <= PAGE_LIMIT
        assert verify_export(destination, expected_zettels=1) == []


def test_quotes_are_cut_at_four_hundred_characters_with_a_marker(tmp_path: Path) -> None:
    with populate(tmp_path, records=1) as library:
        long_text = "word " * 400
        library.put(
            Conversation(
                provider="chatgpt",
                source_id="long",
                title="Long",
                messages=[Message(role="user", text=long_text, id="l")],
            )
        )
        ref = reference(library, "chatgpt-long", end=2000)
        item = zettel(library, 1, [ref])
        destination = tmp_path / "export"
        export_library(library, destination, zettels=envelope([item]))
        text = page(destination, item["id"]).read_text()
        quoted = [line[2:] for line in text.splitlines() if line.startswith("> ")]
        assert quoted == [long_text[:400]]
        assert "Quotation truncated: 400 of 2000 characters shown" in text
        assert verify_export(destination) == []


def test_created_and_updated_come_only_from_the_zettel(tmp_path: Path) -> None:
    with populate(tmp_path, records=1) as library:
        ref = reference(library, "chatgpt-rec0", message=1)
        dated = zettel(library, 1, [ref], created="2026-02-03", updated="2026-02-04")
        bare = zettel(library, 2, [ref])
        destination = tmp_path / "export"
        export_library(library, destination, zettels=envelope([dated, bare]))
        meta, _ = frontmatter(page(destination, "idea-001"))
        assert meta["created"] == ['"2026-02-03"'] and meta["updated"] == ['"2026-02-04"']
        meta, _ = frontmatter(page(destination, "idea-002"))
        assert meta["created"] == ['""'] and meta["updated"] == ['""']


def test_invalid_envelopes_fail_before_any_output(tmp_path: Path) -> None:
    with populate(tmp_path, records=1) as library:
        ref = reference(library, "chatgpt-rec0", message=1)
        good = zettel(library, 1, [ref])
        destination = tmp_path / "export"
        stale = {**good, "sources": [{"source": {**ref, "end": 9999}}]}
        with pytest.raises(ValueError):
            export_library(library, destination, zettels=envelope([stale]))
        with pytest.raises(ValueError, match="schema"):
            export_library(library, destination, zettels={"schema": "nope", "zettels": []})
        with pytest.raises(ValueError, match="too long"):
            export_library(library, destination, zettels=envelope([{**good, "id": "i" * 513}]))
        assert not destination.exists()


def test_cli_exports_with_zettels_and_verifies_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with populate(tmp_path) as library:
        payload = acceptance_envelope(library)
        monkeypatch.setattr(
            library_commands, "open_library", lambda store: (tmp_path, Library(library.path))
        )
        path = tmp_path / "reviewed.json"
        path.write_text(json.dumps(payload))
        destination = tmp_path / "cli-export"
        parser = build_parser()
        args = parser.parse_args(
            [
                "library",
                "export",
                "--store-root",
                str(tmp_path),
                "--zettels",
                str(path),
                "--out",
                str(destination),
            ]
        )
        assert library_commands.handle_library(args) == 0
        assert json.loads(capsys.readouterr().out)["zettels"] == 24
        args = parser.parse_args(
            ["library", "verify-export", str(destination), "--zettels", str(path), "--json"]
        )
        assert library_commands.handle_library(args) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["exported"] == report["reviewed"] == 24
        assert report["unresolved_anchors"] == 0 and report["problems"] == []
        (destination / "vault/records/chatgpt-rec2.md").write_text("tampered")
        assert library_commands.handle_library(args) == 1
        report = json.loads(capsys.readouterr().out)
        assert report["unresolved_anchors"] > 0
