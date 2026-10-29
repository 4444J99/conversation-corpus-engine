from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conversation_corpus_engine.corpus_text import detect_near_duplicates  # noqa: E402
from conversation_corpus_engine.import_claude_export_corpus import (  # noqa: E402
    build_thread_audit,
    extract_message_text,
    import_claude_export_corpus,
)
from conversation_corpus_engine.sharded_collection import (  # noqa: E402
    collection_storage_path,
    load_corpus_collection,
)


class ImportClaudeExportCorpusTests(unittest.TestCase):
    def _write_bundle(self, root: Path, conversations: list[dict[str, object]]) -> Path:
        bundle = root / "claude-bundle"
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "users.json").write_text(
            json.dumps([{"uuid": "user-1", "full_name": "Test User"}]),
            encoding="utf-8",
        )
        (bundle / "projects.json").write_text("[]", encoding="utf-8")
        (bundle / "memories.json").write_text("[]", encoding="utf-8")
        (bundle / "conversations.json").write_text(
            json.dumps(conversations),
            encoding="utf-8",
        )
        return bundle

    def test_import_claude_export_corpus_builds_federation_surface(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_root = Path(tmpdir) / "claude-history-memory"
            bundle = self._write_bundle(
                root,
                [
                    {
                        "uuid": "conv-1",
                        "name": "Claude Test Thread",
                        "summary": "",
                        "created_at": "2026-03-14T10:00:00Z",
                        "updated_at": "2026-03-14T10:05:00Z",
                        "chat_messages": [
                            {
                                "uuid": "msg-1",
                                "sender": "human",
                                "created_at": "2026-03-14T10:00:00Z",
                                "updated_at": "2026-03-14T10:00:00Z",
                                "text": "Build the adapter for Claude exports.",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "Build the adapter for Claude exports.",
                                    }
                                ],
                                "attachments": [],
                                "files": [],
                            },
                            {
                                "uuid": "msg-2",
                                "sender": "assistant",
                                "created_at": "2026-03-14T10:01:00Z",
                                "updated_at": "2026-03-14T10:01:00Z",
                                "text": "I will implement a Claude export adapter and keep it in calibration.",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "I will implement a Claude export adapter and keep it in calibration.",
                                    },
                                    {
                                        "type": "tool_use",
                                        "name": "artifacts",
                                        "input": {"title": "Claude Adapter Plan"},
                                    },
                                ],
                                "attachments": [],
                                "files": [],
                            },
                        ],
                    },
                ],
            )

            result = import_claude_export_corpus(
                bundle,
                output_root,
                corpus_id="claude-history-memory",
                name="Claude History Memory",
            )

            self.assertEqual(result["corpus_id"], "claude-history-memory")
            self.assertEqual(result["thread_count"], 1)
            self.assertGreaterEqual(result["pair_count"], 1)
            contract = json.loads(
                (output_root / "corpus" / "contract.json").read_text(encoding="utf-8")
            )
            self.assertEqual(contract["adapter_type"], "archive-normalized")
            self.assertEqual(contract["corpus_id"], "corpus-claude-archive")
            self.assertEqual(contract["counts"]["threads"], 1)
            self.assertFalse((output_root / "corpus" / "source-snapshot.json").exists())
            threads = load_corpus_collection(output_root / "corpus" / "threads-index.json")
            self.assertEqual(threads[0]["title_normalized"], "Claude Test Thread")
            self.assertIn("claude-archive", threads[0]["tags"])
            readme = (output_root / "README.md").read_text(encoding="utf-8")
            self.assertIn("Imported conversations: 1", readme)

    def test_import_claude_export_writes_projector_surface_without_legacy_audit_files(
        self,
    ) -> None:
        # The rewire (#78) retired the importers' hand-built audit and
        # near-duplicate files with the hand-built shaping; the same bundle now
        # yields the projector's collections, and the repeated ask is no longer
        # screened here.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_root = root / "claude-history-memory"
            duplicate_prompt = (
                "Please build a deterministic federation adapter with audit visibility."
            )
            bundle = self._write_bundle(
                root,
                [
                    {
                        "uuid": "conv-1",
                        "name": "Claude Audit Thread A",
                        "summary": "",
                        "created_at": "2026-03-14T10:00:00Z",
                        "updated_at": "2026-03-14T10:05:00Z",
                        "chat_messages": [
                            {
                                "uuid": "msg-1",
                                "sender": "human",
                                "created_at": "2026-03-14T10:00:00Z",
                                "updated_at": "2026-03-14T10:00:00Z",
                                "text": duplicate_prompt,
                                "content": [{"type": "text", "text": duplicate_prompt}],
                                "attachments": [],
                                "files": [],
                            },
                            {
                                "uuid": "msg-2",
                                "sender": "assistant",
                                "created_at": "2026-03-14T10:01:00Z",
                                "updated_at": "2026-03-14T10:01:00Z",
                                "text": "I will keep parity with the richer adapter path.",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "I will keep parity with the richer adapter path.",
                                    }
                                ],
                                "attachments": [],
                                "files": [],
                            },
                        ],
                    },
                    {
                        "uuid": "conv-2",
                        "name": "Claude Audit Thread B",
                        "summary": "",
                        "created_at": "2026-03-14T11:00:00Z",
                        "updated_at": "2026-03-14T11:05:00Z",
                        "chat_messages": [
                            {
                                "uuid": "msg-3",
                                "sender": "human",
                                "created_at": "2026-03-14T11:00:00Z",
                                "updated_at": "2026-03-14T11:00:00Z",
                                "text": duplicate_prompt,
                                "content": [{"type": "text", "text": duplicate_prompt}],
                                "attachments": [],
                                "files": [],
                            },
                            {
                                "uuid": "msg-4",
                                "sender": "assistant",
                                "created_at": "2026-03-14T11:01:00Z",
                                "updated_at": "2026-03-14T11:01:00Z",
                                "text": "I will keep parity with the richer adapter path.",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "I will keep parity with the richer adapter path.",
                                    }
                                ],
                                "attachments": [],
                                "files": [],
                            },
                        ],
                    },
                ],
            )

            result = import_claude_export_corpus(
                bundle,
                output_root,
                corpus_id="claude-history-memory",
                name="Claude History Memory",
            )

            contract = json.loads(
                (output_root / "corpus" / "contract.json").read_text(encoding="utf-8")
            )
            self.assertEqual(result["thread_count"], 2)
            self.assertEqual(contract["adapter_type"], "archive-normalized")
            self.assertEqual(contract["corpus_id"], "corpus-claude-archive")
            self.assertEqual(result["near_duplicate_count"], 0)
            self.assertFalse((output_root / "corpus" / "import-audit.json").exists())
            self.assertFalse((output_root / "corpus" / "near-duplicates.json").exists())
            manifest = load_corpus_collection(output_root / "import-manifest.json")
            self.assertEqual(len(manifest), 2)


class ExtractMessageTextTests(unittest.TestCase):
    def _message(
        self,
        *,
        text: str = "",
        content: list[dict[str, object]] | None = None,
        attachments: list[dict[str, object]] | None = None,
        files: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        return {
            "sender": "assistant",
            "text": text,
            "content": content or [],
            "attachments": attachments or [],
            "files": files or [],
        }

    def test_extracts_rich_segments_from_claude_message(self) -> None:
        message = self._message(
            text="Review the generated artifacts.",
            content=[
                {"type": "code", "language": "python", "text": "print(1)"},
                {
                    "type": "tool_result",
                    "name": "python",
                    "content": [{"type": "text", "text": "42"}],
                },
                {"type": "execution_output", "content": {"stdout": "done"}},
                {"type": "image", "width": 800, "height": 600},
            ],
            attachments=[{"file_name": "spec.pdf"}],
            files=[{"name": "notes.txt"}],
        )

        result = extract_message_text(message)

        self.assertIn("Review the generated artifacts.", result)
        self.assertIn("```python", result)
        self.assertIn("print(1)", result)
        self.assertIn("[tool_result:python]", result)
        self.assertIn("[Execution output: done]", result)
        self.assertIn("[Image: 800x600]", result)
        self.assertIn("[attachment:spec.pdf]", result)
        self.assertIn("[file:notes.txt]", result)

    def test_extracts_fallback_text_from_unknown_content(self) -> None:
        message = self._message(
            content=[{"type": "custom", "content": [{"text": "Nested text survives."}]}],
        )
        self.assertIn("Nested text survives.", extract_message_text(message))


class BuildThreadAuditTests(unittest.TestCase):
    def test_produces_audit_statistics(self) -> None:
        messages = [
            {"sender": "human", "text": "hi", "content": [{"type": "text", "text": "hi"}]},
            {
                "sender": "assistant",
                "text": "",
                "content": [{"type": "tool_result", "name": "tool", "content": "ok"}],
            },
        ]

        audit = build_thread_audit(messages, ["hi", "[tool_result:tool]"], [])

        self.assertEqual(audit["message_count"], 2)
        self.assertEqual(audit["retained_count"], 2)
        self.assertEqual(audit["raw_sender_counts"]["human"], 1)
        self.assertEqual(audit["raw_sender_counts"]["assistant"], 1)
        self.assertEqual(audit["raw_content_type_counts"]["text"], 1)
        self.assertEqual(audit["raw_content_type_counts"]["tool_result"], 1)
        self.assertIsInstance(audit["quality_flags"], list)


class DetectNearDuplicatesTests(unittest.TestCase):
    def test_detects_identical_prompts(self) -> None:
        threads = [
            {"thread_uid": "t1", "title_normalized": "Claude A"},
            {"thread_uid": "t2", "title_normalized": "Claude B"},
        ]
        prompts = {
            "t1": "Please help me build a recursive engine for symbolic computing",
            "t2": "Please help me build a recursive engine for symbolic computing",
        }

        result = detect_near_duplicates(threads, prompts)

        self.assertEqual(len(result), 1)
        self.assertGreaterEqual(result[0]["similarity"], 0.92)

    def test_ignores_short_prompts(self) -> None:
        threads = [
            {"thread_uid": "t1", "title_normalized": "A"},
            {"thread_uid": "t2", "title_normalized": "B"},
        ]
        prompts = {"t1": "hi", "t2": "hi"}

        self.assertEqual(detect_near_duplicates(threads, prompts), [])


from conversation_corpus_engine.import_claude_export_corpus import (  # noqa: E402
    discover_bundle_roots,
    resolve_bundle_root,
)


def _write_claude_bundle(root: Path, conversations: list[dict[str, object]]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "users.json").write_text(
        json.dumps([{"uuid": "user-1", "full_name": "Test User"}]), encoding="utf-8"
    )
    (root / "projects.json").write_text("[]", encoding="utf-8")
    (root / "memories.json").write_text("[]", encoding="utf-8")
    (root / "conversations.json").write_text(json.dumps(conversations), encoding="utf-8")


def _claude_conv(uuid: str, name: str, prompt: str) -> dict[str, object]:
    return {
        "uuid": uuid,
        "name": name,
        "summary": "",
        "created_at": "2026-03-14T10:00:00Z",
        "updated_at": "2026-03-14T10:05:00Z",
        "chat_messages": [
            {
                "uuid": f"{uuid}-msg-1",
                "sender": "human",
                "created_at": "2026-03-14T10:00:00Z",
                "updated_at": "2026-03-14T10:00:00Z",
                "text": prompt,
                "content": [{"type": "text", "text": prompt}],
                "attachments": [],
                "files": [],
            },
            {
                "uuid": f"{uuid}-msg-2",
                "sender": "assistant",
                "created_at": "2026-03-14T10:01:00Z",
                "updated_at": "2026-03-14T10:01:00Z",
                "text": "Acknowledged: " + prompt,
                "content": [{"type": "text", "text": "Acknowledged: " + prompt}],
                "attachments": [],
                "files": [],
            },
        ],
    }


class DiscoverBundleRootsTests(unittest.TestCase):
    def test_single_bundle_directory_returns_one_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bundle = Path(tmpdir) / "export"
            _write_claude_bundle(bundle, [])
            self.assertEqual(discover_bundle_roots(bundle), [bundle.resolve()])

    def test_file_input_returns_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bundle = Path(tmpdir) / "export"
            _write_claude_bundle(bundle, [])
            self.assertEqual(
                discover_bundle_roots(bundle / "conversations.json"), [bundle.resolve()]
            )

    def test_multi_part_returns_sorted_subdirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir) / "split-export"
            for name in ("part-3", "part-1", "part-2"):
                _write_claude_bundle(parent / name, [])
            result = discover_bundle_roots(parent)
            self.assertEqual([p.name for p in result], ["part-1", "part-2", "part-3"])

    def test_missing_files_raises_with_clear_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            empty = Path(tmpdir) / "nothing"
            empty.mkdir()
            with self.assertRaises(FileNotFoundError) as ctx:
                discover_bundle_roots(empty)
            self.assertIn("conversations.json", str(ctx.exception))
            self.assertIn("multi-part", str(ctx.exception))

    def test_directory_with_bundle_takes_precedence_over_subdirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir) / "ambiguous"
            _write_claude_bundle(parent, [])
            _write_claude_bundle(parent / "stray-subdir", [])
            self.assertEqual(discover_bundle_roots(parent), [parent.resolve()])

    def test_resolve_bundle_root_still_works_for_back_compat(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bundle = Path(tmpdir) / "export"
            _write_claude_bundle(bundle, [])
            self.assertEqual(resolve_bundle_root(bundle), bundle.resolve())


class ImportMultiPartCorpusTests(unittest.TestCase):
    def test_multi_part_concatenates_and_dedupes_by_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir) / "split-export"
            output = Path(tmpdir) / "out"

            _write_claude_bundle(
                parent / "part-1",
                [
                    _claude_conv("conv-A", "Alpha thread", "Configure A please"),
                    _claude_conv("conv-B", "Beta thread", "Walk me through B"),
                ],
            )
            _write_claude_bundle(
                parent / "part-2",
                [
                    # conv-B duplicated across parts — must be skipped
                    _claude_conv("conv-B", "Beta thread", "Walk me through B"),
                    _claude_conv("conv-C", "Gamma thread", "Explain gamma"),
                ],
            )

            result = import_claude_export_corpus(
                parent, output, corpus_id="claude-multi-part-test", name="Claude Multi"
            )

            self.assertEqual(result["bundle_part_count"], 2)
            self.assertEqual(result["bundle_part_names"], ["part-1", "part-2"])
            self.assertEqual(result["duplicate_conversations_skipped"], 1)
            self.assertEqual(result["thread_count"], 3, "Expected 3 unique threads after dedup")

            self.assertTrue(
                collection_storage_path(
                    output / "source" / "part-1" / "conversations.json"
                ).exists()
            )
            self.assertTrue(
                collection_storage_path(
                    output / "source" / "part-2" / "conversations.json"
                ).exists()
            )

            readme_text = (output / "README.md").read_text(encoding="utf-8")
            self.assertIn("Bundle parts: 2", readme_text)
            self.assertIn("part-1", readme_text)

    def test_single_bundle_keeps_flat_source_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bundle = Path(tmpdir) / "export"
            output = Path(tmpdir) / "out"
            _write_claude_bundle(bundle, [_claude_conv("conv-X", "Solo", "Just one bundle")])

            result = import_claude_export_corpus(bundle, output, corpus_id="claude-single-test")

            self.assertEqual(result["bundle_part_count"], 1)
            self.assertEqual(result["duplicate_conversations_skipped"], 0)
            self.assertTrue(
                collection_storage_path(output / "source" / "conversations.json").exists()
            )
            self.assertFalse((output / "source" / "export").exists())


if __name__ == "__main__":
    unittest.main()
