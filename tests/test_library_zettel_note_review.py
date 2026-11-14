"""Late review regressions: exact rendered evidence, flat metadata and committed sources."""

from __future__ import annotations

import hashlib
import json
from html.parser import HTMLParser

import pytest
from markdown_it import MarkdownIt

from conversation_corpus_engine import library_zettel_notes as notes
from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.library_store import Library


def attribution(text, count=1, author="agent"):
    return {
        "schema": "cce.span-attribution.v1",
        "text_digest": hashlib.sha256(text.encode()).hexdigest(),
        "spans": [
            {
                "start": index * len(text) // count,
                "end": (index + 1) * len(text) // count,
                "author": author,
                "author_id": f"synthetic-{index}",
                "assertion_status": "asserted",
                "evidence": ["synthetic assertion"],
                "profile_eligible": False,
            }
            for index in range(count)
        ],
    }


@pytest.fixture
def world(tmp_path):
    library = Library.create(tmp_path / "library.sqlite")
    library.put(
        Conversation(
            provider="chatgpt",
            source_id="note-review",
            title="Synthetic source",
            messages=[
                Message(
                    role="assistant",
                    id="source-message",
                    text="Exact source text for synthetic notes. " * 5,
                )
            ],
        )
    )
    yield library
    library.close()


def idea(library, *, caller_id="synthetic-note", created="2026-03-04"):
    uid = "chatgpt-note-review"
    history = library.history(uid)[-1]
    message = library.record(uid, version=history["version"])["messages"][0]
    title, body = (
        "Careful synthetic source notes",
        "This agent draft keeps its exact source and separate attribution.",
    )
    return {
        "id": caller_id,
        "title": title,
        "body": body,
        "created": created,
        "review": {"status": "draft"},
        "relationships": [],
        "title_attribution": attribution(title),
        "attribution": attribution(body),
        "sources": [
            {
                "source": {
                    "uid": uid,
                    "version": history["version"],
                    "revision_digest": history["digest"],
                    "sequence": "main",
                    "message": 0,
                    "message_id": message["id"],
                    "text_digest": hashlib.sha256(message["text"].encode()).hexdigest(),
                    "start": 0,
                    "end": len(message["text"]),
                },
                "quote": message["text"],
                "attribution": attribution(message["text"], author="third_party"),
            }
        ],
    }


@pytest.mark.parametrize("field", ["created", "updated"])
@pytest.mark.parametrize(
    "value",
    [
        "2026-03-04T99:99:99",
        "2026-03-04T12:00:99Z",
        "2026-03-04T12:00:00+99:00",
        "2026-03-04garbage",
        "2026-03-04T",
        "2026-02-30T12:00:00Z",
    ],
)
def test_complete_invalid_timestamp_is_refused_before_writes(world, tmp_path, field, value):
    item = idea(world)
    item[field] = value
    destination = tmp_path / "notes"
    with pytest.raises(ValueError, match=field):
        notes.export_zettel_notes(world, [item], destination, store_root=tmp_path / "store")
    assert not destination.exists()


@pytest.mark.parametrize(
    "value",
    [
        "2026-03-04",
        "2026-03-04T12:34:56Z",
        "2026-03-04T12:34:56.123456+03:00",
        "2026-03-04 12:34:56-04:00",
    ],
)
def test_valid_date_or_timestamp_preserves_its_calendar_day(world, value):
    item = idea(world, created=value)
    item["updated"] = value
    note = notes.build_notes(world, [item])[0]
    assert note.id.startswith("20260304-")
    assert 'created: "2026-03-04"' in note.text and 'updated: "2026-03-04"' in note.text


@pytest.mark.parametrize(
    "text",
    [
        "    [example] & <safe>",
        "\t[example]",
        " \t  `literal`",
        "    ---",
        "    &#32;",
        "    [first]\n\n\t[second]",
    ],
)
def test_indented_quote_is_text_not_code_and_recovers_every_character(text):
    block = notes.quote_block(text)
    tokens = MarkdownIt("commonmark").parse(block)
    assert not any(token.type in {"code_block", "fence"} for token in tokens)
    assert not any(token.type in {"heading_open", "hr"} for token in tokens)
    paragraphs = [
        "".join("\n" if child.type == "softbreak" else child.content for child in token.children)
        for token in tokens
        if token.type == "inline"
    ]
    assert "\n\n".join(paragraphs) == text
    assert notes.recover_quote(block) == text


@pytest.mark.parametrize(
    "section,count,prefix",
    [
        ("source", 7, "- Quote attribution:"),
        ("title", 13, "- Idea title:"),
        ("body", 13, "- Idea text:"),
    ],
)
def test_every_source_title_and_body_attribution_span_is_rendered(world, section, count, prefix):
    item = idea(world)
    if section == "source":
        item["sources"][0]["attribution"] = attribution(
            item["sources"][0]["quote"], count, "third_party"
        )
    elif section == "title":
        item["title_attribution"] = attribution(item["title"], count)
    else:
        item["attribution"] = attribution(item["body"], count)
    note = notes.build_notes(world, [item])[0]
    assert sum(line.startswith(prefix) for line in note.text.splitlines()) == count
    assert notes.lint_note(note.text)["errors"] == []


def test_complete_attribution_over_note_budget_refuses_the_entire_export(world, tmp_path):
    item = idea(world)
    item["body"] = "Synthetic attribution bounded text. " * 100
    item["attribution"] = attribution(item["body"], 2000)
    destination = tmp_path / "notes"
    with pytest.raises(ValueError, match="byte hard cap"):
        notes.export_zettel_notes(world, [item], destination, store_root=tmp_path / "store")
    assert not destination.exists()


def changed_frontmatter(text, field, value):
    lines = text.splitlines()
    index = next(index for index, line in enumerate(lines) if line.startswith(field + ":"))
    end = index + 1
    while end < len(lines) and lines[end].startswith("  - "):
        end += 1
    lines[index:end] = [field + ": " + json.dumps(value)]
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", None),
        ("id", 42),
        ("title", None),
        ("title", []),
        ("summary", None),
        ("summary", 7),
        ("author", {}),
        ("created", None),
        ("updated", 7),
        ("authorship", {}),
        ("status", ["draft"]),
        ("sources", 7),
        ("sources", None),
        ("sources", {"nested": "value"}),
        ("links", {"nested": "value"}),
        ("links", [{"nested": "value"}]),
        ("links", [None]),
        ("aliases", [7]),
        ("aliases", [["nested"]]),
        ("tags", [False]),
        ("tags", "zettel"),
        ("created", "2026-02-30"),
    ],
)
def test_flat_frontmatter_reports_wrong_types_and_nested_values(world, field, value):
    note = notes.build_notes(world, [idea(world)])[0]
    malformed = changed_frontmatter(note.text, field, value)
    result = notes.lint_note(malformed)
    assert result["errors"], (field, value)


def test_flat_frontmatter_reports_duplicate_keys(world):
    text = notes.build_notes(world, [idea(world)])[0].text
    malformed = text.replace('type: "zettel"', 'type: "zettel"\ntype: "zettel"', 1)
    assert notes.lint_note(malformed)["errors"]


def test_uncommitted_revision_cannot_escape_the_callers_transaction(world, tmp_path):
    previous = world.history("chatgpt-note-review")[-1]
    ordinal = world.connection.execute("SELECT MAX(ordinal) FROM revisions").fetchone()[0] + 1
    world.connection.execute(
        "INSERT INTO revisions(uid,version,ordinal,digest,previous_version,change_kind) VALUES (?,?,?,?,?,?)",
        (
            "chatgpt-note-review",
            "1.0.1",
            ordinal,
            previous["digest"],
            previous["version"],
            "synthetic",
        ),
    )
    assert world.connection.in_transaction
    item = idea(world)
    assert item["sources"][0]["source"]["version"] == "1.0.1"
    destination = tmp_path / "notes"
    with pytest.raises(ValueError, match="transaction|committed"):
        notes.export_zettel_notes(world, [item], destination, store_root=tmp_path / "store")
    assert world.connection.in_transaction
    assert world.history("chatgpt-note-review")[-1]["version"] == "1.0.1"
    assert not destination.exists()
    world.connection.rollback()
    assert world.history("chatgpt-note-review")[-1]["version"] == previous["version"]


def test_explicit_committed_source_is_exportable_after_transaction_refusal(world):
    item = idea(world)
    world.connection.execute("BEGIN")
    with pytest.raises(ValueError, match="transaction|committed"):
        notes.build_notes(world, [item])
    assert world.connection.in_transaction
    world.connection.commit()
    assert notes.build_notes(world, [item])
    assert not world.connection.in_transaction


def test_short_display_collision_passes_distinct_full_identity_to_the_writer(
    world, tmp_path, monkeypatch
):
    first, second = [
        notes.build_notes(world, [idea(world, caller_id=value)])[0]
        for value in ("idea-03701", "idea-06538")
    ]
    assert first.id == second.id == "20260304-3c3861"
    assert first.filename == second.filename
    assert first.caller_digest != second.caller_digest
    assert len(first.caller_digest) == len(second.caller_digest) == 64
    plans = []

    def inspect_plan(_root, planned, *, max_entries):
        plans.append(planned)
        return {
            "entries": 1,
            "written": 0,
            "unchanged": 1,
            "recovery": {"retained": 0, "directory": "synthetic"},
        }

    monkeypatch.setattr(notes, "write_owned_notes", inspect_plan)
    notes.write_notes([first], tmp_path / "notes")
    notes.write_notes([second], tmp_path / "notes")
    assert plans[0][first.filename]["caller_digest"] == first.caller_digest
    assert plans[1][second.filename]["caller_digest"] == second.caller_digest


class QuoteRendering(HTMLParser):
    """Inspect the text a CommonMark renderer actually puts in paragraph elements."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.paragraphs = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == "p":
            self.current = []

    def handle_endtag(self, tag):
        if tag == "p" and self.current is not None:
            self.paragraphs.append("".join(self.current))
            self.current = None

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)


@pytest.mark.parametrize(
    "text",
    [
        "3. Keep original\n9. Keep number",
        "4) Keep parenthesis\n7) Keep suffix",
        "- Keep hyphen\n+ Keep plus\n* Keep star",
        "1. First number\n\n9. Separated number\n\n3) Changed marker",
        "0. Keep zero\n999999999. Keep nine digits",
        "3.\n9)\n-\n+",
        "3.\tKeep tab\n9.\tKeep later tab",
        "   3. Keep leading spaces\n\n\t+ Keep leading tab",
        "Earlier paragraph\n+ Keep continuation marker",
        "* Keep already escaped star\n- Keep later hyphen",
        "~~~\n[example]\n~~~",
        "~~~~python\nprint('[example]')\n~~~~",
        "    ~~~\n    [example]\n    ~~~",
        "~~literal tildes~~ and a lone ~",
        "٣. Unicode digit stays literal\nWord 3. and plus+ stay literal",
        "-without whitespace and +without whitespace stay literal",
        "1000000000. Ten digits stay literal",
    ],
)
def test_quote_markers_render_as_exact_text_without_lists_or_fences(text):
    block = notes.quote_block(text)
    rendered = QuoteRendering()
    rendered.feed(MarkdownIt("commonmark").render(block))
    rendered.close()
    assert set(rendered.tags) <= {"blockquote", "p"}
    assert "\n\n".join(rendered.paragraphs) == text
    assert notes.recover_quote(block) == text


def test_quote_tilde_runs_stay_literal_when_strikethrough_is_enabled():
    text = "~~literal tildes~~ and a lone ~"
    block = notes.quote_block(text)
    rendered = QuoteRendering()
    rendered.feed(MarkdownIt("commonmark").enable("strikethrough").render(block))
    rendered.close()
    assert set(rendered.tags) <= {"blockquote", "p"}
    assert rendered.paragraphs == [text]
    assert notes.recover_quote(block) == text


@pytest.mark.parametrize("control", ["\r", "\x00", "\x0b", "\x1f", "\x7f", "\u2028", "\u2029"])
def test_quote_marker_controls_are_refused_before_any_output(world, tmp_path, control):
    world.put(
        Conversation(
            provider="chatgpt",
            source_id="note-review",
            title="Synthetic source",
            messages=[
                Message(role="assistant", id="source-message", text=f"3. Keep{control}9. Later")
            ],
        )
    )
    destination = tmp_path / "notes"
    with pytest.raises(ValueError, match="control|line-separator"):
        notes.export_zettel_notes(world, [idea(world)], destination, store_root=tmp_path / "store")
    assert not destination.exists()
