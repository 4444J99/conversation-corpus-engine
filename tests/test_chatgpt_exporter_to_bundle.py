from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from chatgpt_exporter_to_bundle import (  # noqa: E402
    _conv_id_from_link,
    _normalize_role,
    _parse_date,
    convert_directory,
    convert_one,
    discover_input_files,
)

from conversation_corpus_engine.import_chatgpt_export_corpus import (  # noqa: E402
    import_chatgpt_export_corpus,
)
from conversation_corpus_engine.sharded_collection import collection_storage_path  # noqa: E402


def _exporter_doc(title: str, link: str, messages: list[dict]) -> dict:
    return {
        "metadata": {
            "title": title,
            "user": {"name": "", "email": ""},
            "dates": {
                "created": "1/3/2026 7:53:47",
                "updated": "1/3/2026 8:00:00",
                "exported": "1/3/2026 8:01:00",
            },
            "link": link,
            "powered_by": "ChatGPT Exporter (https://www.chatgptexporter.com)",
        },
        "messages": messages,
    }


class HelpersTests(unittest.TestCase):
    def test_parse_date_known_format(self) -> None:
        ts = _parse_date("1/3/2026 7:53:47")
        self.assertIsNotNone(ts)
        self.assertGreater(ts, 0)

    def test_parse_date_handles_empty(self) -> None:
        self.assertIsNone(_parse_date(None))
        self.assertIsNone(_parse_date(""))
        self.assertIsNone(_parse_date("definitely not a date"))

    def test_conv_id_from_link_extracts_uuid(self) -> None:
        link = "https://chatgpt.com/c/695910b8-1700-832d-a078-0f59f65163f8"
        self.assertEqual(_conv_id_from_link(link, "fb"), "695910b8-1700-832d-a078-0f59f65163f8")

    def test_conv_id_falls_back_when_no_link(self) -> None:
        cid = _conv_id_from_link(None, "fallback-name")
        self.assertEqual(len(cid), 36)
        # Same fallback always yields same id
        self.assertEqual(cid, _conv_id_from_link("", "fallback-name"))

    def test_normalize_role(self) -> None:
        self.assertEqual(_normalize_role("user"), "user")
        self.assertEqual(_normalize_role("Human"), "user")
        self.assertEqual(_normalize_role("ChatGPT"), "assistant")
        self.assertEqual(_normalize_role("AI"), "assistant")
        self.assertEqual(_normalize_role(None), "user")
        self.assertEqual(_normalize_role("weird"), "user")


class ConvertOneTests(unittest.TestCase):
    def test_builds_linear_mapping_tree(self) -> None:
        doc = _exporter_doc(
            "Test Title",
            "https://chatgpt.com/c/abcdef01-2345-6789-abcd-ef0123456789",
            [
                {"role": "user", "say": "Hello", "time": "10:00"},
                {"role": "assistant", "say": "Hi back", "time": "10:01"},
                {"role": "user", "say": "Tell me more", "time": "10:02"},
            ],
        )
        conv = convert_one(doc, fallback_id="testfile")
        self.assertEqual(conv["title"], "Test Title")
        self.assertEqual(conv["conversation_id"], "abcdef01-2345-6789-abcd-ef0123456789")

        mapping = conv["mapping"]
        # 1 root + 3 message nodes
        self.assertEqual(len(mapping), 4)

        # Find root
        roots = [n for n in mapping.values() if n["parent"] is None]
        self.assertEqual(len(roots), 1)
        root = roots[0]
        self.assertIsNone(root["message"])
        self.assertEqual(len(root["children"]), 1)

        # Walk chain
        cur = mapping[root["children"][0]]
        self.assertEqual(cur["message"]["author"]["role"], "user")
        self.assertEqual(cur["message"]["content"]["parts"], ["Hello"])
        cur = mapping[cur["children"][0]]
        self.assertEqual(cur["message"]["author"]["role"], "assistant")
        cur = mapping[cur["children"][0]]
        self.assertEqual(cur["message"]["author"]["role"], "user")
        self.assertEqual(cur["children"], [])

    def test_skips_empty_messages(self) -> None:
        doc = _exporter_doc(
            "Edgy",
            "https://chatgpt.com/c/aaaabbbb-cccc-dddd-eeee-ffff00001111",
            [{"role": "user", "say": "  ", "time": "x"}],
        )
        conv = convert_one(doc, fallback_id="edge")
        # Only root, no message nodes (all messages were empty)
        non_root = [n for n in conv["mapping"].values() if n["message"] is not None]
        self.assertEqual(non_root, [])

    def test_default_title_when_missing(self) -> None:
        doc = {"metadata": {}, "messages": [{"role": "user", "say": "hi", "time": ""}]}
        conv = convert_one(doc, fallback_id="x")
        self.assertEqual(conv["title"], "Untitled")


class ConvertDirectoryTests(unittest.TestCase):
    def _write(self, dir: Path, name: str, doc: dict) -> Path:
        dir.mkdir(parents=True, exist_ok=True)
        p = dir / name
        p.write_text(json.dumps(doc), encoding="utf-8")
        return p

    def test_dedupes_by_conversation_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "in"
            output = Path(tmpdir) / "out"
            doc1 = _exporter_doc(
                "Same",
                "https://chatgpt.com/c/11111111-2222-3333-4444-555555555555",
                [{"role": "user", "say": "hi", "time": ""}],
            )
            self._write(input_dir, "ChatGPT-Same.json", doc1)
            self._write(input_dir, "ChatGPT-Same (1).json", doc1)
            doc2 = _exporter_doc(
                "Different",
                "https://chatgpt.com/c/99999999-8888-7777-6666-555555555555",
                [{"role": "user", "say": "hello", "time": ""}],
            )
            self._write(input_dir, "ChatGPT-Different.json", doc2)

            result = convert_directory(input_dir, output)
            self.assertEqual(result["files_scanned"], 3)
            self.assertEqual(result["conversations_written"], 2)
            self.assertEqual(result["skipped_duplicate"], 1)

            # Bundle is valid
            convs = json.loads((output / "conversations.json").read_text())
            self.assertEqual(len(convs), 2)
            self.assertTrue((output / "user.json").exists())

    def test_skips_files_with_no_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "in"
            output = Path(tmpdir) / "out"
            doc = _exporter_doc(
                "Empty",
                "https://chatgpt.com/c/deadbeef-0000-1111-2222-333344445555",
                [{"role": "user", "say": "", "time": ""}],
            )
            self._write(input_dir, "ChatGPT-Empty.json", doc)
            result = convert_directory(input_dir, output)
            self.assertEqual(result["skipped_empty"], 1)
            self.assertEqual(result["conversations_written"], 0)

    def test_raises_on_no_input_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "in"
            input_dir.mkdir()
            output = Path(tmpdir) / "out"
            with self.assertRaises(FileNotFoundError):
                convert_directory(input_dir, output)

    def test_discover_only_matches_chatgpt_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "in"
            input_dir.mkdir()
            (input_dir / "ChatGPT-Yes.json").write_text("{}")
            (input_dir / "Other.json").write_text("{}")
            (input_dir / "ChatGPT-Also.json").write_text("{}")
            files = discover_input_files(input_dir)
            self.assertEqual([f.name for f in files], ["ChatGPT-Also.json", "ChatGPT-Yes.json"])


class EndToEndIntegrationTests(unittest.TestCase):
    """Verify the converter's bundle is ingestible by the standard adapter."""

    def test_converter_output_imports_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "exports"
            input_dir.mkdir()
            doc = _exporter_doc(
                "Integration Test",
                "https://chatgpt.com/c/cafebabe-1234-5678-90ab-cdef00112233",
                [
                    {"role": "user", "say": "What is the capital of France?", "time": ""},
                    {"role": "assistant", "say": "Paris.", "time": ""},
                    {
                        "role": "user",
                        "say": "Tell me one historical fact about it that surprises tourists.",
                        "time": "",
                    },
                    {
                        "role": "assistant",
                        "say": "The Louvre was originally a fortress built in 1190.",
                        "time": "",
                    },
                ],
            )
            (input_dir / "ChatGPT-Integration.json").write_text(json.dumps(doc))

            bundle = Path(tmpdir) / "bundle"
            output = Path(tmpdir) / "corpus"

            convert_directory(input_dir, bundle)
            result = import_chatgpt_export_corpus(bundle, output, corpus_id="integration-test")

            self.assertEqual(result["thread_count"], 1)
            self.assertGreaterEqual(result["pair_count"], 1)
            # Federation artifacts produced
            self.assertTrue(
                collection_storage_path(output / "corpus" / "threads-index.json").exists()
            )
            self.assertTrue(
                collection_storage_path(output / "corpus" / "pairs-index.json").exists()
            )
            self.assertTrue((output / "corpus" / "contract.json").exists())


if __name__ == "__main__":
    unittest.main()
