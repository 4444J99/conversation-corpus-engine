"""Copilot reports: the documents saved beside the Copilot pages of a capture folder.

A Copilot capture folder holds what the person saved from the web app: SingleFile `.html` pages of
a Copilot answer, which `archive_read_copilot` reads (provider `copilot`), and the research
reports Copilot produced that they kept as `.docx` or `.txt` files, which this module reads
(provider `copilotreports`). Both are Copilot's output, so both are machine turns, and between
them every file of the folder is accounted for: pages + reports + files neither reader opens.

The reports go through the library's document extraction (UTF-8 text, DOCX XML). A file it cannot
read stays a conversation with no turn and a body status, never a silent drop, so the count of
conversations equals the count of report files.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .archive_model import Conversation, SourceTracker
from .library_documents import iter_documents

PROVIDER = "copilotreports"
READER = "archive_read_captures@1"
REPORT_SUFFIXES = (".docx", ".txt")


def _reports(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in REPORT_SUFFIXES
    )


def detect(root: Path) -> bool:
    return bool(_reports(root))


def channels(root: Path) -> dict[str, list[str]]:
    reports = _reports(root)
    return {
        "read": [f"{len(reports)} saved report file(s) (.docx/.txt) — text extracted"],
        "not_read": [
            "the saved Copilot pages (.html) — read by the `copilot` provider, not this one"
        ],
    }


def _source(root: Path) -> dict[str, Any]:
    return {
        "id": "copilot-captures",
        "root": str(root),
        "include": ["**/*.docx", "**/*.txt"],
        "exclude": [],
        "wing": "ai",
        "lane": "support",
        "kind": "document",
    }


def _report(conversation: Conversation) -> Conversation:
    """A report is Copilot's output: the library marks it a project document, which is machine."""
    conversation.provider = PROVIDER
    conversation.kind = "document"
    for message in conversation.messages:
        message.role = "assistant"
        message.channel = "reply"
        message.typed_chars = 0
    return conversation


def iter_conversations(root: Path, tracker: SourceTracker) -> Iterator[Conversation]:
    counters: dict[str, Any] = {}
    for conversation in iter_documents([_source(root)], report=counters):
        tracker.note("report_files")
        tracker.provenance(Path(conversation.provenance["source_path"]))
        yield _report(conversation)
    tracker.note("report_files_unread", counters.get("counts", {}).get("metadata_only", 0))
