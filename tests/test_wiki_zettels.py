import hashlib
import json

import pytest

from conversation_corpus_engine.cli import build_parser
from conversation_corpus_engine.wiki_zettels import (
    MANIFEST_NAME,
    emit,
    parse_note,
    split_sections,
)
from conversation_corpus_engine.wiki_zettels_cli import handle_wiki

NOTE = """---
id: 20261030-abc123
type: memory
title: "Pantry notes"
summary: "s"
created: 2026-10-30
updated: 2026-11-06
author: claude-code
authorship: agent
status: stable
tags:
  - "memory/reference"
---
# Pantry notes

Intro paragraph that is long enough to keep.

## Jars

Keep a dated inventory of the jars on the shelf.

```
## not a heading
```

## Parts

- [[pantry--part-01|part 1]]: x
"""


def make_wiki(tmp_path, note=NOTE):
    wiki = tmp_path / "wiki"
    (wiki / "notes" / "memory").mkdir(parents=True)
    (wiki / "hubs").mkdir()
    (wiki / "00-home.md").write_text("---\nid: x\ntitle: home\n---\nhome body is long enough\n")
    (wiki / "hubs" / "h.md").write_text(note)
    (wiki / "notes" / "memory" / "pantry.md").write_text(note)
    return wiki


def test_parse_note_reads_lists_and_scalars():
    meta, body = parse_note(NOTE)
    assert meta["title"] == "Pantry notes"
    assert meta["tags"] == ["memory/reference"]
    assert body.startswith("# Pantry notes")


def test_fenced_heading_does_not_split():
    sections = split_sections("## A\n\ntext\n```\n## B\n```\n")
    assert [path for path, _ in sections] == [[], ["A"]]


def test_emit_cuts_sections_with_exact_source_and_inherited_authorship(tmp_path):
    wiki, out = make_wiki(tmp_path), tmp_path / "z"
    result = emit(wiki, out)
    # Intro + Jars; the link-only Parts section, hubs and the home note are skipped.
    assert (result.notes_read, result.zettels) == (1, 2)
    files = sorted(out.glob("*.md"))
    jars = next(f for f in files if "Jars" in f.name).read_text()
    assert "authorship: agent" in jars and "authorship_basis: inherited" in jars
    assert "## not a heading" in jars
    chunk = jars.split("# Pantry notes — Jars\n\n", 1)[1].rstrip("\n")
    assert hashlib.sha256(chunk.encode()).hexdigest() in jars
    assert '"[[pantry]]"' in jars
    assert "Parts" not in "".join(f.name for f in files)


def test_idempotent_and_removes_stale_owned_only(tmp_path):
    wiki, out = make_wiki(tmp_path), tmp_path / "z"
    emit(wiki, out)
    again = emit(wiki, out)
    assert (again.written, again.unchanged, again.removed) == (0, 2, 0)
    (out / "mine.md").write_text("keep")
    (wiki / "notes" / "memory" / "pantry.md").unlink()
    gone = emit(wiki, out)
    assert (gone.zettels, gone.removed) == (0, 2)
    assert (out / "mine.md").read_text() == "keep"


def test_refuses_foreign_file_and_unsafe_out(tmp_path):
    wiki, out = make_wiki(tmp_path), tmp_path / "z"
    emit(wiki, out)
    name = next(out.glob("*Jars*")).name
    manifest = json.loads((out / MANIFEST_NAME).read_text())
    del manifest["files"][name]
    (out / MANIFEST_NAME).write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="does not own"):
        emit(wiki, out)
    with pytest.raises(ValueError, match="outside the wiki"):
        emit(wiki, wiki / "z")


def test_secret_shaped_chunk_is_skipped_not_written(tmp_path):
    leaky = NOTE.replace("Keep a dated", "ghp_" + "a" * 36 + " Keep a dated")
    wiki, out = make_wiki(tmp_path, leaky), tmp_path / "z"
    result = emit(wiki, out)
    assert result.skipped_secret == 1
    assert not any("ghp_" in f.read_text() for f in out.glob("*.md"))


def test_long_section_is_cut_below_target(tmp_path):
    paras = "\n\n".join(f"Paragraph {i} " + "word " * 120 for i in range(20))
    wiki, out = (
        make_wiki(tmp_path, NOTE.replace("Keep a dated inventory.", "") + "\n## Big\n\n" + paras),
        tmp_path / "z",
    )
    emit(wiki, out)
    big = [f for f in out.glob("*Big*")]
    assert len(big) > 1
    assert all(f.stat().st_size < 5000 for f in big)


def test_cli_registered(tmp_path, capsys):
    wiki, out = make_wiki(tmp_path), tmp_path / "z"
    args = build_parser().parse_args(["wiki", "zettels", "--wiki", str(wiki), "--out", str(out)])
    assert handle_wiki(args) == 0
    assert json.loads(capsys.readouterr().out)["zettels"] == 2
