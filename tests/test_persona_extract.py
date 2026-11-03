from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from conversation_corpus_engine.persona_extract import (
    extract_persona_lexicon,
    render_persona_extract_markdown,
    write_persona_extract_artifacts,
)


def _write_transcript(root: Path, name: str, turns: list[dict[str, str]]) -> Path:
    path = root / name
    path.write_text(json.dumps(turns), encoding="utf-8")
    return path


class PersonaExtractTests(unittest.TestCase):
    def test_no_source_returns_curated_scaffold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = extract_persona_lexicon(Path(tmp), "claude")
        self.assertEqual(payload["status"], "VACUUM (SCAFFOLDED)")
        self.assertEqual(payload["yearning_method"], "curated")
        self.assertIn("real participant", payload["ideal_yearning"])
        self.assertEqual(payload["archetypal_pattern"], "Initiation Architect")
        self.assertEqual(payload["vocabulary"], [])

    def test_missing_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = extract_persona_lexicon(Path(tmp), "rob", source_corpus=Path(tmp) / "nope")
        self.assertEqual(payload["status"], "VACUUM (SOURCE NOT FOUND)")

    def test_legacy_extracts_human_persona_lexicon(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "transcripts"
            source.mkdir()
            _write_transcript(
                source,
                "session.json",
                [
                    {"role": "user", "text": "Build the cathedral. Build it carefully."},
                    {"role": "assistant", "text": "Acknowledged, deploying the scaffold."},
                    {"role": "user", "text": "The cathedral needs a foundation first."},
                ],
            )
            payload = extract_persona_lexicon(
                Path(tmp), "rob", source_corpus=source, mode="legacy-role"
            )

        self.assertEqual(payload["status"], "EXTRACTED")
        terms = {entry["term"]: entry for entry in payload["vocabulary"]}
        self.assertIn("cathedral", terms)
        self.assertEqual(terms["cathedral"]["count"], 2)
        # "cathedral" is unique to the human persona -> distinctive and high salience.
        self.assertTrue(terms["cathedral"]["distinctive"])
        # Assistant-only word must not appear in the human persona's lexicon.
        self.assertNotIn("deploying", terms)
        self.assertGreater(payload["stats"]["persona_tokens"], 0)
        self.assertEqual(payload["yearning_method"], "derived")

    def test_legacy_assistant_persona_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "transcripts"
            source.mkdir()
            _write_transcript(
                source,
                "session.json",
                [
                    {"role": "user", "text": "Please proceed with the migration."},
                    {"role": "assistant", "text": "I cannot proceed with that migration request."},
                ],
            )
            payload = extract_persona_lexicon(
                Path(tmp), "claude", source_corpus=source, mode="legacy-role"
            )

        terms = {entry["term"] for entry in payload["vocabulary"]}
        self.assertIn("migration", terms)
        # Curated yearning still wins for a known persona even with a source.
        self.assertEqual(payload["yearning_method"], "curated")
        # Friction sentence ("I cannot proceed ...") surfaces forbidden terms.
        forbidden = {entry["term"] for entry in payload["forbidden_terms"]}
        self.assertIn("proceed", forbidden)
        self.assertIn("migration", forbidden)

    def test_legacy_jsonl_and_mapping_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "transcripts"
            source.mkdir()
            (source / "a.jsonl").write_text(
                "\n".join(
                    json.dumps(turn)
                    for turn in [
                        {"speaker": "rob", "content": "recursion recursion lattice"},
                        {"speaker": "assistant", "content": "noted"},
                    ]
                ),
                encoding="utf-8",
            )
            mapping = {"mapping": {}}
            mapping["mapping"]["n1"] = {
                "message": {"author": {"role": "user"}, "content": {"parts": ["lattice"]}}
            }
            mapping["mapping"]["n2"] = {
                "message": {"author": {"role": "assistant"}, "content": {"parts": ["ok"]}}
            }
            (source / "b.json").write_text(json.dumps(mapping), encoding="utf-8")
            payload = extract_persona_lexicon(
                Path(tmp), "rob", source_corpus=source, mode="legacy-role"
            )

        terms = {entry["term"]: entry["count"] for entry in payload["vocabulary"]}
        self.assertEqual(terms.get("lattice"), 2)
        self.assertEqual(terms.get("recursion"), 2)

    def test_legacy_corpus_pairs_index_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "corpus-root"
            corpus = source / "corpus"
            corpus.mkdir(parents=True)
            pairs = [
                {
                    "pair_id": "t-1-pair-001",
                    "summary": "User: harmonics resonate strongly Assistant: understood signal",
                }
            ]
            (corpus / "pairs-index.json").write_text(json.dumps(pairs), encoding="utf-8")
            payload = extract_persona_lexicon(
                Path(tmp), "rob", source_corpus=source, mode="legacy-role"
            )

        terms = {entry["term"] for entry in payload["vocabulary"]}
        self.assertIn("harmonics", terms)
        self.assertNotIn("understood", terms)

    def test_dry_run_status_is_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "transcripts"
            source.mkdir()
            _write_transcript(source, "s.json", [{"role": "user", "text": "alpha beta gamma"}])
            payload = extract_persona_lexicon(
                Path(tmp), "rob", source_corpus=source, dry_run=True, mode="legacy-role"
            )
        self.assertEqual(payload["status"], "PREVIEW")
        self.assertTrue(payload["dry_run"])

    def test_write_artifacts_emits_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            payload = extract_persona_lexicon(project_root, "claude")
            payload["timestamp"] = "20260619"
            artifacts = write_persona_extract_artifacts(project_root, payload)

            self.assertEqual(len(artifacts), 2)
            json_path, markdown_path = artifacts
            self.assertTrue(json_path.exists())
            self.assertTrue(markdown_path.exists())
            self.assertEqual(json_path.suffix, ".json")
            self.assertEqual(markdown_path.suffix, ".md")
            reloaded = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(reloaded["persona_id"], "claude")
            self.assertIn("persona-extract-claude-20260619", json_path.name)

    def test_render_markdown_contains_sections(self) -> None:
        payload = {
            "persona_id": "rob",
            "status": "EXTRACTED",
            "archetypal_pattern": "Seeker",
            "yearning_method": "derived",
            "ideal_yearning": "To explore.",
            "vocabulary": [{"term": "lattice", "count": 3, "salience": 5.0, "distinctive": True}],
            "forbidden_terms": [{"term": "proceed", "count": 1, "context": "I cannot proceed"}],
        }
        markdown = render_persona_extract_markdown(payload)
        self.assertIn("# Persona Extract — rob", markdown)
        self.assertIn("## Lexicon", markdown)
        self.assertIn("lattice", markdown)
        self.assertIn("## Forbidden Terms", markdown)
        self.assertIn("proceed", markdown)


if __name__ == "__main__":
    unittest.main()
