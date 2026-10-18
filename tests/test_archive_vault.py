from __future__ import annotations

import json
import re
from pathlib import Path

from archive_support import conv, topical_corpus, write_store

from conversation_corpus_engine.archive_ingest import ingest
from conversation_corpus_engine.archive_model import Message, write_jsonl_atomic
from conversation_corpus_engine.archive_naming import verify_names
from conversation_corpus_engine.archive_topics import build_analysis
from conversation_corpus_engine.archive_vault import (
    RETIRED_VAULT_MANIFEST,
    build_vault,
    link_targets,
    neutralize,
    plain_title,
    render_conversation,
    verify_vault,
)

LINK_RE = re.compile(r"(?<!\\)\[\[([^\]|\\]+)(?:\\?\|[^\]]*)?\]\]")


def _vault_files(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): p.read_text(encoding="utf-8") for p in sorted(root.rglob("*.md"))
    }


def _assert_links_resolve(root: Path) -> int:
    files = _vault_files(root)
    names = {Path(rel).stem for rel in files}
    total = 0
    for rel, text in files.items():
        for target in LINK_RE.findall(text):
            total += 1
            assert target in names, f"{rel} links to missing note {target!r}"
    return total


def _full_build(env) -> dict:
    ingest(export_root=env.export, paths=env.paths, authorization=env.auth)
    build_analysis(env.paths)
    return build_vault(env.paths, env.auth)


def test_vault_from_synthetic_export_has_resolvable_links(archive_env) -> None:
    summary = _full_build(archive_env)
    root = archive_env.paths.vault
    assert summary["notes"] == 24
    assert (root / "home-all.md").is_file()
    assert (root / "meta" / "coverage-all.md").is_file()
    assert (root / "hubs" / "provider-chatgpt.md").is_file()
    assert (root / "conversations" / "conversation-chatgpt-aaaa1111.md").is_file()
    assert _assert_links_resolve(root) > 30
    graph = json.loads((root / ".obsidian" / "graph.json").read_text(encoding="utf-8"))
    assert {g["query"] for g in graph["colorGroups"]} >= {
        "tag:#provider/chatgpt",
        "tag:#provider/claude",
    }


def test_the_vault_manifest_sits_beside_the_archive_manifest_not_inside_the_vault(
    archive_env,
) -> None:
    _full_build(archive_env)
    paths = archive_env.paths
    assert paths.vault_manifest == paths.root / "manifest-all-vault.json"
    assert paths.vault_manifest.parent == paths.manifest.parent
    listed = json.loads(paths.vault_manifest.read_text(encoding="utf-8"))["files"]
    assert "home-all.md" in listed and listed == sorted(listed)
    assert not [p for p in paths.vault.iterdir() if "manifest" in p.name]
    assert verify_vault(paths) == []
    report = verify_names(paths.root)
    assert report["ok"], report["problems"]
    paths.vault_manifest.unlink()
    assert "vault manifest missing" in verify_vault(paths)[0]


def test_a_manifest_left_inside_an_old_vault_is_read_once_and_removed(archive_env) -> None:
    _full_build(archive_env)
    paths = archive_env.paths
    old_note = paths.vault_hubs / "topic-all-gone.md"
    old_note.write_text("# stale\n", encoding="utf-8")
    retired = paths.vault / RETIRED_VAULT_MANIFEST
    retired.write_text(json.dumps({"files": ["hubs/topic-all-gone.md"]}), encoding="utf-8")
    summary = build_vault(paths, archive_env.auth)
    assert not retired.exists() and not old_note.exists()
    assert summary["removed_stale"] == 1
    assert verify_vault(paths) == []


def test_vault_note_content_and_frontmatter(archive_env) -> None:
    _full_build(archive_env)
    note = (archive_env.paths.vault_conversations / "conversation-chatgpt-aaaa1111.md").read_text(
        encoding="utf-8"
    )
    head, _, body = note.partition("\n---\n")
    assert head.startswith('---\nuid: "chatgpt-aaaa1111"')
    assert 'title: "Kubernetes rollout planning"' in head
    assert "tags: [provider/chatgpt, kind/chat, year/2023" in head
    assert "source_sha256:" in head and "archived: false" in head
    assert "# Kubernetes rollout planning" in body
    assert "speech acts" in body
    assert "### User · 2023-11-14 22:14" in body
    assert "### Assistant ·" in body
    assert "*Attachments (names only):* `diagram.png`" in body
    assert "```py" in body
    home = (archive_env.paths.vault / "home-all.md").read_text(encoding="utf-8")
    assert "[[provider-chatgpt\\|ChatGPT]]" in home
    coverage = (archive_env.paths.vault / "meta" / "coverage-all.md").read_text(encoding="utf-8")
    assert "Channels not read" in coverage and "user.json" in coverage


def test_message_text_cannot_create_links_tags_or_headings() -> None:
    hostile = "see [[Secret Note]] and #tag-me and ![[embed]]\n# Heading\n```\n[[kept]] #kept\n```"
    cleaned = neutralize(hostile)
    assert "\\[\\[Secret Note\\]\\]" in cleaned and "\\#tag-me" in cleaned
    assert "\\# Heading" in cleaned
    assert "[[kept]] #kept" in cleaned
    assert not LINK_RE.findall(cleaned.split("```")[0])


def test_hostile_conversation_keeps_the_graph_clean(archive_env) -> None:
    hostile = conv(
        "grok",
        "evil",
        "Evil",
        [("user", "[[ghost]] #ghost"), ("assistant", "ok")],
    )
    write_store(archive_env.paths, [hostile])
    ingest_manifest = {
        "source_fingerprint": "f" * 64,
        "totals": {"conversations": 1, "messages": 2, "words": 4},
        "providers": {
            "grok": {
                "status": "ok",
                "conversations": 1,
                "empty_conversations": 0,
                "messages": 2,
                "words": 4,
                "created_min": "2025-01-01T00:00:00Z",
                "last_activity_max": "2025-01-01T00:00:00Z",
                "duplicates_folded": 0,
                "roles": {},
                "kinds": {},
                "reader": "t",
                "reader_counters": {},
                "channels": {"read": [], "not_read": []},
            }
        },
    }
    archive_env.paths.manifest.write_text(json.dumps(ingest_manifest), encoding="utf-8")
    build_analysis(archive_env.paths)
    build_vault(archive_env.paths, archive_env.auth)
    names = {Path(rel).stem for rel in _vault_files(archive_env.paths.vault)}
    assert "ghost" not in names
    _assert_links_resolve(archive_env.paths.vault)


def test_reasoning_and_tool_messages_are_collapsed_callouts() -> None:
    conversation = conv("chatgpt", "r", "R", [("user", "q")])
    conversation.messages.append(
        Message(
            role="assistant",
            text="line one\nline two",
            ts="2025-01-01T00:00:00Z",
            meta={"kind": "reasoning"},
        )
    )
    text, _ = render_conversation(
        conversation, related=[], topics=[], titles={}, collection_link=""
    )
    assert "> [!quote]- Assistant · reasoning · 2025-01-01 00:00\n> line one\n> line two" in text


def test_large_messages_and_notes_are_capped_with_a_marker() -> None:
    conversation = conv("chatgpt", "big", "Big", [("user", "x" * 500), ("assistant", "y" * 500)])
    text, truncated = render_conversation(
        conversation, related=[], topics=[], titles={}, collection_link="", message_limit=100
    )
    assert truncated and "400 characters omitted" in text
    text, truncated = render_conversation(
        conversation, related=[], topics=[], titles={}, collection_link="", note_limit=1200
    )
    assert truncated and "further message(s) omitted" in text


def test_rebuild_is_stable_prunes_stale_notes_and_keeps_user_notes(archive_env) -> None:
    write_store(archive_env.paths, topical_corpus())
    archive_env.paths.manifest.write_text(
        json.dumps(
            {
                "source_fingerprint": "a" * 64,
                "totals": {"conversations": 12, "messages": 24, "words": 100},
                "providers": {},
            }
        ),
        encoding="utf-8",
    )
    build_analysis(archive_env.paths)
    first = build_vault(archive_env.paths, archive_env.auth)
    root = archive_env.paths.vault
    snapshot = _vault_files(root)
    mine = root / "My notes.md"
    mine.write_text("# mine\n", encoding="utf-8")
    again = build_vault(archive_env.paths, archive_env.auth)
    assert again["removed_stale"] == 0 and first["notes"] == 12
    assert {k: v for k, v in _vault_files(root).items() if k != "My notes.md"} == snapshot
    kept = [c for c in topical_corpus() if c.uid != "chatgpt-o0"]
    write_store(archive_env.paths, kept)
    (archive_env.paths.conversations("chatgpt")).write_text(
        "".join(json.dumps(c.to_dict()) + "\n" for c in kept if c.provider == "chatgpt"),
        encoding="utf-8",
    )
    (archive_env.paths.index("chatgpt")).write_text(
        "".join(json.dumps(c.index_row()) + "\n" for c in kept if c.provider == "chatgpt"),
        encoding="utf-8",
    )
    build_analysis(archive_env.paths)
    pruned = build_vault(archive_env.paths, archive_env.auth)
    assert pruned["removed_stale"] >= 1
    assert not list(root.rglob("conversation-chatgpt-o0.md"))
    assert mine.is_file()
    _assert_links_resolve(root)


def test_topic_hubs_bridge_providers(archive_env) -> None:
    write_store(archive_env.paths, topical_corpus())
    archive_env.paths.manifest.write_text(
        json.dumps(
            {
                "source_fingerprint": "b" * 64,
                "totals": {"conversations": 12, "messages": 0, "words": 0},
                "providers": {},
            }
        ),
        encoding="utf-8",
    )
    build_analysis(archive_env.paths)
    build_vault(archive_env.paths, archive_env.auth)
    hub = (archive_env.paths.vault / "hubs" / "topic-all-kubernetes.md").read_text(encoding="utf-8")
    assert "across 4 provider(s)" in hub
    assert hub.count("[[") == 4


def test_inline_code_passes_through_and_link_targets_skip_all_code() -> None:
    cleaned = neutralize("use `[[ -f x ]]` and [[real]] and `#tag` plus #tag")
    assert "`[[ -f x ]]`" in cleaned and "`#tag`" in cleaned
    assert "\\[\\[real\\]\\]" in cleaned and "plus \\#tag" in cleaned
    sample = (
        "```bash\n[[ -f x ]]\n```\n> ```\n> [[ -d y ]]\n> ```\n~~~\n[[ z ]]\n~~~\n"
        "`[[ w ]]` [[real]] [[other|label]] [[tbl\\|label]]"
    )
    assert link_targets(sample) == ["real", "other", "tbl"]


def test_nested_and_unterminated_fences_cannot_leak_links_or_swallow_the_note() -> None:
    nested = "````markdown\n```py\n[[inner]]\n```\n[[still code]]\n````\n[[outside]]"
    cleaned = neutralize(nested)
    assert "[[inner]]" in cleaned and "[[still code]]" in cleaned
    assert "\\[\\[outside\\]\\]" in cleaned
    assert link_targets(cleaned) == []
    open_ended = neutralize("```py\nprint(1)\n[[x]]")
    assert open_ended.endswith("\n```"), "an open fence is closed so later messages render"
    assert link_targets(open_ended + "\n[[after]]") == ["after"]
    truncated, _ = render_conversation(
        conv(
            "chatgpt",
            "cut",
            "Cut",
            [("user", "```\n" + "line\n" * 50), ("assistant", "[[next]] ok")],
        ),
        related=[],
        topics=[],
        titles={},
        collection_link="",
        message_limit=40,
    )
    assert "\\[\\[next\\]\\]" in truncated, (
        "a fence cut by truncation must not swallow the next turn"
    )


def test_bracketed_titles_cannot_mint_links() -> None:
    assert plain_title("  [[AI Prompt]]   template \n") == "((AI Prompt)) template"
    assert plain_title("[]") == "()"
    assert plain_title("   ") == "Untitled"
    note, _ = render_conversation(
        conv("grok", "t", "[[Decision: Adopt X]]"),
        related=[],
        topics=[],
        titles={},
        collection_link="",
    )
    assert link_targets(note) == ["provider-grok", "month-all-202501"]
    assert 'title: "((Decision: Adopt X))"' in note


def test_shell_conditionals_in_messages_do_not_break_vault_verification(archive_env) -> None:
    script = "run:\n```bash\nif [[ -f x ]]; then echo ok; fi\n```\ninline `[[ -d y ]]` too"
    conversation = conv("chatgpt", "shell", "Shell help", [("user", script), ("assistant", "ok")])
    conversation.messages.append(
        Message(
            role="assistant",
            text='```sh\n[[ "$a" == b ]]\n```',
            ts="2025-01-01T00:00:00Z",
            meta={"kind": "reasoning"},
        )
    )
    write_store(archive_env.paths, [conversation])
    archive_env.paths.manifest.write_text(
        json.dumps(
            {
                "source_fingerprint": "c" * 64,
                "totals": {"conversations": 1, "messages": 3, "words": 10},
                "providers": {},
            }
        ),
        encoding="utf-8",
    )
    build_analysis(archive_env.paths)
    build_vault(archive_env.paths, archive_env.auth)
    note = next(archive_env.paths.vault.rglob("conversation-chatgpt-shell.md")).read_text(
        encoding="utf-8"
    )
    assert "[[ -f x ]]" in note and "`[[ -d y ]]`" in note
    assert verify_vault(archive_env.paths) == []


def test_undated_conversations_get_no_month_link_and_unnameable_topics_stay_plain(
    archive_env,
) -> None:
    write_store(
        archive_env.paths,
        [conv("grok", "u1", "Undated", created=""), conv("grok", "u2", "Dated")],
    )
    archive_env.paths.manifest.write_text(
        json.dumps(
            {
                "source_fingerprint": "d" * 64,
                "totals": {"conversations": 2, "messages": 4, "words": 8},
                "providers": {},
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {"uid": "grok-u1", "topics": ["well-known", "kubernetes"], "terms": [], "similar": []},
        {"uid": "grok-u2", "topics": ["kubernetes"], "terms": [], "similar": []},
    ]
    write_jsonl_atomic(archive_env.paths.enrichment, rows)
    summary = build_vault(archive_env.paths, archive_env.auth)
    undated = (archive_env.paths.vault_conversations / "conversation-grok-u1.md").read_text(
        encoding="utf-8"
    )
    assert "**Month**" not in undated and "month-all" not in undated
    assert "[[topic-all-kubernetes|kubernetes]]" in undated
    assert "well-known" in undated and "topic-all-well-known" not in undated
    assert "topic/kubernetes" in undated and "topic/well-known" not in undated
    assert summary["topics_without_hub"] == 1
    hubs = {p.name for p in archive_env.paths.vault_hubs.iterdir()}
    assert hubs == {
        "provider-grok.md",
        "month-all-202501.md",
        "topic-all-kubernetes.md",
    }
    assert verify_vault(archive_env.paths) == []


def test_every_link_in_the_vault_comes_from_the_one_wikilink_function(archive_env) -> None:
    source = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "conversation_corpus_engine"
        / "archive_vault.py"
    ).read_text(encoding="utf-8")
    by_hand = [line for line in source.splitlines() if "[[{" in line]
    assert all("dangling wikilink" in line for line in by_hand), by_hand
    assert 'f"[[' not in source and "f'[[" not in source
