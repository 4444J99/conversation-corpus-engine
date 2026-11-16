from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any

from conversation_corpus_engine.archive_ingest import ArchivePaths
from conversation_corpus_engine.archive_model import (
    Attachment,
    Conversation,
    Message,
    finalize_conversation,
    write_jsonl_atomic,
)
from conversation_corpus_engine.archive_shard import ShardWriter, month_key

BASE_EPOCH = 1_700_000_000.0
XSS = "<script>alert('x')</script>"


def _dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def gpt_conversation(
    cid: str,
    title: str,
    turns: list[tuple[str, str]],
    *,
    archived: bool = False,
    branch: bool = False,
    image: bool = False,
    start: float = BASE_EPOCH,
) -> dict[str, Any]:
    mapping: dict[str, Any] = {
        "root": {"id": "root", "message": None, "parent": None, "children": []}
    }
    parent = "root"
    hidden_id = f"{cid}-sys"
    mapping[hidden_id] = {
        "id": hidden_id,
        "parent": parent,
        "children": [],
        "message": {
            "id": hidden_id,
            "author": {"role": "system"},
            "create_time": start,
            "content": {"content_type": "text", "parts": ["hidden scaffolding"]},
            "metadata": {"is_visually_hidden_from_conversation": True},
        },
    }
    mapping[parent]["children"].append(hidden_id)
    parent = hidden_id
    last = parent
    for index, (role, text) in enumerate(turns):
        node_id = f"{cid}-{index}"
        parts: list[Any] = [text]
        if image and index == 0:
            parts.append(
                {
                    "content_type": "image_asset_pointer",
                    "asset_pointer": "file-service://file-IMG1",
                    "size_bytes": 12,
                }
            )
        mapping[node_id] = {
            "id": node_id,
            "parent": parent,
            "children": [],
            "message": {
                "id": node_id,
                "author": {"role": role},
                "create_time": start + 60 * (index + 1),
                "content": {
                    "content_type": "multimodal_text" if image and index == 0 else "text",
                    "parts": parts,
                },
                "metadata": {"model_slug": "gpt-test"} if role == "assistant" else {},
            },
        }
        mapping[parent]["children"].append(node_id)
        parent = node_id
        last = node_id
    if branch:
        stray = f"{cid}-stray"
        stray_parent = mapping[last]["parent"]
        mapping[stray] = {
            "id": stray,
            "parent": stray_parent,
            "children": [],
            "message": {
                "id": stray,
                "author": {"role": "assistant"},
                "create_time": start + 999,
                "content": {"content_type": "text", "parts": ["abandoned regeneration"]},
                "metadata": {"model_slug": "gpt-test"},
            },
        }
        mapping[stray_parent]["children"].append(stray)
    return {
        "conversation_id": cid,
        "id": cid,
        "title": title,
        "create_time": start,
        "update_time": start + 3600,
        "current_node": last,
        "is_archived": archived,
        "default_model_slug": None,
        "gizmo_id": "g-p-proj" if cid.endswith("p") else None,
        "mapping": mapping,
    }


def _zip(path: Path, members: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, json.dumps(payload))


def build_export(root: Path) -> Path:
    """Write a synthetic multi-provider export tree; every string is invented."""
    chatgpt = root / "chatgpt"
    _dump(
        chatgpt / "conversations-000.json",
        [
            gpt_conversation(
                "aaaa1111",
                "Kubernetes rollout planning",
                [
                    ("user", f"How do I plan a kubernetes cluster rollout? {XSS}"),
                    (
                        "assistant",
                        "Plan the kubernetes cluster rollout in phases.\n\n```py\nprint(1)\n```",
                    ),
                ],
                branch=True,
                image=True,
            ),
            gpt_conversation(
                "bbbb2222",
                "Sourdough starter notes",
                [
                    ("user", "Feeding my sourdough starter sourdough daily"),
                    ("assistant", "Keep sourdough warm."),
                ],
                archived=True,
                start=BASE_EPOCH + 86400 * 40,
            ),
            gpt_conversation("cccc3333p", "Empty shell", []),
        ],
    )
    _dump(chatgpt / "conversation_asset_file_names.json", {"file-IMG1.dat": "diagram.png"})
    _dump(chatgpt / "user.json", {"email": "redacted@example.invalid"})

    claude = root / "claude"
    _dump(
        claude / "conversations.json",
        [
            {
                "uuid": "c1c1c1c1",
                "name": "Kubernetes cost review",
                "summary": "cost",
                "created_at": "2025-01-05T10:00:00Z",
                "updated_at": "2025-01-05T11:00:00Z",
                "chat_messages": [
                    {
                        "uuid": "m1",
                        "text": "ignored when blocks exist",
                        "sender": "human",
                        "created_at": "2025-01-05T10:00:00Z",
                        "content": [{"type": "text", "text": "Review kubernetes cluster costs"}],
                        "attachments": [
                            {
                                "file_name": "bill.pdf",
                                "file_size": 9,
                                "file_type": "pdf",
                                "extracted_content": "PRIVATE-ATTACHMENT-TEXT",
                            }
                        ],
                        "files": [],
                        "parent_message_uuid": "root",
                    },
                    {
                        "uuid": "m2",
                        "text": "",
                        "sender": "assistant",
                        "created_at": "2025-01-05T10:01:00Z",
                        "content": [
                            {"type": "thinking", "thinking": "hmm"},
                            {"type": "text", "text": "Right-size the kubernetes cluster nodes."},
                            {"type": "tool_use", "name": "calc"},
                        ],
                        "attachments": [],
                        "files": [{"file_name": "chart.png"}],
                        "parent_message_uuid": "m1",
                    },
                ],
            },
            {
                "uuid": "c2c2c2c2",
                "name": "Bread hydration",
                "summary": "",
                "created_at": "2025-02-01T10:00:00Z",
                "updated_at": "2025-02-01T10:05:00Z",
                "chat_messages": [
                    {
                        "uuid": "n1",
                        "text": "sourdough hydration sourdough ratio",
                        "sender": "human",
                        "created_at": "2025-02-01T10:00:00Z",
                        "content": [],
                        "attachments": [],
                        "files": [],
                        "parent_message_uuid": "root",
                    },
                    {
                        "uuid": "n2",
                        "text": "Use 75% hydration for sourdough.",
                        "sender": "assistant",
                        "created_at": "2025-02-01T10:01:00Z",
                        "content": [],
                        "attachments": [],
                        "files": [],
                        "parent_message_uuid": "n1",
                    },
                ],
            },
        ],
    )
    _dump(claude / "users.json", [{"uuid": "u", "email_address": "redacted@example.invalid"}])
    _zip(
        claude / "projects-000.zip",
        {
            "projects/p1.json": {
                "uuid": "p1",
                "name": "Ops handbook",
                "description": "d",
                "is_private": True,
                "prompt_template": "Be terse about kubernetes operations.",
                "created_at": "2025-03-01T00:00:00Z",
                "updated_at": "2025-03-02T00:00:00Z",
                "docs": [
                    {
                        "uuid": "d1",
                        "filename": "runbook.md",
                        "content": "Runbook for kubernetes cluster upgrades.",
                        "created_at": "2025-03-01T00:00:00Z",
                    }
                ],
            }
        },
    )
    _zip(
        claude / "memories-000.zip",
        {
            "memories/mem.json": {
                "conversations_memory": "Likes sourdough baking.",
                "project_memories": {"p1": "Ops handbook memory"},
                "memory_files": [
                    {
                        "path": "notes.md",
                        "content": "note body",
                        "updated_at": "2025-03-03T00:00:00Z",
                    }
                ],
                "account_uuid": "x",
            }
        },
    )
    _zip(
        claude / "design_chats-000.zip",
        {
            "design_chats/d.json": {
                "uuid": "dd",
                "title": "Design chat",
                "project": {"uuid": "p1", "name": "Ops handbook"},
                "created_at": "2025-03-04T00:00:00Z",
                "updated_at": "2025-03-04T00:00:00Z",
                "messages": [],
            }
        },
    )

    _dump(
        root / "perplexity" / "conversations-20250101.json",
        {
            "conversations": [
                {
                    "context_uuid": "pp1",
                    "context_title": "Kubernetes ingress options",
                    "created_at": "2025-04-01T10:00:00Z",
                    "updated_at": "2025-04-01T10:05:00Z",
                    "mode": "search",
                    "collection_uuid": None,
                    "entries": [
                        {
                            "entry_uuid": "e1",
                            "query": "kubernetes ingress controller choices",
                            "answer": "Compare ingress kubernetes controllers.",
                            "created_at": "2025-04-01T10:00:00Z",
                            "label": None,
                            "query_status": "COMPLETED",
                            "engine_mode": "pro",
                        },
                    ],
                },
                {
                    "context_uuid": "pp2",
                    "context_title": "",
                    "created_at": "2025-04-02T10:00:00Z",
                    "updated_at": "2025-04-02T10:00:00Z",
                    "mode": "search",
                    "collection_uuid": None,
                    "entries": [],
                },
            ]
        },
    )
    (root / "perplexity" / "user-data-20250101.xlsx").write_bytes(b"not-read")

    grok_dir = root / "grok" / "30d" / "export_data" / "wsid"
    _dump(
        grok_dir / "prod-grok-backend.json",
        {
            "conversations": [
                {
                    "conversation": {
                        "id": "g1",
                        "title": "Sourdough schedule",
                        "create_time": "2025-05-01T10:00:00.123456789Z",
                        "modify_time": "2025-05-01T10:30:00Z",
                        "leaf_response_id": "r3",
                        "starred": True,
                        "summary": "bake",
                        "temporary": False,
                    },
                    "responses": [
                        {
                            "response": {
                                "_id": "r1",
                                "conversation_id": "g1",
                                "message": "sourdough schedule please sourdough",
                                "sender": "human",
                                "create_time": {"$date": {"$numberLong": "1746093600000"}},
                                "parent_response_id": None,
                            }
                        },
                        {
                            "response": {
                                "_id": "r2",
                                "conversation_id": "g1",
                                "message": "off-branch draft",
                                "sender": "ASSISTANT",
                                "create_time": {"$date": {"$numberLong": "1746093660000"}},
                                "parent_response_id": "r1",
                                "model": "grok-test",
                            }
                        },
                        {
                            "response": {
                                "_id": "r3",
                                "conversation_id": "g1",
                                "message": "Bake sourdough at dawn.",
                                "sender": "ASSISTANT",
                                "create_time": {"$date": {"$numberLong": "1746093720000"}},
                                "parent_response_id": "r1",
                                "model": "grok-test",
                            }
                        },
                    ],
                },
                {
                    "conversation": {
                        "id": "g2",
                        "title": "No leaf",
                        "create_time": "2025-05-02T10:00:00Z",
                        "modify_time": "2025-05-02T10:00:00Z",
                    },
                    "responses": [
                        {
                            "response": {
                                "_id": "s2",
                                "message": "second",
                                "sender": "ASSISTANT",
                                "create_time": {"$date": {"$numberLong": "1746180060000"}},
                            }
                        },
                        {
                            "response": {
                                "_id": "s1",
                                "message": "first",
                                "sender": "human",
                                "create_time": {"$date": {"$numberLong": "1746180000000"}},
                            }
                        },
                    ],
                },
            ],
            "projects": [
                {
                    "workspace_id": "w1",
                    "name": "Bakery",
                    "create_time": "2025-05-03T00:00:00Z",
                    "last_use_time": "2025-05-04T00:00:00Z",
                    "custom_personality": "Be a baker.",
                    "conversation_starters": ["Start dough"],
                    "preferred_model": "grok-test",
                    "conversations_created_count": 2,
                }
            ],
            "tasks": [
                {
                    "task": {
                        "id": "t1",
                        "title": "Daily bread reminder",
                        "prompt": "Remind me about sourdough",
                    },
                    "schedules": [{}],
                    "results": [{"message": "Time to feed the starter"}],
                }
            ],
            "media_posts": [
                {
                    "id": "mp1",
                    "original_prompt": "a loaf of bread",
                    "media_type": "image",
                    "create_time": "2025-05-05T00:00:00Z",
                    "link": "https://example.invalid/1",
                },
                {
                    "id": "mp2",
                    "original_prompt": "a kubernetes logo",
                    "media_type": "image",
                    "create_time": "2025-06-05T00:00:00Z",
                    "link": "https://example.invalid/2",
                },
                {
                    "id": "mp3",
                    "original_prompt": "  ",
                    "media_type": "image",
                    "create_time": "2025-06-06T00:00:00Z",
                    "link": "https://example.invalid/3",
                },
            ],
        },
    )
    (grok_dir / "prod-mc-billing.json").write_text("{}", encoding="utf-8")

    gemini = root / "gemini"
    activity = gemini / "My Activity" / "Gemini Apps" / "MyActivity.json"

    def item(
        title: str, time: str, app: str | None, html: str | None = None, **extra: Any
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "header": "Gemini Apps",
            "title": title,
            "time": time,
            "products": ["Gemini Apps"],
        }
        if app:
            payload["details"] = [
                {
                    "name": f"https://gemini.google.com/app/{app}",
                    "url": f"https://gemini.google.com/app/{app}",
                }
            ]
        if html is not None:
            payload["safeHtmlItem"] = [{"html": html}]
        payload.update(extra)
        return payload

    _dump(
        activity,
        [
            item(
                "Prompted Explain kubernetes cluster autoscaling",
                "2025-06-01T10:00:00.000Z",
                "appone",
                "<p>Autoscaling <strong>kubernetes</strong> clusters.</p><pre><code>kubectl get nodes</code></pre>",
            ),
            item(
                "Prompted And pods?",
                "2025-06-01T10:05:00.000Z",
                "appone",
                "<ul><li>pods</li><li>nodes</li></ul>",
                attachedFiles=["shot.png"],
            ),
            item(
                "Branched Try another approach",
                "2025-06-01T10:10:00.000Z",
                "appone",
                "<p>branch answer</p>",
            ),
            item("Prompted Another thread", "2025-06-02T10:00:00.000Z", "apptwo", "<p>ok</p>"),
            item(
                "Created Gemini Canvas titled Bread plan",
                "2025-06-03T10:00:00.000Z",
                None,
                subtitles=[{"name": "# Bread plan\n\nsourdough sourdough"}],
            ),
            item("Used Gemini Apps", "2025-06-03T10:01:00.000Z", None),
            item("Prompted loose one", "2025-06-04T10:00:00.000Z", None, "<p>a</p>"),
            item("Prompted loose two", "2025-06-04T10:10:00.000Z", None, "<p>b</p>"),
            item("Prompted loose three much later", "2025-06-04T18:00:00.000Z", None, "<p>c</p>"),
        ],
    )
    _dump(
        gemini / "My Activity" / "AI Mode" / "AI Mode.json",
        [
            {
                "header": "AI Mode",
                "title": "Searched for kubernetes pricing",
                "time": "2025-07-01T10:00:00.000Z",
                "safeHtmlItem": [
                    {"html": "<p><strong>Your prompt:</strong><br>kubernetes pricing</p>"}
                ],
            }
        ],
    )
    (gemini / "My Activity" / "Maps").mkdir(parents=True)
    _dump(
        gemini / "Gemini in Workspace" / "Conversation History" / "conversation_111.txt",
        {
            "conversation_turns": [
                {
                    "user_turn": {
                        "prompt": "Summarize the file",
                        "turn_index": 0,
                        "turn_last_modified": "2025-08-01T10:00:00+00:00",
                    }
                },
                {
                    "system_turn": {
                        "text": "Summary here",
                        "citations": [
                            {"display_text": "Doc", "url": "https://example.invalid/doc"}
                        ],
                        "turn_index": 0,
                        "turn_last_modified": "2025-08-01T10:00:05+00:00",
                    }
                },
                {
                    "user_turn": {
                        "prompt": "deleted prompt",
                        "turn_deleted_time": "2025-08-02T00:00:00+00:00",
                        "turn_last_modified": "2025-08-01T10:01:00+00:00",
                    }
                },
            ]
        },
    )

    notebook = gemini / "NotebookLM" / "Alpha Notebook"
    _dump(
        notebook / "Alpha Notebook metadata.json",
        {
            "title": "Alpha Notebook",
            "id": "nb-alpha",
            "emoji": "A",
            "metadata": {
                "isShared": False,
                "lastViewed": "2025-09-02T00:00:00Z",
                "createTime": "2025-09-01T00:00:00Z",
            },
        },
    )
    (notebook / "Sources").mkdir(parents=True)
    (notebook / "Sources" / "Paper.html").write_text(
        "<h1>Paper</h1><p>kubernetes kubernetes scheduling theory</p>", encoding="utf-8"
    )
    _dump(notebook / "Sources" / "Paper metadata.json", {"title": "Paper", "type": "PDF"})
    (notebook / "Notes").mkdir()
    (notebook / "Notes" / "Note one.html").write_text("<p>sourdough note</p>", encoding="utf-8")
    (notebook / "Artifacts").mkdir()
    _dump(
        notebook / "Artifacts" / "Overview metadata.json",
        {
            "title": "Overview",
            "type": "ARTIFACT_TYPE_AUDIO_OVERVIEW",
            "status": "ARTIFACT_STATUS_READY",
            "sources": [{}],
        },
    )
    (notebook / "Artifacts" / "Overview.wav").write_bytes(b"RIFF")
    return root


def conv(
    provider: str,
    source_id: str,
    title: str,
    texts: list[tuple[str, str]] | None = None,
    *,
    created: str = "2025-01-01T00:00:00Z",
    kind: str = "chat",
    collection: str = "",
    attachments: list[Attachment] | None = None,
) -> Conversation:
    messages = [
        Message(role=role, text=text, ts=created, meta={"kind": "text"})
        for role, text in (
            texts or [("user", f"{title} question"), ("assistant", f"{title} answer")]
        )
    ]
    if attachments and messages:
        messages[0].attachments = attachments
    conversation = Conversation(
        provider=provider,
        source_id=source_id,
        title=title,
        created=created,
        updated=created,
        kind=kind,
        collection=collection,
        messages=messages,
        provenance={"source_file": "x.json", "source_sha256": "0" * 64, "reader": "test@1"},
    )
    finalize_conversation(conversation)
    return conversation


def add_idless_chatgpt_shard(export: Path) -> Path:
    """Add a second ChatGPT shard holding two conversations that have no vendor id.

    The two differ in content, so each must get its own derived id. Every string is invented.
    """
    shard = export / "chatgpt" / "conversations-001.json"
    _dump(
        shard,
        [
            gpt_conversation(
                "",
                "Lantern maintenance",
                [("user", "How often should lantern wicks be trimmed?"), ("assistant", "Weekly.")],
                start=BASE_EPOCH + 86400 * 60,
            ),
            gpt_conversation(
                "",
                "Kite flying basics",
                [("user", "Which wind speed suits a beginner kite?"), ("assistant", "Light wind.")],
                start=BASE_EPOCH + 86400 * 61,
            ),
        ],
    )
    return shard


def write_store(paths: ArchivePaths, conversations: list[Conversation]) -> None:
    by_provider: dict[str, list[Conversation]] = {}
    for conversation in conversations:
        finalize_conversation(conversation)
        by_provider.setdefault(conversation.provider, []).append(conversation)
    for provider, items in by_provider.items():
        write_jsonl_atomic(paths.conversations(provider), [c.to_dict() for c in items])
        write_jsonl_atomic(paths.index(provider), [c.index_row() for c in items])


def write_sharded_store(
    paths: ArchivePaths, conversations: list[Conversation], *, shard_bytes: int = 48 * 1024 * 1024
) -> None:
    """The bounded layout: each provider's conversations in provider-month shards."""
    by_provider: dict[str, list[Conversation]] = {}
    for conversation in conversations:
        finalize_conversation(conversation)
        by_provider.setdefault(conversation.provider, []).append(conversation)
    for provider, items in by_provider.items():
        directory = paths.provider_dir(provider)
        writer = ShardWriter(directory, provider, shard_bytes=shard_bytes)
        for item in items:
            writer.add(
                month_key(item.created or item.last_activity),
                (json.dumps(item.to_dict(), sort_keys=True, ensure_ascii=False) + "\n").encode(),
                (json.dumps(item.index_row(), sort_keys=True, ensure_ascii=False) + "\n").encode(),
            )
        writer.close()


def topical_corpus() -> list[Conversation]:
    """Twelve invented conversations with two clear topics spanning providers."""
    items: list[Conversation] = []
    for index in range(4):
        provider = ["chatgpt", "claude", "gemini", "grok"][index]
        items.append(
            conv(
                provider,
                f"k{index}",
                f"Kubernetes note {index}",
                [
                    ("user", "kubernetes cluster rollout kubernetes cluster"),
                    ("assistant", "kubernetes cluster operators"),
                ],
            )
        )
    for index in range(3):
        items.append(
            conv(
                ["chatgpt", "claude", "perplexity"][index],
                f"s{index}",
                f"Sourdough note {index}",
                [
                    ("user", "sourdough starter sourdough hydration"),
                    ("assistant", "sourdough hydration schedule"),
                ],
            )
        )
    for index in range(5):
        items.append(
            conv(
                "chatgpt",
                f"o{index}",
                f"Misc {index}",
                [("user", f"unrelated{index}x topic"), ("assistant", "short")],
            )
        )
    return items


# --- synthetic Copilot captures --------------------------------------------------------------
# SingleFile-style saved pages built from small pieces so a test can vary one trait at a time.
# Every string is invented. Page text is passed as HTML source: nothing here is escaped.

COPILOT_ID = "Zq8Lw3Nx-Tp_4RmKe7Vh2"
COPILOT_URL = f"https://copilot.microsoft.com/research/{COPILOT_ID}"
COPILOT_SAVED = "Tue Mar 10 2026 09:15:00 GMT-0400 (Eastern Daylight Time)"
COPILOT_SAVED_ISO = "2026-03-10T13:15:00Z"
COPILOT_TITLE = "Lantern Wick Care"
COPILOT_BADGE = "Deep Research"
COPILOT_PAGE_NAME = "research-lantern.html"
COPILOT_CHROME_STRINGS = (
    "SIDEBAR-CHROME-MUST-NOT-APPEAR",
    "New chat chrome",
    "Share chrome",
    "DIALOG-CHROME-MUST-NOT-APPEAR",
    "INFOBAR-CHROME-MUST-NOT-APPEAR",
    "STYLE-CHROME-MUST-NOT-APPEAR",
    "GENERIC-TITLE-CHROME",
)
COPILOT_CARD_URLS = (
    "https://lamps.example.org/guides/wicks",
    "https://oil.example.com/handbook/glass",
)

_COPILOT_HEAD = (
    "<!DOCTYPE html><html lang=en-US data-theme=light dir=ltr><head><meta charset=utf-8>"
    "<title>GENERIC-TITLE-CHROME</title>"
    '<meta property=og:title content="GENERIC-TITLE-CHROME">'
    "<style>.sidebar-chrome{color:red}/* STYLE-CHROME-MUST-NOT-APPEAR */</style></head>"
)
_COPILOT_OPEN = (
    "<body data-tabster='{\"root\":{}}'><div id=root>"
    '<div class="bg-sidebar-light flex h-full overflow-hidden"><div class=flex-none>'
    "<div role=navigation>SIDEBAR-CHROME-MUST-NOT-APPEAR"
    "<button type=button data-testid=sidebar-new-conversation-nav-item>New chat chrome</button>"
    "</div></div>"
    '<main class="after:border"><div class="flex-1 h-full overflow-hidden">'
    '<div class="bg-background-150 overflow-y-auto size-full">'
    '<div class="pointer-events-none sticky top-0 w-full"><span>Share chrome</span></div>'
    '<div class="flex items-center justify-center pb-20 pt-14 w-full">'
    '<div class="lg:max-w-[768px] min-w-[320px] px-4">'
)
_COPILOT_CLOSE = (
    "</div></div></div></div></main></div>"
    "<dialog>DIALOG-CHROME-MUST-NOT-APPEAR</dialog>"
    "<single-file-infobar><template shadowrootmode=open>INFOBAR-CHROME-MUST-NOT-APPEAR"
    "</template></single-file-infobar></div></body></html>\n"
)


def copilot_spans(text: str) -> str:
    """Text as thousands of tiny inline spans, one per word, each keeping its trailing space."""
    return "".join(f"<span>{piece}</span>" for piece in re.findall(r"\S+\s*|\s+", text))


def copilot_pill(label: str = "lamps.example.org+2") -> str:
    """An inline citation pill: a button holding `domain+N`."""
    return (
        '<span class="align-middle group h-6"><button type=button aria-expanded=false '
        f'aria-label="Source {label}">{label}</button></span>'
    )


def copilot_card(
    index: int | str | None,
    site: str | None,
    title: str | tuple[str, ...] | None,
    url: str | tuple[str, ...] | None,
) -> str:
    """One source card (`<li class="list-none w-full">`, end tag omitted like the real pages)."""
    hrefs = (url,) if isinstance(url, str) else tuple(url or ())
    anchors = "".join(
        f'<a class="active:bg-white/60" href="{href}" target=_blank rel=noopener></a>'
        for href in hrefs
    )
    badge = "" if index is None else f'<span class="bg-accent-200 h-4">{index}</span>'
    domain = "" if site is None else f"<span class=me-0.5>{site}</span>"
    titles = (title,) if isinstance(title, str) else tuple(title or ())
    heading = "".join(f"<div>{part}</div>" for part in titles)
    return (
        '<li class="list-none w-full"><span class=inline-flex><div class="flex flex-col">'
        f'<div class="flex items-center">{anchors}{badge}{domain}</div>{heading}</div></span>'
    )


def copilot_card_list(*cards: str, closed: bool = True) -> str:
    """The trailing sources list around already-built cards."""
    return '<ul class="flex flex-col">' + "".join(cards) + ("</ul>" if closed else "")


def copilot_item(
    role: str,
    body: str,
    *,
    title: str | None = None,
    badge: str = COPILOT_BADGE,
    closed: bool = True,
) -> str:
    """One message item. A `title` adds the header block (badge + `text-3xl` session title)."""
    header = ""
    if title is not None:
        header = (
            '<div class="flex flex-col gap-y-6"><div class="flex items-center gap-2">'
            f'<div class="bg-current forced-color-adjust-none">{badge}</div></div>'
            f"<div class=text-3xl>{title}</div></div>"
        )
    opening = f'<div class="break-words group/{role}-message-item pt-9 space-y-3">'
    return f"{opening}{header}{body}" + ("</div>" if closed else "")


def copilot_comment(url: str | None = COPILOT_URL, saved: str | None = COPILOT_SAVED) -> str:
    """The three-line SingleFile comment; passing None leaves that line out."""
    lines = ["<!--", " Page saved with SingleFile "]
    if url is not None:
        lines.append(f" url: {url}")
    if saved is not None:
        lines.append(f" saved date: {saved}")
    lines.append("-->")
    return "\n".join(lines)


def copilot_page(
    items: str | list[str],
    *,
    url: str | None = COPILOT_URL,
    saved: str | None = COPILOT_SAVED,
    wrap: bool = True,
) -> str:
    """A whole saved page. `wrap=True` surrounds the items with balanced page chrome (sidebar,
    share bar, dialog, template, style); `wrap=False` puts them straight in the body, which is
    what a test of an item that never closes needs (no later `</div>` to balance it)."""
    inner = items if isinstance(items, str) else "".join(items)
    if wrap:
        return copilot_comment(url, saved) + _COPILOT_HEAD + _COPILOT_OPEN + inner + _COPILOT_CLOSE
    return copilot_comment(url, saved) + "<html><body>" + inner + "</body></html>\n"


def write_copilot_page(path: Path, items: str | list[str], **kwargs: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(copilot_page(items, **kwargs), encoding="utf-8")
    return path


def copilot_report_body() -> str:
    """The body of the standard report: h1, two h2, one h3, tiny-span paragraphs, pills (some
    before punctuation), strong/em, a list with omitted `</li>`, an `ol start=3`, a blockquote, a
    table with omitted cell and row end tags (one cell holds a pipe), and two source cards."""
    spans = copilot_spans
    pill = copilot_pill
    wrapper_open = (
        '<div class=max-w-full><div class="relative max-w-full">'
        '<div class="horizontal-scrollbar mt-6" aria-label="Table" role=region tabindex=0>'
    )
    return "".join(
        [
            '<h1 class="[&:not(:first-child)]:pt-6 text-2xl-medium">',
            spans("Lantern Care Playbook: Wicks, Oil and Glass"),
            "</h1>",
            '<div class="after:absolute after:border-b relative pb-6 w-full"></div>',
            '<h2 class="text-xl-medium pb-1">Overview</h2>',
            '<p><span class="font-ligatures-none whitespace-pre-wrap">',
            spans("A trimmed wick burns evenly "),
            pill("lamps.example.org+2"),
            spans(". Keep the oil level high "),
            pill("oil.example.com"),
            spans(", and the glass clean."),
            "</span>",
            "<p><span class=font-ligatures-none>",
            spans("Trim "),
            "<strong>",
            spans("twice weekly "),
            "</strong>",
            spans("when "),
            "<em>",
            spans("windy"),
            "</em>",
            spans(", once when calm."),
            "</span>",
            '<h3 class="text-lg-medium">Trimming schedule</h3>',
            '<ul class="flex flex-col list-outside ms-4">',
            "<li class=ps-2><span>Cut the char flat.</span>",
            pill("lamps.example.org"),
            "<li class=ps-2><span>Round the corners lightly.</span>",
            "<li class=ps-2><span>Wipe the glass.</span>",
            "</ul>",
            "<ol start=3 class=list-outside>",
            "<li class=ps-2><span>Light the lantern.</span>",
            "<li class=ps-2><span>Check the flame height.</span>",
            "</ol>",
            '<blockquote class="border-foreground-300 border-s-2 flex flex-col gap-3 ps-3">',
            "<p><span>A steady flame is a quiet flame.</span>",
            "</blockquote>",
            '<h2 class="text-xl-medium pb-1">Comparison</h2>',
            wrapper_open,
            "<table class=t-table><thead><tr><th>Wick<th>Burn rate<th>Notes",
            "<tbody><tr><td>Flat cotton<td><span>Slow</span>",
            pill("oil.example.com"),
            "<td>Use a | b mix",
            "<tr><td>Round braid<td><strong>Fast</strong><td>Needs trimming",
            "</table></div></div></div>",
            copilot_card_list(
                copilot_card(
                    1, "lamps.example.org", "Wick Trimming for Oil Lanterns", COPILOT_CARD_URLS[0]
                ),
                copilot_card(2, "oil.example.com", "Glass Care Handbook", COPILOT_CARD_URLS[1]),
            ),
        ]
    )


def copilot_report_page(**kwargs: Any) -> str:
    return copilot_page(
        [copilot_item("ai", copilot_report_body(), title=COPILOT_TITLE)],
        **kwargs,
    )


def copilot_small_page(
    identifier: str,
    title: str,
    paragraph: str,
    *,
    saved: str | None = COPILOT_SAVED,
    surface: str = "research",
) -> str:
    """A one-paragraph page for tests that need several distinct captures."""
    url = f"https://copilot.microsoft.com/{surface}/{identifier}"
    body = f"<p>{copilot_spans(paragraph)}"
    return copilot_page([copilot_item("ai", body, title=title)], url=url, saved=saved)


def foreign_singlefile_page(url: str = "https://example.org/article") -> str:
    """A SingleFile page from some other site: it must never be read as a Copilot page."""
    return (
        copilot_comment(url, COPILOT_SAVED)
        + "<html><body><div class=article><p>NOT-A-COPILOT-PAGE-TEXT</div></body></html>\n"
    )


def build_copilot_folder(root: Path) -> Path:
    """A provider folder: the standard page plus a `.docx` pair, a `.txt` and a foreign `.html`."""
    root.mkdir(parents=True, exist_ok=True)
    (root / COPILOT_PAGE_NAME).write_text(copilot_report_page(), encoding="utf-8")
    (root / "attachments").mkdir(exist_ok=True)
    (root / "attachments" / "report-draft.docx").write_bytes(b"stub-docx-one")
    (root / "summary.docx").write_bytes(b"stub-docx-two")
    (root / "notes.txt").write_text("stub text capture\n", encoding="utf-8")
    (root / "unrelated.html").write_text(foreign_singlefile_page(), encoding="utf-8")
    return root
