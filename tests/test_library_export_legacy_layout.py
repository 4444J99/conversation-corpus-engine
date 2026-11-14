"""The legacy export layout stays byte-identical while vault profiles are added beside it.

The digests below were captured from the unmodified exporter before any vault-profile change.
Every file counts, including ``agent-entry.md`` and ``notion-import.zip``; the interpreter path
that ``agent-entry.md`` embeds is pinned so the digest does not depend on the host.

One deliberate recapture: the default-layout digest was taken again when the title escaper became
idempotent (``plain`` in ``library_note_text``). The earlier escaper replaced ``[`` with ``&#91;``
and then escaped the ``#`` inside that entity, so a title or a metadata value with a Markdown
special rendered as literal entity text (``metadata&&#35;95;only``). The fixture below has two
such values; ``test_special_characters_are_escaped_once_in_the_legacy_layout`` pins the exact
new bytes. Every differing line is a doubled escape (``&&#NN;``) becoming a single one
(``&#NN;``), in the two record notes, the seven hub pages that list them and the Notion ZIP that
embeds the same notes; no other byte of the layout changed.
"""

from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest
from test_library_export_zettels import acceptance_envelope
from test_library_export_zettels import populate as zettel_library

from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.library_export import export_library, verify_export
from conversation_corpus_engine.library_store import Library

REPEATED = (
    "This is a shared paragraph explaining a recurring research idea, with enough precise "
    "original wording to remain a meaningful passage across independent records and source "
    "observations."
)
DEFAULT_TREE_DIGEST = "824763f4861ff8ae3ead6708d567412ef63a3100c7a293f0aa083c3c863fc437"
ZETTEL_TREE_DIGEST = "954bb334bc1ea9f8e7798b0330fe420b36e89aac7fbfd68de379c5663c796536"


def tree_digest(destination: Path) -> str:
    outer = hashlib.sha256()
    for path in sorted(destination.rglob("*")):
        if path.is_file():
            outer.update(path.relative_to(destination).as_posix().encode())
            outer.update(b"\0")
            outer.update(hashlib.sha256(path.read_bytes()).digest())
    return outer.hexdigest()


def layout_library(tmp_path: Path) -> Library:
    library = Library.create(tmp_path / "layout.sqlite")
    library.put(
        Conversation(
            provider="chatgpt",
            source_id="one",
            title="A shared title",
            created="2026-02-01",
            updated="2026-02-02",
            messages=[Message(role="user", text=REPEATED + "\n\nFirst unique observation.")],
            provenance={"source_path": "/srv/synthetic/source one.json"},
            extra={"wing": "ai", "lane": "conversations"},
        )
    )
    library.put(
        Conversation(
            provider="documents",
            source_id="two",
            title="A shared title",
            kind="document",
            messages=[Message(role="user", text=REPEATED + "\n\nSecond unique observation.")],
            extra={"wing": "authored", "lane": "research"},
        )
    )
    library.put(
        Conversation(
            provider="chatgpt",
            source_id="child",
            title="<script>bad</script> [[injected]]",
            messages=[
                Message(
                    role="assistant",
                    text='<img src="https://example.invalid/pixel"> [execute](javascript:alert)',
                )
            ],
            extra={"wing": "ai", "lane": "conversations", "parent_session_id": "one"},
        )
    )
    library.put(
        Conversation(
            provider="documents",
            source_id="formula",
            title='=HYPERLINK("evil")',
            kind="plan",
            extra={"wing": "ai", "lane": "support", "body_status": "metadata_only"},
        )
    )
    library.index_blocks()
    return library


@pytest.fixture(autouse=True)
def pinned_interpreter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "executable", str(tmp_path / "pinned-python"))


def test_default_export_is_byte_identical_to_the_legacy_layout(tmp_path: Path) -> None:
    with layout_library(tmp_path) as library:
        destination = tmp_path / "export"
        result = export_library(library, destination)
        assert result["vault"] == "vault/Home.md"
        assert tree_digest(destination) == DEFAULT_TREE_DIGEST
        assert verify_export(destination) == []
        manifest = json.loads((destination / "export-manifest.json").read_text())
        assert manifest["schema"] == "cce.library-export.v1"
        assert "profile" not in manifest
        assert (destination / "vault" / "blocks").is_dir()


def test_special_characters_are_escaped_once_in_the_legacy_layout(tmp_path: Path) -> None:
    with layout_library(tmp_path) as library:
        destination = tmp_path / "export"
        export_library(library, destination)
        child = (destination / "vault" / "records" / "chatgpt-child.md").read_text()
        formula = (destination / "vault" / "records" / "documents-formula.md").read_text()
        assert child.startswith("# &lt;script&gt;bad&lt;/script&gt; &#91;&#91;injected&#93;&#93;\n")
        assert "- Body coverage: metadata&#95;only\n" in formula
        for note in destination.rglob("*.md"):
            assert "&&#" not in note.read_text(), note.name


def test_default_export_with_reviewed_zettels_is_byte_identical(tmp_path: Path) -> None:
    with zettel_library(tmp_path) as library:
        destination = tmp_path / "export"
        export_library(library, destination, zettels=acceptance_envelope(library))
        assert tree_digest(destination) == ZETTEL_TREE_DIGEST
        assert verify_export(destination, expected_zettels=24) == []
        with zipfile.ZipFile(destination / "notion-import.zip") as archive:
            assert "vault/Home.md" in archive.namelist()
