from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conversation_corpus_engine.corpus_store import register_corpus_store  # noqa: E402
from conversation_corpus_engine.import_chatgpt_export_corpus import (  # noqa: E402
    import_chatgpt_export_corpus,
)
from conversation_corpus_engine.provider_exports import looks_like_chatgpt_export  # noqa: E402
from conversation_corpus_engine.sharded_collection import (  # noqa: E402
    collection_storage_path,
    load_corpus_collection,
)

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "chatgpt-export"


class ChatGPTDetectionTests(unittest.TestCase):
    def test_looks_like_chatgpt_export_identifies_fixture(self) -> None:
        self.assertTrue(looks_like_chatgpt_export(FIXTURE_ROOT))

    def test_looks_like_chatgpt_export_rejects_empty_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertFalse(looks_like_chatgpt_export(Path(tmpdir)))

    def test_looks_like_chatgpt_export_rejects_claude_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "conversations.json").write_text("[]", encoding="utf-8")
            (root / "users.json").write_text("[]", encoding="utf-8")
            # Claude bundles have users.json (plural), ChatGPT has user.json (singular)
            self.assertFalse(looks_like_chatgpt_export(root))


class ImportChatGPTExportCorpusTests(unittest.TestCase):
    def test_import_chatgpt_export_builds_federation_surface(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir) / "chatgpt-history-memory"
            result = import_chatgpt_export_corpus(
                FIXTURE_ROOT,
                output_root,
                corpus_id="chatgpt-history-memory",
                name="ChatGPT History Memory",
            )

            self.assertEqual(result["corpus_id"], "chatgpt-history-memory")
            self.assertEqual(result["name"], "ChatGPT History Memory")
            # 3 conversations in fixture, but conv 3 has no assistant response
            # so it should still import (single user message is valid)
            self.assertGreaterEqual(result["thread_count"], 2)
            self.assertGreaterEqual(result["pair_count"], 1)

            # Verify contract.json was written
            contract = json.loads(
                (output_root / "corpus" / "contract.json").read_text(encoding="utf-8"),
            )
            self.assertEqual(contract["adapter_type"], "archive-normalized")
            self.assertEqual(contract["corpus_id"], "corpus-chatgpt-archive")
            self.assertEqual(contract["contract_name"], "conversation-corpus-engine.v2")
            self.assertEqual(result["projector_corpus_id"], "corpus-chatgpt-archive")

            # Verify federation-required files exist
            corpus_dir = output_root / "corpus"
            for required in (
                "threads-index.collection",
                "pairs-index.collection",
                "doctrine-briefs.collection",
                "canonical-families.collection",
                "evaluation-summary.json",
                "regression-gates.json",
            ):
                self.assertTrue(
                    (corpus_dir / required).exists(),
                    f"Missing required corpus artifact: {required}",
                )

    def test_import_chatgpt_handles_null_title_and_null_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir) / "chatgpt-memory"
            import_chatgpt_export_corpus(
                FIXTURE_ROOT,
                output_root,
                corpus_id="chatgpt-memory",
                name="ChatGPT Memory",
            )
            threads = load_corpus_collection(output_root / "corpus" / "threads-index.json")
            # At least one thread should exist even from the null-title conversation
            titles = [t["title_normalized"] for t in threads]
            # The null-title conversation should have an inferred title
            self.assertTrue(all(isinstance(t, str) and len(t) > 0 for t in titles))


from conversation_corpus_engine.provider_catalog import PROVIDER_CONFIG  # noqa: E402
from conversation_corpus_engine.provider_discovery import discover_provider_uploads  # noqa: E402
from conversation_corpus_engine.provider_import import import_provider_corpus  # noqa: E402


class ChatGPTProviderIntegrationTests(unittest.TestCase):
    def test_chatgpt_in_provider_config(self) -> None:
        self.assertIn("chatgpt", PROVIDER_CONFIG)
        self.assertEqual(PROVIDER_CONFIG["chatgpt"]["adapter_type"], "chatgpt-export")

    def test_discover_chatgpt_upload_in_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "project"
            source_drop_root = Path(tmpdir) / "source-drop"
            inbox = source_drop_root / "chatgpt" / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            shutil.copy2(
                FIXTURE_ROOT / "conversations.json",
                inbox / "conversations.json",
            )
            shutil.copy2(
                FIXTURE_ROOT / "user.json",
                inbox / "user.json",
            )
            payload = discover_provider_uploads(project_root, source_drop_root)
            chatgpt = next(item for item in payload["providers"] if item["provider"] == "chatgpt")
            self.assertEqual(chatgpt["upload_state"], "ready")

    def test_import_provider_corpus_routes_chatgpt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "project"
            project_root.mkdir()
            corpus_store_root = Path(tmpdir) / "corpus-store"
            corpus_store_root.mkdir()
            register_corpus_store(project_root, corpus_store_root)
            source_path = FIXTURE_ROOT
            output_root = corpus_store_root / "chatgpt-history-memory"
            result = import_provider_corpus(
                project_root=project_root,
                provider="chatgpt",
                source_path=source_path,
                output_root=output_root,
                corpus_store_root=corpus_store_root,
                bootstrap_eval=False,
            )
            self.assertEqual(result["provider"], "chatgpt")
            self.assertGreaterEqual(result["import_result"]["thread_count"], 2)


from conversation_corpus_engine.corpus_text import detect_near_duplicates  # noqa: E402
from conversation_corpus_engine.import_chatgpt_export_corpus import (  # noqa: E402
    build_thread_audit,
    extract_node_text,
)


class ExtractNodeTextTests(unittest.TestCase):
    def _node(self, content: dict, role: str = "assistant") -> dict:
        return {"message": {"author": {"role": role}, "content": content}}

    def test_extracts_plain_text(self) -> None:
        node = self._node({"parts": ["hello world"], "content_type": "text"})
        self.assertEqual(extract_node_text(node), "hello world")

    def test_extracts_code_block(self) -> None:
        node = self._node(
            {"content_type": "code", "text": "print(1)", "language": "python", "parts": []}
        )
        result = extract_node_text(node)
        self.assertIn("```python", result)
        self.assertIn("print(1)", result)

    def test_extracts_execution_output(self) -> None:
        node = self._node({"content_type": "execution_output", "text": "42\n", "parts": []})
        result = extract_node_text(node)
        self.assertIn("[Execution output:", result)
        self.assertIn("42", result)

    def test_truncates_long_execution_output(self) -> None:
        node = self._node({"content_type": "execution_output", "text": "x" * 600, "parts": []})
        result = extract_node_text(node)
        self.assertIn("...", result)
        self.assertLess(len(result), 600)

    def test_handles_multimodal_with_image(self) -> None:
        node = self._node(
            {
                "content_type": "multimodal_text",
                "parts": [
                    "Check this out",
                    {"content_type": "image_asset_pointer", "width": 800, "height": 600},
                ],
            }
        )
        result = extract_node_text(node)
        self.assertIn("Check this out", result)
        self.assertIn("[Image: 800x600]", result)

    def test_skips_editable_context(self) -> None:
        node = self._node({"content_type": "user_editable_context", "parts": ["system prompt"]})
        self.assertEqual(extract_node_text(node), "")

    def test_skips_thoughts(self) -> None:
        node = self._node({"content_type": "thoughts", "parts": ["internal reasoning"]})
        self.assertEqual(extract_node_text(node), "")

    def test_tool_output_fallback(self) -> None:
        node = self._node(
            {"content_type": "text", "text": "tool result data", "parts": []}, role="tool"
        )
        result = extract_node_text(node)
        self.assertIn("tool result data", result)

    def test_empty_message_returns_empty(self) -> None:
        self.assertEqual(extract_node_text({}), "")
        self.assertEqual(extract_node_text({"message": None}), "")


class BuildThreadAuditTests(unittest.TestCase):
    def test_produces_audit_with_quality_flags(self) -> None:
        mapping = {
            "n1": {
                "message": {
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["hi"]},
                }
            },
            "n2": {
                "message": {
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": ["hello"]},
                }
            },
            "n3": {
                "message": {
                    "author": {"role": "system"},
                    "content": {"content_type": "text", "parts": ["sys"]},
                }
            },
        }
        nodes = [mapping["n1"], mapping["n2"]]
        audit = build_thread_audit(mapping, nodes, ["hi", "hello"], [])
        self.assertEqual(audit["mapping_node_count"], 3)
        self.assertEqual(audit["path_node_count"], 2)
        self.assertEqual(audit["retained_count"], 2)
        self.assertGreater(audit["retention_ratio"], 0)
        self.assertIsInstance(audit["quality_flags"], list)


class DetectNearDuplicatesTests(unittest.TestCase):
    def test_detects_identical_prompts(self) -> None:
        threads = [
            {"thread_uid": "t1", "title_normalized": "Chat A"},
            {"thread_uid": "t2", "title_normalized": "Chat B"},
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
        result = detect_near_duplicates(threads, prompts)
        self.assertEqual(len(result), 0)

    def test_no_duplicates_for_different_prompts(self) -> None:
        threads = [
            {"thread_uid": "t1", "title_normalized": "A"},
            {"thread_uid": "t2", "title_normalized": "B"},
        ]
        prompts = {
            "t1": "Build a chess engine with neural network evaluation",
            "t2": "Design a garden irrigation system with soil sensors",
        }
        result = detect_near_duplicates(threads, prompts)
        self.assertEqual(len(result), 0)


from conversation_corpus_engine.import_chatgpt_export_corpus import (  # noqa: E402
    discover_bundle_roots,
    resolve_bundle_root,
)


class DiscoverBundleRootsTests(unittest.TestCase):
    def _write_minimal_bundle(self, root: Path, *, conversations: list[dict] | None = None) -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / "conversations.json").write_text(json.dumps(conversations or []), encoding="utf-8")
        (root / "user.json").write_text("{}", encoding="utf-8")

    def test_single_bundle_directory_returns_one_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bundle = Path(tmpdir) / "export"
            self._write_minimal_bundle(bundle)
            result = discover_bundle_roots(bundle)
            self.assertEqual(result, [bundle.resolve()])

    def test_file_input_returns_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bundle = Path(tmpdir) / "export"
            self._write_minimal_bundle(bundle)
            result = discover_bundle_roots(bundle / "conversations.json")
            self.assertEqual(result, [bundle.resolve()])

    def test_multi_part_returns_sorted_subdirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir) / "split-export"
            for name in ("part-3", "part-1", "part-2"):
                self._write_minimal_bundle(parent / name)
            result = discover_bundle_roots(parent)
            self.assertEqual(
                [p.name for p in result],
                ["part-1", "part-2", "part-3"],
                "Multi-part discovery must return parts in sorted order",
            )

    def test_missing_files_raises_with_clear_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            empty = Path(tmpdir) / "nothing"
            empty.mkdir()
            with self.assertRaises(FileNotFoundError) as ctx:
                discover_bundle_roots(empty)
            self.assertIn("conversations.json", str(ctx.exception))
            self.assertIn("multi-part", str(ctx.exception))

    def test_directory_with_bundle_takes_precedence_over_subdirs(self) -> None:
        # If a dir is itself a valid bundle AND has bundle subdirs, the dir wins.
        # Avoids accidental multi-part interpretation of single bundles that happen
        # to contain incidental subfolders.
        with tempfile.TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir) / "ambiguous"
            self._write_minimal_bundle(parent)
            self._write_minimal_bundle(parent / "stray-subdir")
            result = discover_bundle_roots(parent)
            self.assertEqual(result, [parent.resolve()])

    def test_resolve_bundle_root_still_works_for_back_compat(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bundle = Path(tmpdir) / "export"
            self._write_minimal_bundle(bundle)
            self.assertEqual(resolve_bundle_root(bundle), bundle.resolve())


class ImportMultiPartCorpusTests(unittest.TestCase):
    def _write_bundle_with_conversations(self, root: Path, conversations: list[dict]) -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / "conversations.json").write_text(json.dumps(conversations), encoding="utf-8")
        (root / "user.json").write_text(
            json.dumps({"id": "user-test", "email": "test@example.com"}), encoding="utf-8"
        )

    @staticmethod
    def _conv(uid: str, title: str, prompt: str) -> dict:
        return {
            "title": title,
            "create_time": 1700000000,
            "update_time": 1700000100,
            "conversation_id": uid,
            "mapping": {
                "n1": {
                    "id": "n1",
                    "parent": None,
                    "children": ["n2"],
                    "message": None,
                },
                "n2": {
                    "id": "n2",
                    "parent": "n1",
                    "children": ["n3"],
                    "message": {
                        "id": "n2",
                        "author": {"role": "user"},
                        "create_time": 1700000010,
                        "content": {"content_type": "text", "parts": [prompt]},
                    },
                },
                "n3": {
                    "id": "n3",
                    "parent": "n2",
                    "children": [],
                    "message": {
                        "id": "n3",
                        "author": {"role": "assistant"},
                        "create_time": 1700000020,
                        "content": {"content_type": "text", "parts": ["Reply: " + prompt]},
                    },
                },
            },
        }

    def test_multi_part_concatenates_and_dedupes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir) / "split-export"
            output = Path(tmpdir) / "out"

            self._write_bundle_with_conversations(
                parent / "part-1",
                [
                    self._conv("conv-A", "Alpha thread", "How do I configure A?"),
                    self._conv("conv-B", "Beta thread", "Walk me through B setup"),
                ],
            )
            self._write_bundle_with_conversations(
                parent / "part-2",
                [
                    # conv-B is duplicated across parts and must be skipped
                    self._conv("conv-B", "Beta thread", "Walk me through B setup"),
                    self._conv("conv-C", "Gamma thread", "Explain the gamma topic"),
                ],
            )

            result = import_chatgpt_export_corpus(
                parent, output, corpus_id="multi-part-test", name="Multi-Part Test"
            )

            self.assertEqual(result["bundle_part_count"], 2)
            self.assertEqual(result["bundle_part_names"], ["part-1", "part-2"])
            self.assertEqual(result["duplicate_conversations_skipped"], 1)
            self.assertEqual(result["thread_count"], 3, "Expected 3 unique threads after dedup")

            # Sources copied under prefixed subdirectories
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

            # README mentions multi-part
            readme_text = (output / "README.md").read_text(encoding="utf-8")
            self.assertIn("Bundle parts: 2", readme_text)
            self.assertIn("part-1", readme_text)
            self.assertIn("part-2", readme_text)

    def test_single_bundle_keeps_flat_source_layout(self) -> None:
        # Backwards compat: single-bundle imports must NOT prefix source/ with a part name.
        with tempfile.TemporaryDirectory() as tmpdir:
            bundle = Path(tmpdir) / "export"
            output = Path(tmpdir) / "out"
            self._write_bundle_with_conversations(
                bundle, [self._conv("conv-X", "Solo", "Just one bundle")]
            )

            result = import_chatgpt_export_corpus(bundle, output, corpus_id="single-test")

            self.assertEqual(result["bundle_part_count"], 1)
            self.assertEqual(result["duplicate_conversations_skipped"], 0)
            # Source files at top level of source/, NOT under source/<name>/
            self.assertTrue(
                collection_storage_path(output / "source" / "conversations.json").exists()
            )
            self.assertFalse((output / "source" / "export").exists())


if __name__ == "__main__":
    unittest.main()
