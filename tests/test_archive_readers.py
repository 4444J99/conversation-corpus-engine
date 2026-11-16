from __future__ import annotations

import json
from pathlib import Path

from archive_support import gpt_conversation

from conversation_corpus_engine import (
    archive_read_chatgpt,
    archive_read_claude,
    archive_read_gemini,
    archive_read_grok,
    archive_read_notebooklm,
    archive_read_perplexity,
)
from conversation_corpus_engine.archive_model import (
    Conversation,
    SourceTracker,
    finalize_conversation,
)


def _read(module, root: Path, **kwargs) -> tuple[dict[str, Conversation], SourceTracker]:
    tracker = SourceTracker(root, module.READER)
    items = {c.source_id: c for c in module.iter_conversations(root, tracker, **kwargs)}
    return items, tracker


def test_chatgpt_linearizes_current_branch_and_hides_scaffolding(archive_env) -> None:
    root = archive_env.export / "chatgpt"
    assert archive_read_chatgpt.detect(root)
    items, tracker = _read(archive_read_chatgpt, root)
    assert list(items) == ["aaaa1111", "bbbb2222", "cccc3333p"]
    first = items["aaaa1111"]
    assert [m.role for m in first.messages] == ["user", "assistant"]
    assert "hidden scaffolding" not in "".join(m.text for m in first.messages)
    assert first.extra["hidden_messages"] == 1
    assert first.extra["off_branch_messages"] == 1
    assert first.model == "gpt-test"
    assert [a.name for a in first.messages[0].attachments] == ["diagram.png"]
    assert first.messages[0].attachments[0].ref == "file-IMG1.dat"
    assert "```py" in first.messages[1].text
    assert first.created == "2023-11-14T22:13:20Z"
    assert items["bbbb2222"].archived is True
    empty = items["cccc3333p"]
    assert empty.messages == [] and empty.collection == "g-p-proj"
    assert tracker.files_read == ["conversations-000.json"]


def test_chatgpt_ids_are_node_ids_and_off_branch_messages_are_kept_beside_the_chain(
    archive_env,
) -> None:
    items, tracker = _read(archive_read_chatgpt, archive_env.export / "chatgpt")
    first = items["aaaa1111"]
    assert [m.id for m in first.messages] == ["aaaa1111-0", "aaaa1111-1"]
    (alternate,) = first.extra["alternate_messages"]
    assert alternate["id"] == "aaaa1111-stray" and alternate["branch"] == "alternate"
    assert alternate["parent_id"] == "aaaa1111-0"
    assert (alternate["role"], alternate["channel"]) == ("assistant", "reply")
    assert alternate["text"] == "abandoned regeneration"
    assert alternate["typed_chars"] == len("abandoned regeneration")
    assert alternate["id"] not in {m.id for m in first.messages}
    assert first.extra["off_branch_messages"] == 1, "the main accounting does not move"
    assert items["bbbb2222"].extra["alternate_messages"] == []
    assert tracker.counters["alternate_messages_kept"] == 1


def test_chatgpt_alternates_skip_hidden_and_empty_nodes_but_stay_counted(tmp_path: Path) -> None:
    def node(identifier: str, parent: str, role: str, text: str, hidden: bool = False) -> dict:
        return {
            "id": identifier,
            "parent": parent,
            "children": [],
            "message": {
                "id": identifier,
                "author": {"role": role},
                "create_time": 10.0,
                "content": {"content_type": "text", "parts": [text]},
                "metadata": {"is_visually_hidden_from_conversation": hidden},
            },
        }

    mapping = {
        "root": {"id": "root", "parent": None, "children": ["a", "b", "c", "d"], "message": None},
        "a": node("a", "root", "user", "main"),
        "b": node("b", "root", "user", "kept elsewhere"),
        "c": node("c", "root", "user", "hidden elsewhere", hidden=True),
        "d": node("d", "root", "user", "   "),
    }
    raw = {"conversation_id": "x1", "current_node": "a", "mapping": mapping}
    source = tmp_path / "c.json"
    source.write_text("[]", encoding="utf-8")
    tracker = SourceTracker(tmp_path, "t")
    conversation = archive_read_chatgpt._conversation(raw, {}, tracker, source)
    assert [row["id"] for row in conversation.extra["alternate_messages"]] == ["b"]
    assert conversation.extra["off_branch_messages"] == 3
    assert [m.id for m in conversation.messages] == ["a"]


def test_chatgpt_message_accounting_reconciles_exactly(archive_env) -> None:
    _, tracker = _read(archive_read_chatgpt, archive_env.export / "chatgpt")
    counters = tracker.counters
    assert counters["message_nodes"] == 8
    assert counters["message_nodes"] == (
        counters["messages_kept"]
        + counters["messages_hidden"]
        + counters["messages_dropped_empty_or_context"]
        + counters["messages_off_branch"]
    )
    assert (counters["messages_kept"], counters["messages_hidden"]) == (4, 3)
    assert counters["messages_off_branch"] == 1


def test_chatgpt_extra_files_take_precedence_in_order(archive_env, tmp_path: Path) -> None:
    delta = tmp_path / "delta.json"
    delta.write_text(
        json.dumps([gpt_conversation("aaaa1111", "Newer title", [("user", "newer")])]),
        encoding="utf-8",
    )
    tracker = SourceTracker(archive_env.export / "chatgpt", "r")
    ordered = list(
        archive_read_chatgpt.iter_conversations(
            archive_env.export / "chatgpt", tracker, extra_files=[delta]
        )
    )
    assert ordered[0].title == "Newer title"
    assert ordered[0].provenance["source_file"] == "delta.json"


def test_chatgpt_channels_report_unread_surfaces(archive_env) -> None:
    info = archive_read_chatgpt.channels(archive_env.export / "chatgpt")
    assert info["read"] == ["1 conversation shard file(s)"]
    assert any(item.startswith("user.json") for item in info["not_read"])


def test_claude_reads_chats_projects_memory_and_design(archive_env) -> None:
    root = archive_env.export / "claude"
    items, _ = _read(archive_claude := archive_read_claude, root)
    assert archive_claude.detect(root)
    assert set(items) == {"c1c1c1c1", "c2c2c2c2", "project-p1", "memories", "design-dd"}
    chat = items["c1c1c1c1"]
    assert chat.messages[0].text == "Review kubernetes cluster costs"
    assert "PRIVATE-ATTACHMENT-TEXT" not in "".join(m.text for m in chat.messages)
    assert [a.name for a in chat.messages[0].attachments] == ["bill.pdf"]
    assert [(m.role, m.meta["kind"]) for m in chat.messages] == [
        ("user", "text"),
        ("assistant", "reasoning"),
        ("assistant", "text"),
        ("tool", "tool"),
    ]
    assert chat.messages[1].text == "hmm"
    assistant = chat.messages[2]
    assert assistant.text == "Right-size the kubernetes cluster nodes."
    assert assistant.meta["tools"] == ["calc"]
    assert [a.name for a in assistant.attachments] == ["chart.png"]
    assert chat.messages[3].text == "tool call: calc"
    assert items["c2c2c2c2"].messages[0].text.startswith("sourdough hydration")
    project = items["project-p1"]
    assert project.kind == "notebook"
    assert [m.meta["kind"] for m in project.messages] == ["prompt-template", "project-doc"]
    memory = items["memories"]
    assert memory.kind == "memory"
    assert [m.meta["kind"] for m in memory.messages] == ["summary", "project-memory", "memory-file"]
    assert items["design-dd"].messages == []


def test_claude_turns_carry_the_message_uuid_and_the_rest_are_minted_later(archive_env) -> None:
    items, _ = _read(archive_read_claude, archive_env.export / "claude")
    chat = items["c1c1c1c1"]
    assert [m.id for m in chat.messages] == ["m1", "", "m2", ""]
    finalize_conversation(chat)
    ids = [m.id for m in chat.messages]
    assert ids[0] == "m1" and ids[2] == "m2" and len(set(ids)) == 4
    assert [m.channel for m in chat.messages] == ["typed", "thinking", "reply", "tool"]
    project = items["project-p1"]
    finalize_conversation(project)
    # A project's instructions and documents are text the person put there (`written`, human);
    # a memory is something Claude wrote about the person (`memory`, machine).
    assert {m.channel for m in project.messages} == {"written"}
    assert {m.role for m in project.messages} == {"user"}
    assert {m.meta["author_basis"] for m in project.messages} == {
        "user-supplied:project-instructions",
        "user-supplied:project-document",
    }
    memory = items["memories"]
    finalize_conversation(memory)
    assert {m.channel for m in memory.messages} == {"memory"}


def test_claude_typed_characters_exclude_appended_attachment_text(archive_env) -> None:
    root = archive_env.export / "claude"
    plain, _ = _read(archive_read_claude, root)
    appended, _ = _read(archive_read_claude, root, include_attachment_text=True)
    typed = "Review kubernetes cluster costs"
    before, after = plain["c1c1c1c1"].messages[0], appended["c1c1c1c1"].messages[0]
    assert before.typed_chars == len(before.text) == len(typed)
    assert after.typed_chars == len(typed) and len(after.text) > len(typed)
    assert after.text[: after.typed_chars] == typed


def test_claude_tool_only_turns_are_preserved_not_dropped() -> None:
    call = {
        "sender": "assistant",
        "created_at": "2025-01-01T00:00:00Z",
        "text": "",
        "content": [{"type": "tool_use", "name": "search", "input": {"q": "x" * 400}}],
    }
    result = {
        "sender": "human",
        "created_at": "2025-01-01T00:00:01Z",
        "text": "",
        "content": [
            {"type": "tool_result", "content": [{"type": "text", "text": "r" * 2500}]},
            {"type": "token_budget"},
        ],
    }
    (call_messages, result_messages) = (
        archive_read_claude._entry_messages(call, False),
        archive_read_claude._entry_messages(result, False),
    )
    assert [(m.role, m.meta["kind"]) for m in call_messages] == [("tool", "tool")]
    assert call_messages[0].text.startswith("tool call: search {")
    assert "more characters" in call_messages[0].text
    assert [(m.role, m.meta["kind"]) for m in result_messages] == [("tool", "tool")]
    assert result_messages[0].text.startswith("tool result: rrr")
    assert "more characters" in result_messages[0].text
    nothing = {"sender": "human", "text": "", "content": [{"type": "token_budget"}]}
    assert archive_read_claude._entry_messages(nothing, False) == []


def test_claude_attachment_text_requires_explicit_opt_in(archive_env) -> None:
    items, _ = _read(
        archive_read_claude, archive_env.export / "claude", include_attachment_text=True
    )
    assert "PRIVATE-ATTACHMENT-TEXT" in items["c1c1c1c1"].messages[0].text


def test_perplexity_pairs_queries_with_answers(archive_env) -> None:
    root = archive_env.export / "perplexity"
    items, _ = _read(archive_read_perplexity, root)
    assert set(items) == {"pp1", "pp2"}
    assert [m.role for m in items["pp1"].messages] == ["user", "assistant"]
    assert items["pp1"].kind == "search" and items["pp1"].model == "pro"
    assert items["pp2"].title == "Untitled" and items["pp2"].messages == []
    info = archive_read_perplexity.channels(root)
    assert any("user-data" in line for line in info["not_read"])


def test_grok_follows_leaf_chain_and_reads_side_channels(archive_env) -> None:
    root = archive_env.export / "grok"
    assert archive_read_grok.detect(root)
    items, _ = _read(archive_read_grok, root)
    assert set(items) == {
        "g1",
        "g2",
        "project-w1",
        "task-t1",
        "media-2025-05",
        "media-2025-06",
    }
    chat = items["g1"]
    assert [m.text for m in chat.messages] == [
        "sourdough schedule please sourdough",
        "Bake sourdough at dawn.",
    ]
    assert chat.extra["off_branch_messages"] == 1 and chat.model == "grok-test"
    assert chat.created == "2025-05-01T10:00:00Z" and chat.messages[0].ts == "2025-05-01T10:00:00Z"
    assert [m.text for m in items["g2"].messages] == ["first", "second"]
    assert items["project-w1"].kind == "notebook"
    task = items["task-t1"]
    assert task.kind == "activity" and any("Time to feed" in m.text for m in task.messages)
    assert len(items["media-2025-06"].messages) == 1


def test_gemini_groups_activity_by_app_url_then_session_gap(archive_env) -> None:
    root = archive_env.export / "gemini"
    assert archive_read_gemini.detect(root)
    items, tracker = _read(archive_read_gemini, root)
    app = items["appone"]
    assert [m.role for m in app.messages] == ["user", "assistant"] * 3
    assert [m.meta["origin"] for m in app.messages if m.role == "user"] == [
        "typed",
        "typed",
        "branched",
    ]
    assert app.messages[4].text == "Try another approach"
    assert app.messages[1].text.startswith("Autoscaling **kubernetes** clusters.")
    assert "```\nkubectl get nodes\n```" in app.messages[1].text
    assert [a.name for a in app.messages[2].attachments] == ["shot.png"]
    sessions = [c for c in items.values() if c.extra.get("grouping") == "session-gap"]
    assert len(sessions) == 3
    assert sorted(len(c.messages) for c in sessions) == [1, 2, 4]
    canvas = next(c for c in sessions if c.messages[0].meta.get("kind") == "canvas")
    assert canvas.title == "Bread plan" and "# Bread plan" in canvas.messages[0].text
    assert tracker.counters["skipped_activity:used gemini"] == 1
    assert tracker.counters["skipped_deleted_turns"] == 1
    workspace = items["conversation_111"]
    assert [m.text for m in workspace.messages] == ["Summarize the file", "Summary here"]
    assert workspace.messages[1].meta["citations"][0]["url"] == "https://example.invalid/doc"
    ai_mode = next(c for c in items.values() if c.collection == "ai-mode")
    assert ai_mode.kind == "search" and ai_mode.messages[0].text == "kubernetes pricing"


def test_gemini_channels_exclude_unrelated_activity_products(archive_env) -> None:
    info = archive_read_gemini.channels(archive_env.export / "gemini")
    assert any("out of scope" in line for line in info["not_read"])
    assert any("Gemini Apps/MyActivity.json" in line for line in info["read"])


def test_notebooklm_collects_sources_notes_and_media_names(archive_env) -> None:
    root = archive_env.export / "gemini" / "NotebookLM"
    assert archive_read_notebooklm.detect(root)
    items, _ = _read(archive_read_notebooklm, root)
    notebook = items["nb-alpha"]
    assert notebook.kind == "notebook" and notebook.title == "Alpha Notebook"
    assert [m.meta["kind"] for m in notebook.messages] == ["source", "note", "artifact"]
    assert notebook.extra["sources"] == 1 and notebook.extra["artifacts"] == 1
    artifact = notebook.messages[2]
    assert artifact.text == "Overview · audio_overview · ready"
    assert [(a.name, a.size) for a in artifact.attachments] == [("Overview.wav", 4)]
    assert notebook.messages[0].meta["source_meta"] == {"title": "Paper", "type": "PDF"}
