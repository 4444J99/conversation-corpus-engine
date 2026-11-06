"""`cce library zettels --format obsidian-md`: one small Wiki Standard note per idea.

Synthetic conversations and invented text only.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from conversation_corpus_engine import library_commands
from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.attributed_knowledge import ZETTEL_SCHEMA, export_zettels
from conversation_corpus_engine.cli import build_parser
from conversation_corpus_engine.corpus_store import CorpusStoreError
from conversation_corpus_engine.library_commands import handle_library, read_record
from conversation_corpus_engine.library_store import Library
from conversation_corpus_engine.library_zettel_notes import (
    FRONTMATTER_KEYS,
    build_notes,
    export_zettel_notes,
    lint_note,
    note_id,
    recover_quote,
)

REQUIRED = (
    "id",
    "type",
    "title",
    "summary",
    "created",
    "updated",
    "author",
    "authorship",
    "status",
)
TUPLE = re.compile(
    r"cce:(?P<uid>[a-z0-9-]+)@(?P<version>[0-9.]+)#(?P<mark>[ma])(?P<message>\d+):"
    r"(?P<start>\d+)-(?P<end>\d+) sha256:(?P<digest>[0-9a-f]{64})"
)
TEXTS = {
    "lantern": [
        "How should a lantern keeper log the nightly oil level?",
        "Write the level on the slate before dusk, then compare it with the dawn reading.",
        "A drop of more than two fingers overnight means a leak. Check the seal first.",
        "Thanks, the slate habit worked for the whole harbour season.",
    ],
    "orchard": [
        "Which pear tree should be pruned first in winter?",
        "Start with the oldest tree [the one by the gate](http://example.invalid/x) & its crossing limbs.",
        "Cut crossing limbs; ``` never leave a stub. ---",
        "Tidy the cuttings into a heap away from the roots.",
    ],
    "kiln": [
        "What cone should the first test firing reach?",
        "Cone 06 is safe for a first bisque firing of small test tiles.",
        "Hold the peak for ten minutes and let the kiln cool for a full day.",
        "Label every tile before it goes in.",
    ],
}


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def annotation(text: str, author: str, author_id: str | None = None, **changes: Any) -> dict:
    span = {
        "start": 0,
        "end": len(text),
        "author": author,
        "author_id": author_id,
        "evidence": ["synthetic receipt"],
        "assertion_status": "asserted",
        "profile_eligible": author == "owner",
        **changes,
    }
    return {"schema": "cce.span-attribution.v1", "text_digest": sha(text), "spans": [span]}


def make_library(tmp_path: Path) -> Library:
    library = Library.create(tmp_path / "library.sqlite")
    for name, texts in TEXTS.items():
        library.put(
            Conversation(
                provider="chatgpt",
                source_id=name,
                title=f"Talk about {name}",
                created="2026-02-01",
                messages=[
                    Message(role="user" if i % 2 == 0 else "assistant", text=t, id=f"{name}{i}")
                    for i, t in enumerate(texts)
                ],
            )
        )
    return library


def ref(library: Library, name: str, message: int, start: int = 0, end: int = 60) -> dict:
    uid = f"chatgpt-{name}"
    row = library.history(uid)[-1]
    item = library.record(uid, version=row["version"])["messages"][message]
    return {
        "uid": uid,
        "version": row["version"],
        "revision_digest": row["digest"],
        "sequence": "main",
        "message": message,
        "message_id": item["id"],
        "text_digest": sha(item["text"]),
        "start": start,
        "end": min(end, len(item["text"])),
    }


def source(library: Library, reference: dict, author: str = "agent", **extra: Any) -> dict:
    item = library.record(reference["uid"], version=reference["version"])["messages"][
        reference["message"]
    ]
    who = {"agent": "model", "owner": "rob"}.get(author)
    return {"source": reference, "attribution": annotation(item["text"], author, who), **extra}


def zettel(library: Library, number: int, sources: list[dict], **changes: Any) -> dict:
    body = f"Invented idea number {number} about keeping a careful record."
    title = changes.get("title", f"Careful records beat memory {number}")
    return {
        "id": f"idea-{number:03d}",
        "title": title,
        "body": body,
        "created": "2026-03-04",
        "review": {"status": "reviewed", "reviewer": "rev", "evidence": ["synthetic check"]},
        "relationships": [],
        "attribution": annotation(body, "agent", "model"),
        "title_attribution": annotation(title, "agent", "model"),
        "sources": sources,
        **changes,
    }


def envelope(library: Library, count: int = 5) -> dict:
    names = list(TEXTS)
    items = []
    for number in range(count):
        name = names[number % 3]
        found = [source(library, ref(library, name, 1 + number % 3))]
        relationships = (
            [{"type": "supports", "target": f"idea-{number - 1:03d}"}] if number % 2 else []
        )
        items.append(zettel(library, number, found, relationships=relationships))
    return {"schema": ZETTEL_SCHEMA, "zettels": items}


def parse(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    assert lines[0] == "---"
    close = lines.index("---", 1)
    meta: dict[str, Any] = {}
    key = ""
    for line in lines[1:close]:
        if line.startswith("  - "):
            meta[key].append(json.loads(line[4:]))
        else:
            assert re.fullmatch(r"[a-z]+:( .*)?", line), f"nested or odd frontmatter: {line!r}"
            key, _, rest = line.partition(":")
            meta[key] = [] if rest.strip() in ("", "[]") else json.loads(rest)
    return meta, "\n".join(lines[close + 1 :])


def quotes(body: str) -> list[str]:
    blocks = re.findall(r"(?m)(?:^> .*\n?)+", body)
    return [recover_quote(block.rstrip("\n")) for block in blocks]


def command(out: Path, store: Path, source_file: Path, *extra: str):
    return build_parser().parse_args(
        [
            "library",
            "zettels",
            "--store-root",
            str(store),
            "--input",
            str(source_file),
            "--format",
            "obsidian-md",
            "--out",
            str(out),
            *extra,
        ]
    )


@pytest.fixture
def world(tmp_path, monkeypatch):
    library = make_library(tmp_path)
    store = tmp_path / "store"
    store.mkdir()
    database = tmp_path / "library.sqlite"
    monkeypatch.setattr(library_commands, "open_library", lambda root: (store, Library(database)))
    payload = envelope(library)
    path = tmp_path / "ideas.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    yield library, payload, path, store, tmp_path / "wiki"
    library.close()


def run(out, store, path, capsys, *extra):
    code = handle_library(command(out, store, path, *extra))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_command_writes_one_note_per_idea(world, capsys):
    library, payload, path, store, out = world
    code, stdout, _ = run(out, store, path, capsys)
    assert code == 0
    report = json.loads(stdout)
    names = sorted(p.name for p in (out / "notes" / "zettel").glob("*.md"))
    assert names == sorted(f"{z['title']}.md" for z in payload["zettels"])
    assert report["written"] == len(payload["zettels"]) == 5 and report["unchanged"] == 0
    assert not list((out / "notes" / "zettel").glob(".*"))


def test_every_quote_matches_its_source_and_the_tuple_reopens_it(world, capsys):
    library, payload, path, store, out = world
    run(out, store, path, capsys)
    for item in payload["zettels"]:
        meta, body = parse(out / "notes" / "zettel" / f"{item['title']}.md")
        found = quotes(body)
        assert len(found) == len(meta["sources"]) == len(item["sources"])
        for entry, quote, tup in zip(item["sources"], found, meta["sources"], strict=True):
            r = entry["source"]
            text = library.record(r["uid"], version=r["version"])["messages"][r["message"]]["text"]
            assert quote.encode("utf-8") == text[r["start"] : r["end"]].encode("utf-8")
            parts = TUPLE.fullmatch(tup)
            assert parts and parts["digest"] == sha(text)
            reopened = read_record(
                library,
                parts["uid"],
                version=parts["version"],
                message=int(parts["message"]),
                offset=int(parts["start"]),
                limit=int(parts["end"]) - int(parts["start"]),
            )
            assert reopened["text"] == quote
            assert (
                f"cce library read {parts['uid']} --version {parts['version']} "
                f"--message {parts['message']} --offset {parts['start']} --limit "
                f"{int(parts['end']) - int(parts['start'])}"
            ) in body


def test_each_note_passes_the_wiki_caps_and_has_flat_required_frontmatter(world, capsys):
    library, payload, path, store, out = world
    run(out, store, path, capsys)
    for note in (out / "notes" / "zettel").glob("*.md"):
        meta, body = parse(note)
        assert note.stat().st_size <= 4 * 1024
        assert list(meta) == list(FRONTMATTER_KEYS)
        assert all(meta[key] for key in REQUIRED)
        assert re.fullmatch(r"20260304-[0-9a-f]{6}", meta["id"])
        assert meta["type"] == "zettel" and meta["status"] == "draft"
        assert meta["authorship"] == "agent" and meta["author"] == "agent"
        assert len(meta["summary"]) <= 200 and len(meta["title"]) <= 100
        assert meta["created"] == meta["updated"] == "2026-03-04"
        assert meta["sources"] and "\r" not in note.read_text(encoding="utf-8")
        assert all(len(q) <= 400 for q in quotes(body))
        assert lint_note(note.read_text(encoding="utf-8")) == {"errors": [], "warnings": []}
    linked, _ = parse(out / "notes" / "zettel" / "Careful records beat memory 1.md")
    assert linked["links"] == ["[[Careful records beat memory 0]]"]


def test_rerun_is_byte_identical_and_touches_nothing(world, capsys):
    library, payload, path, store, out = world
    run(out, store, path, capsys)
    first = snapshot(out)
    code, stdout, _ = run(out, store, path, capsys)
    assert code == 0
    assert json.loads(stdout)["written"] == 0 and json.loads(stdout)["unchanged"] == 5
    assert snapshot(out) == first
    shuffled = dict(payload, zettels=list(reversed(payload["zettels"])))
    path.write_text(json.dumps(shuffled), encoding="utf-8")
    run(out, store, path, capsys)
    assert snapshot(out) == first


def test_a_quote_that_does_not_match_is_refused_and_nothing_is_written(world, capsys):
    library, payload, path, store, out = world
    entry = payload["zettels"][2]["sources"][0]
    r = entry["source"]
    text = library.record(r["uid"], version=r["version"])["messages"][r["message"]]["text"]
    entry["quote"] = text[r["start"] : r["end"] - 1] + "?"
    assert entry["quote"] != text[r["start"] : r["end"]]
    path.write_text(json.dumps(payload), encoding="utf-8")
    code, stdout, err = run(out, store, path, capsys)
    assert code == 2 and stdout == ""
    assert "idea-002" in err and "byte for byte" in err
    assert not out.exists()
    entry["quote"] = text[r["start"] : r["end"]]
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert run(out, store, path, capsys)[0] == 0


def test_a_span_over_400_characters_or_with_control_characters_is_refused(tmp_path):
    with Library.create(tmp_path / "l.sqlite") as library:
        library.put(
            Conversation(
                provider="chatgpt",
                source_id="long",
                title="Long",
                messages=[
                    Message(role="user", text="word " * 200, id="a"),
                    Message(role="user", text="line one\r\nline two", id="b"),
                    Message(role="user", text="tab\tis fine\nand so is a newline", id="c"),
                ],
            )
        )
        long_one = zettel(library, 1, [source(library, ref(library, "long", 0, 0, 401))])
        crlf = zettel(library, 2, [source(library, ref(library, "long", 1, 0, 30))])
        fine = zettel(library, 3, [source(library, ref(library, "long", 2, 0, 40))])
        with pytest.raises(ValueError, match="at most 400"):
            build_notes(library, [long_one])
        with pytest.raises(ValueError, match="control or line-separator"):
            build_notes(library, [crlf])
        (note,) = build_notes(library, [fine])
        assert quotes(note.text) == ["tab\tis fine\nand so is a newline"]
        ok = zettel(library, 4, [source(library, ref(library, "long", 0, 0, 400))])
        assert len(quotes(build_notes(library, [ok])[0].text)[0]) == 400


def test_awkward_quote_characters_round_trip_exactly(tmp_path):
    with make_library(tmp_path) as library:
        reference = ref(library, "orchard", 1, 0, 200)
        reference2 = ref(library, "orchard", 2, 0, 200)
        note = build_notes(
            library, [zettel(library, 1, [source(library, reference), source(library, reference2)])]
        )[0]
        original = [library.record("chatgpt-orchard")["messages"][m]["text"] for m in (1, 2)]
        assert quotes(note.text) == original
        assert "](http" not in note.text.split("### Source 1", 1)[1]


def test_agent_written_drafts_are_accepted_by_the_intake_and_emitted_as_agent_draft(world, capsys):
    library, payload, path, store, out = world
    exported = export_zettels(library, payload["zettels"])
    assert {s["author"] for z in exported["zettels"] for s in z["body_attribution"]} == {"agent"}
    run(out, store, path, capsys)
    for note in (out / "notes" / "zettel").glob("*.md"):
        meta, _ = parse(note)
        assert (meta["authorship"], meta["status"]) == ("agent", "draft")
    # The intake still requires an explicit review: an unreviewed draft is refused, unchanged.
    payload["zettels"][0]["review"] = {"status": "draft", "reviewer": "rev", "evidence": ["x"]}
    with pytest.raises(ValueError, match="explicitly reviewed"):
        build_notes(library, payload["zettels"])


@pytest.mark.parametrize(
    "case",
    [
        "owner_not_eligible",
        "owner_no_evidence",
        "owner_disputed",
        "no_attribution",
        "reviewer_is_someone_else",
        "owner_text_agent_quote",
        "owner_text_unattributed_quote",
        "third_party",
    ],
)
def test_unreviewed_or_unproven_text_is_never_labelled_owner_authored(tmp_path, case):
    with make_library(tmp_path) as library:
        reference = ref(library, "kiln", 1)
        owner_source = source(library, reference, "owner")
        entry = zettel(library, 1, [owner_source])
        entry["review"]["reviewer"] = "rob"
        if case == "owner_not_eligible":
            entry["attribution"] = annotation(entry["body"], "owner", "rob", profile_eligible=False)
        elif case == "owner_no_evidence":
            entry["attribution"] = annotation(entry["body"], "owner", "rob", evidence=[])
        elif case == "owner_disputed":
            entry["attribution"] = annotation(
                entry["body"], "owner", "rob", assertion_status="disputed"
            )
        elif case == "no_attribution":
            entry["attribution"] = None
        elif case == "reviewer_is_someone_else":
            entry["attribution"] = annotation(entry["body"], "owner", "rob")
            entry["review"]["reviewer"] = "someone-else"
        elif case == "owner_text_agent_quote":
            entry["attribution"] = annotation(entry["body"], "owner", "rob")
            entry["sources"] = [source(library, reference, "agent")]
        elif case == "owner_text_unattributed_quote":
            entry["attribution"] = annotation(entry["body"], "owner", "rob")
            entry["sources"] = [{"source": reference}]
        elif case == "third_party":
            entry["attribution"] = annotation(entry["body"], "third_party", "someone")
        if case == "no_attribution":
            with pytest.raises(ValueError, match="explicit digest-bound attribution"):
                build_notes(library, [entry])
            return
        (note,) = build_notes(library, [entry])
        meta, _ = parse_text(note.text)
        assert meta["authorship"] != "human" and note.authorship != "human"
        assert meta["status"] == "draft"


def parse_text(text: str) -> tuple[dict[str, Any], str]:
    lines = text.split("\n")
    close = lines.index("---", 1)
    meta: dict[str, Any] = {}
    key = ""
    for line in lines[1:close]:
        if line.startswith("  - "):
            meta[key].append(json.loads(line[4:]))
        else:
            key, _, rest = line.partition(":")
            meta[key] = [] if rest.strip() in ("", "[]") else json.loads(rest)
    return meta, "\n".join(lines[close + 1 :])


def test_only_a_fully_asserted_reviewed_owner_text_is_credited_to_the_owner(tmp_path):
    with make_library(tmp_path) as library:
        entry = zettel(library, 1, [source(library, ref(library, "kiln", 1), "owner")])
        entry["review"]["reviewer"] = "rob"
        entry["attribution"] = annotation(entry["body"], "owner", "rob")
        entry["title_attribution"] = annotation(entry["title"], "owner", "rob")
        # Only with the configured owner id; without it nothing is human.
        (unconfigured,) = build_notes(library, [entry])
        assert unconfigured.authorship == "generated"
        (note,) = build_notes(library, [entry], owner_id="rob")
        meta, _ = parse_text(note.text)
        assert (meta["authorship"], meta["author"], meta["status"]) == ("human", "rob", "draft")


def test_ids_are_stable_and_come_from_the_created_date_and_a_hash(tmp_path):
    with make_library(tmp_path) as library:
        base = zettel(library, 1, [source(library, ref(library, "kiln", 1))])
        edited = dict(base, body="A rewritten body that must not change the id.")
        edited["attribution"] = annotation(edited["body"], "agent", "model")
        first = build_notes(library, [base])[0]
        second = build_notes(library, [edited])[0]
        assert first.id == second.id == note_id("2026-03-04", "idea-001")
        assert re.fullmatch(r"20260304-[0-9a-f]{6}", first.id)
        assert note_id("2026-03-04", "idea-002") != first.id
        assert note_id("2026-03-05", "idea-001") != first.id


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"created": ""}, "created"),
        ({"created": "2026-13-45"}, "calendar"),
        ({"title": "Why: a colon"}, "file-safe"),
        ({"title": "x" * 101}, "1 to 100"),
        ({"title": "Has [brackets]"}, "file-safe"),
    ],
)
def test_unusable_created_or_title_is_refused_with_a_reason(tmp_path, changes, reason):
    with make_library(tmp_path) as library:
        entry = zettel(library, 1, [source(library, ref(library, "kiln", 1))], **changes)
        with pytest.raises(ValueError, match=reason):
            build_notes(library, [entry])


def test_duplicate_titles_ignoring_case_are_refused(tmp_path):
    with make_library(tmp_path) as library:
        a = zettel(library, 1, [source(library, ref(library, "kiln", 1))], title="Same title")
        b = zettel(library, 2, [source(library, ref(library, "kiln", 2))], title="same TITLE")
        with pytest.raises(ValueError, match="duplicate title"):
            build_notes(library, [a, b])


def test_size_rules_warn_at_16_kib_and_refuse_at_64_kib(tmp_path):
    with make_library(tmp_path) as library:
        reference = ref(library, "kiln", 1)

        def sized(words: int) -> dict:
            body = "word " * words
            return zettel(
                library,
                1,
                [source(library, reference)],
                body=body.strip(),
                attribution=annotation(body.strip(), "agent", "model"),
            )

        (big,) = build_notes(library, [sized(4000)])
        assert any("over 16384" in w for w in big.warnings)
        with pytest.raises(ValueError, match="hard cap"):
            build_notes(library, [sized(14000)])


def test_an_existing_note_for_another_idea_is_never_overwritten(world, capsys):
    library, payload, path, store, out = world
    folder = out / "notes" / "zettel"
    folder.mkdir(parents=True)
    other = folder / "Careful records beat memory 0.md"
    other.write_text('---\nid: "20200101-aaaaaa"\n---\nMine.\n', encoding="utf-8")
    code, _, err = run(out, store, path, capsys)
    assert code == 2 and "already holds a different note" in err
    assert other.read_text(encoding="utf-8").endswith("Mine.\n")
    assert sorted(p.name for p in folder.iterdir()) == [other.name]


def test_a_changed_envelope_updates_only_the_changed_note(world, capsys):
    library, payload, path, store, out = world
    run(out, store, path, capsys)
    before = snapshot(out)
    payload["zettels"][0]["body"] = "A reviewed rewrite of the first invented idea."
    payload["zettels"][0]["attribution"] = annotation(
        payload["zettels"][0]["body"], "agent", "model"
    )
    path.write_text(json.dumps(payload), encoding="utf-8")
    report = json.loads(run(out, store, path, capsys)[1])
    after = snapshot(out)
    assert report["written"] == 1 and report["unchanged"] == 4
    changed = [name for name in after if after[name] != before[name]]
    assert changed == ["notes/zettel/Careful records beat memory 0.md"]


def test_git_visible_directory_is_refused_before_the_library_is_opened(
    tmp_path, monkeypatch, capsys
):
    work_tree = tmp_path / "work"
    work_tree.mkdir()
    subprocess.run(["git", "-C", str(work_tree), "init", "-q"], check=True, capture_output=True)

    def opened(root):
        raise AssertionError("the library was opened before the destination was authorised")

    def loaded(path):
        raise AssertionError("the envelope was read before the destination was authorised")

    monkeypatch.setattr(library_commands, "open_library", opened)
    monkeypatch.setattr(library_commands, "_load_zettel_envelope", loaded)
    code = handle_library(command(work_tree / "visible", tmp_path, tmp_path / "x.json"))
    assert code == 2 and "Git-visible" in capsys.readouterr().err
    assert sorted(p.name for p in work_tree.iterdir()) == [".git"]


def test_the_api_refuses_a_git_visible_directory_and_writes_nothing(tmp_path):
    work_tree = tmp_path / "work"
    work_tree.mkdir()
    subprocess.run(["git", "-C", str(work_tree), "init", "-q"], check=True, capture_output=True)
    with make_library(tmp_path) as library:
        payload = envelope(library)
        with pytest.raises(CorpusStoreError, match="Git-visible"):
            export_zettel_notes(
                library, payload["zettels"], work_tree / "visible", store_root=tmp_path / "store"
            )
    assert sorted(p.name for p in work_tree.iterdir()) == [".git"]


def test_top_level_authorship_and_status_in_the_envelope_are_ignored(tmp_path):
    with make_library(tmp_path) as library:
        entry = zettel(
            library,
            1,
            [source(library, ref(library, "kiln", 1))],
            authorship="human",
            status="stable",
            author="rob",
        )
        (note,) = build_notes(library, [entry])
        meta, _ = parse_text(note.text)
        assert (meta["authorship"], meta["status"], meta["author"]) == ("agent", "draft", "agent")


def test_a_directory_inside_the_library_store_is_refused(world, capsys):
    library, payload, path, store, out = world
    inside = store / "wiki"
    code, _, err = run(inside, store, path, capsys)
    assert code == 2 and "outside the library store" in err
    assert not inside.exists()
    code, _, err = run(store.parent, store, path, capsys)
    assert code == 2 and "outside the library store" in err


def test_out_goes_with_the_obsidian_format_only(world, capsys):
    library, payload, path, store, out = world
    plain = build_parser().parse_args(
        ["library", "zettels", "--store-root", str(store), "--input", str(path), "--out", str(out)]
    )
    assert handle_library(plain) == 2
    assert "--out goes with --format obsidian-md" in capsys.readouterr().err
    bare = build_parser().parse_args(
        [
            "library",
            "zettels",
            "--store-root",
            str(store),
            "--input",
            str(path),
            "--format",
            "obsidian-md",
        ]
    )
    assert handle_library(bare) == 2
    assert not out.exists()


def test_the_library_is_not_modified(world, capsys):
    library, payload, path, store, out = world
    database = store.parent / "library.sqlite"
    before = database.read_bytes()
    assert run(out, store, path, capsys)[0] == 0
    assert database.read_bytes() == before
    assert not list(store.iterdir())
    with Library(database) as reopened:
        assert reopened.verify() == []
