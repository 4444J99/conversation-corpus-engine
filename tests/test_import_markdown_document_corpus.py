from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conversation_corpus_engine.import_markdown_document_corpus import (
    import_markdown_document_corpus,
)
from conversation_corpus_engine.sharded_collection import (
    collection_storage_path,
    load_corpus_collection,
)


class ImportMarkdownDocumentCorpusTests(unittest.TestCase):
    def test_import_creates_minimal_federation_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_root = Path(tmpdir) / "input"
            output_root = Path(tmpdir) / "output"
            input_root.mkdir(parents=True, exist_ok=True)
            (input_root / "Plan.md").write_text(
                "\n".join(
                    [
                        "# Implementation Plan",
                        "",
                        "We need to build a queue and should add a notifier.",
                        "Maybe the worker model needs another pass?",
                    ],
                ),
                encoding="utf-8",
            )

            result = import_markdown_document_corpus(
                input_root, output_root, corpus_id="document-memory"
            )

            self.assertEqual(result["thread_count"], 1)
            for filename in (
                "threads-index.json",
                "semantic-v3-index.json",
                "pairs-index.json",
                "doctrine-briefs.json",
                "family-dossiers.json",
            ):
                self.assertTrue(collection_storage_path(output_root / "corpus" / filename).exists())
            self.assertTrue((output_root / "corpus" / "contract.json").exists())
            self.assertTrue((output_root / "corpus" / "source-snapshot.json").exists())
            self.assertTrue((output_root / "corpus" / "regression-gates.json").exists())

            contract = json.loads(
                (output_root / "corpus" / "contract.json").read_text(encoding="utf-8")
            )
            self.assertEqual(contract["adapter_type"], "markdown-document")
            self.assertTrue(contract["source_signature_fingerprint"])

            actions = load_corpus_collection(output_root / "corpus" / "action-ledger.json")
            unresolved = load_corpus_collection(output_root / "corpus" / "unresolved-ledger.json")
            self.assertGreaterEqual(len(actions), 1)
            self.assertGreaterEqual(len(unresolved), 1)

    def test_import_honors_max_depth_for_top_level_collection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_root = Path(tmpdir) / "input"
            output_root = Path(tmpdir) / "output"
            nested_root = input_root / "nested"
            nested_root.mkdir(parents=True, exist_ok=True)
            (input_root / "TopLevel.md").write_text(
                "# Top Level\n\nBuild the top level index.", encoding="utf-8"
            )
            (nested_root / "Nested.md").write_text(
                "# Nested\n\nBuild the nested index.", encoding="utf-8"
            )

            result = import_markdown_document_corpus(
                input_root,
                output_root,
                corpus_id="document-memory",
                max_depth=1,
            )

            self.assertEqual(result["thread_count"], 1)
            manifest = load_corpus_collection(output_root / "import-manifest.json")
            self.assertEqual(len(manifest), 1)
            self.assertIn("TopLevel.md", manifest[0]["source_markdown"])

            contract = json.loads(
                (output_root / "corpus" / "contract.json").read_text(encoding="utf-8")
            )
            self.assertEqual(contract["collection_scope"], "top-level")

    def test_import_filters_noise_and_reduces_unresolved_overreach(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_root = Path(tmpdir) / "input"
            output_root = Path(tmpdir) / "output"
            input_root.mkdir(parents=True, exist_ok=True)
            (input_root / "Noisy.md").write_text(
                "\n".join(
                    [
                        "## Q:",
                        "]||||/\\\\vvvv//\\\\.../\\\\/\\\\---/\\\\/\\\\.../\\\\\\\\vvvv//||||[",
                        "## A:",
                        "# Master Plan",
                        "| Phase | Status | Owner |",
                        "| --- | --- | --- |",
                        "| 0-6 hrs | Empty | You |",
                        "We need to build the canonical queue before launch.",
                        "Maybe the ontology still needs another review?",
                        "1. Click your GitHub org link",
                        "2. Scan 3-5 repository READMEs",
                    ],
                ),
                encoding="utf-8",
            )

            import_markdown_document_corpus(input_root, output_root, corpus_id="document-memory")

            actions = load_corpus_collection(output_root / "corpus" / "action-ledger.json")
            unresolved = load_corpus_collection(output_root / "corpus" / "unresolved-ledger.json")
            threads = load_corpus_collection(output_root / "corpus" / "threads-index.json")

            self.assertEqual(len(actions), 1)
            self.assertEqual(
                actions[0]["canonical_action"],
                "We need to build the canonical queue before launch.",
            )
            self.assertEqual(len(unresolved), 1)
            self.assertEqual(
                unresolved[0]["canonical_question"],
                "Maybe the ontology still needs another review?",
            )
            self.assertEqual(threads[0]["title_normalized"], "Master Plan")


if __name__ == "__main__":
    unittest.main()
