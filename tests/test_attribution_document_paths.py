"""Path conventions for documents: agent folders and names, `.github`, and non-prose files.

Synthetic paths only. The conventions only ever SUBTRACT owner credit: a path that says agent or
machine is never `owner`, and a path that cannot tell stays `unknown`.
"""

from __future__ import annotations

import pytest

from conversation_corpus_engine.attribution_rules import (
    RULES_VERSION,
    AttributedSpan,
    RecordInfo,
    attribute_message,
    record_info,
)

BODY = "A plain paragraph of prose about the week."


def payload(source: str | None = None, key: str = "source_file", **changes) -> dict:
    base = {
        "provider": "documents",
        "kind": "document",
        "extra": {"wing": "authored", "lane": "writing"},
        "provenance": {key: source} if source is not None else {},
    }
    base.update(changes)
    return base


def attribute(text: str, record: RecordInfo) -> list[AttributedSpan]:
    return attribute_message(
        text, role="user", channel="written", typed_chars=len(text), meta={}, record=record
    )


def outcome(source: str | None, text: str = BODY, **kwargs) -> tuple[str, str, str]:
    """(rule, author, detail) of the single span a document at `source` is partitioned into."""
    spans = attribute(text, record_info(payload(source, **kwargs)))
    assert len(spans) == 1, spans
    return spans[0].rule, spans[0].author, spans[0].detail


def test_the_rules_version_names_the_new_path_semantics() -> None:
    assert RULES_VERSION == "cce.attribution-rules@10"


# --- agent paths: skills, intake memory, TASKS.md, AI names ------------------------------------


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("skills/example/notes.md", "path:skills"),
        ("repo/Skills/example/notes.md", "path:skills"),
        ("repo\\skills\\example\\notes.md", "path:skills"),
        ("C:\\repo\\SKILLS\\x.txt", "path:skills"),
        ("intake/memory/2026-01-05.md", "path:intake-memory"),
        ("Repo/Intake/Memory/entry.md", "path:intake-memory"),
        ("repo\\intake\\memory\\entry.md", "path:intake-memory"),
        ("TASKS.md", "path:tasks.md"),
        ("project/tasks.md", "path:tasks.md"),
        ("project\\Tasks.MD", "path:tasks.md"),
        ("drafts/Untitled-1-Claude.md", "path:ai-name"),
        ("exports/run_gemini_notes.json", "path:ai-name"),
        ("chatgpt-ideas.md", "path:ai-name"),
        ("ChatGPT.txt", "path:ai-name"),
        ("a/gpt-4-session.md", "path:ai-name"),
        ("a/GPT4 plan.md", "path:ai-name"),
        ("a/gpt-4o.md", "path:ai-name"),
        ("a/gpt.md", "path:ai-name"),
        ("a/codex_plan.md", "path:ai-name"),
        ("a/Copilot summary.md", "path:ai-name"),
        ("a/opencode.setup.md", "path:ai-name"),
        ("a/antigravity (2).md", "path:ai-name"),
        ("a\\CLAUDE-session.docx", "path:ai-name"),
    ],
)
def test_agent_path_conventions_make_a_document_agent(path: str, reason: str) -> None:
    rule, author, detail = outcome(path)
    assert (rule, author) == ("document_origin", "agent")
    assert detail == f"origin={reason}"


@pytest.mark.parametrize(
    "path",
    [
        "notes/journal.md",
        "essays/draft.docx",
        "claudette.md",
        "notes/claudette-recipes.md",
        "notes/Claudette_2.md",
        "mycopilot.md",
        "copilots.md",
        "copilotes.md",
        "a/codexes.md",
        "a/geminize.md",
        "a/gptq-notes.md",
        "a/chatgptish.md",
        "a/opencoded.md",
        "a/antigravitational.md",
        "a/mytasks.md",
        "a/tasks-2026.md",
        "a/tasks.md.txt",
        "skillset/notes.md",
        "a/skill/notes.md",
        "memory/entry.md",
        "intake/notes/memory/entry.md",
        "intake-memory/entry.md",
        "intake/x/memory/entry.md",
        "intake/memory",
        "claude-notes/draft.md",
        "gemini/draft.md",
    ],
)
def test_ordinary_documents_and_name_lookalikes_stay_owner(path: str) -> None:
    assert outcome(path) == ("note_owner", "owner", "family=documents")


def test_the_ai_name_is_matched_on_the_file_name_only_not_on_folders_or_extensions() -> None:
    assert outcome("claude-notes/draft.md")[1] == "owner"
    assert outcome("notes/draft.claude")[1] == "owner"
    assert outcome("notes/draft-claude.md")[1] == "agent"


def test_precedence_keeps_the_existing_reason_for_the_existing_conventions() -> None:
    assert outcome("skills/example/SKILL.md")[2] == "origin=path:skill.md"
    assert outcome("tools/.claude/skills/x/notes.md")[2] == "origin=path:.claude"
    assert outcome("skills/.codex/notes.md")[2] == "origin=path:.codex"
    assert outcome("skills/x/CLAUDE.md")[2] == "origin=path:claude.md"
    assert outcome("intake/memory/rules.mdc")[2] == "origin=path:rules-file"
    assert outcome("tools/.gemini/tasks.md")[2] == "origin=path:.gemini"
    assert outcome("skills/x/TASKS.md")[2] == "origin=path:skills"


def test_either_provenance_key_is_read() -> None:
    assert outcome("skills/x/notes.md", key="source_path")[2] == "origin=path:skills"
    assert outcome("x/chatgpt-1.md", key="source_path")[2] == "origin=path:ai-name"


def test_agent_paths_do_not_condemn_coding_sessions_or_chats() -> None:
    session = payload("home/.github/skills/chatgpt-tasks.md", provider="claudecode", kind="session")
    info = record_info(session)
    assert (info.agent_origin, info.unknown_origin) == ("", "")
    chat = payload("skills/x.md", provider="chatgpt", kind="chat")
    info = record_info(chat)
    assert (info.agent_origin, info.unknown_origin) == ("", "")


# --- .github ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.md",
        ".github/CONTRIBUTING.md",
        "repo/.github/ISSUE_TEMPLATE/bug.md",
        "repo\\.GitHub\\PULL_REQUEST_TEMPLATE.md",
    ],
)
def test_github_folders_are_unknown_never_owner_and_never_called_agent(path: str) -> None:
    rule, author, detail = outcome(path)
    assert (rule, author) == ("ambiguous", "unknown")
    assert detail == "origin=path:.github"
    assert record_info(payload(path)).agent_origin == ""


def test_an_agent_convention_wins_over_github() -> None:
    assert outcome(".github/copilot-instructions.md")[2] == "origin=path:copilot-instructions.md"
    assert outcome(".github/skills/x.md")[2] == "origin=path:skills"
    assert outcome(".github/chatgpt-notes.md")[2] == "origin=path:ai-name"
    marked = "Notes.\n\nCo-Authored-By: Claude <noreply@example.invalid>"
    assert outcome(".github/notes.md", text=marked)[:2] == ("document_origin", "agent")


# --- non-prose and machine files ---------------------------------------------------------------


@pytest.mark.parametrize(
    "extension",
    [
        ".ics",
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".heic",
        ".heif",
        ".tif",
        ".tiff",
        ".bmp",
        ".svg",
        ".plist",
        ".zip",
        ".gz",
        ".tar",
        ".tgz",
        ".json",
        ".jsonl",
        ".csv",
        ".xml",
        ".pdf",
    ],
)
def test_non_prose_extensions_are_unknown_never_owner(extension: str) -> None:
    rule, author, detail = outcome(f"backup/data/file{extension}")
    assert (rule, author) == ("ambiguous", "unknown")
    assert detail == f"origin=path:non-prose:{extension}"
    assert outcome(f"backup\\data\\FILE{extension.upper()}")[2] == detail


@pytest.mark.parametrize("extension", [".md", ".txt", ".docx", ".pages", ".MD", ".rtf", ""])
def test_text_document_extensions_stay_owner(extension: str) -> None:
    assert outcome(f"essays/draft{extension}") == ("note_owner", "owner", "family=documents")


@pytest.mark.parametrize(
    "path",
    [
        "docs/My Doc.pages/Data/picture-1.png",
        "docs/My Doc.pages/metadata/documentidentifier",
        "docs/My Doc.pages/Index/Document.iwa",
        "docs\\My Doc.PAGES\\metadata\\documentidentifier",
        "docs/documentidentifier",
    ],
)
def test_pages_bundle_internals_are_unknown(path: str) -> None:
    rule, author, detail = outcome(path)
    assert (rule, author) == ("ambiguous", "unknown")
    assert detail.startswith("origin=path:")


def test_a_pages_document_file_itself_stays_owner() -> None:
    assert outcome("docs/My Doc.pages")[1] == "owner"


def test_machine_data_in_an_authored_notes_family_is_not_subtracted() -> None:
    notes = payload("export/note.json", provider="applenotes", kind="note", extra={"wing": "x"})
    info = record_info(notes)
    assert info.authored_family and info.unknown_origin == ""
    assert attribute(BODY, info)[0].author == "owner"
    # Only the data formats are exempt; an image is nobody's prose even there.
    image = record_info({**notes, "provenance": {"source_file": "export/note.png"}})
    assert image.unknown_origin == "path:non-prose:.png"
    drive = record_info(payload("export/table.csv", provider="googledrive", kind="document"))
    assert drive.unknown_origin == "path:non-prose:.csv"


def test_a_note_without_a_source_file_stays_owner() -> None:
    notes = payload(None, provider="applenotes", kind="note", extra={})
    assert attribute(BODY, record_info(notes))[0].rule == "note_owner"


def test_the_whole_record_is_unknown_not_only_the_leftover() -> None:
    text = "First paragraph.\n\nSecond paragraph of the same file."
    spans = attribute(text, record_info(payload("backup/cal.ics")))
    assert [(s.rule, s.author) for s in spans] == [("ambiguous", "unknown")]
    assert (spans[0].start, spans[0].end) == (0, len(text))


def test_an_agent_marker_in_the_text_wins_over_a_non_prose_extension() -> None:
    marked = "Generated with Claude Code"
    assert outcome("backup/export.json", text=marked)[:2] == ("document_origin", "agent")
    assert outcome("backup/cal.ics", text=marked)[:2] == ("document_origin", "agent")


def test_new_checks_never_grant_owner() -> None:
    for path in (".github/a.md", "x/a.png", "x/skills/a.md", "x/gpt-4.md", "x/tasks.md"):
        assert outcome(path)[1] != "owner"
