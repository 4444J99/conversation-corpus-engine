from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conversation_corpus_engine.import_document_export_corpus import import_document_export_corpus
from conversation_corpus_engine.sharded_collection import load_corpus_collection


class ImportDocumentExportCorpusTests(unittest.TestCase):
    def test_import_directory_bundle_writes_provider_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bundle_root = Path(tmpdir) / "perplexity-export"
            output_root = Path(tmpdir) / "perplexity-history-memory"
            bundle_root.mkdir(parents=True, exist_ok=True)
            (bundle_root / "overview.txt").write_text(
                "We should build the Perplexity adapter next.\nMaybe revisit routing later?\n",
                encoding="utf-8",
            )
            (bundle_root / "session.json").write_text(
                json.dumps(
                    {
                        "title": "Perplexity Session",
                        "query": "What changed?",
                        "answer": "Implement the calibration adapter.",
                    },
                ),
                encoding="utf-8",
            )

            result = import_document_export_corpus(
                bundle_root,
                output_root,
                provider_slug="perplexity",
                corpus_id="perplexity-history-memory",
                name="Perplexity History Memory",
            )

            contract = json.loads(
                (output_root / "corpus" / "contract.json").read_text(encoding="utf-8")
            )
            snapshot = json.loads(
                (output_root / "corpus" / "source-snapshot.json").read_text(encoding="utf-8")
            )
            manifest = load_corpus_collection(output_root / "import-manifest.json")

            self.assertEqual(contract["adapter_type"], "perplexity-export")
            self.assertEqual(contract["corpus_id"], "perplexity-history-memory")
            self.assertEqual(contract["collection_scope"], "export-bundle")
            self.assertEqual(snapshot["adapter_type"], "perplexity-export")
            self.assertEqual(result["source_file_count"], 2)
            self.assertEqual(len(manifest), 2)
            self.assertTrue((output_root / "raw-source" / "overview.txt").exists())
            self.assertTrue((output_root / "raw-source" / "session.json").exists())

    def test_import_zip_bundle_extracts_supported_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = Path(tmpdir) / "perplexity-export.zip"
            output_root = Path(tmpdir) / "perplexity-history-memory"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("export/notes.md", "# Notes\n\nBuild the importer.\n")

            result = import_document_export_corpus(
                archive_path, output_root, provider_slug="perplexity"
            )
            contract = json.loads(
                (output_root / "corpus" / "contract.json").read_text(encoding="utf-8")
            )

            self.assertEqual(result["archive_type"], "zip")
            self.assertEqual(contract["source_archive_type"], "zip")
            self.assertEqual(contract["adapter_type"], "perplexity-export")


if __name__ == "__main__":
    unittest.main()
