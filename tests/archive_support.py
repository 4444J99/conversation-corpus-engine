from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from conversation_corpus_engine.archive_ingest import ArchivePaths
from conversation_corpus_engine.archive_model import (
    Attachment,
    Conversation,
    Message,
    write_jsonl_atomic,
)

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
    return Conversation(
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


def write_store(paths: ArchivePaths, conversations: list[Conversation]) -> None:
    by_provider: dict[str, list[Conversation]] = {}
    for conversation in conversations:
        by_provider.setdefault(conversation.provider, []).append(conversation)
    for provider, items in by_provider.items():
        write_jsonl_atomic(paths.conversations(provider), [c.to_dict() for c in items])
        write_jsonl_atomic(paths.index(provider), [c.index_row() for c in items])


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
