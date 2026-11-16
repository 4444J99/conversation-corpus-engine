from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conversation_corpus_engine.corpus_store import CorpusStoreError, register_corpus_store
from conversation_corpus_engine.federation import list_registered_corpora
from conversation_corpus_engine.perplexity_local_session import DEFAULT_PERPLEXITY_COOKIE_JAR
from conversation_corpus_engine.provider_catalog import get_provider_config
from conversation_corpus_engine.provider_import import (
    default_output_root,
    import_provider_corpus,
    resolve_provider_import_source,
)
from conversation_corpus_engine.provider_readiness import build_provider_readiness


class ProviderImportTests(unittest.TestCase):
    def test_import_provider_corpus_registers_and_builds_federation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir)
            project_root = workspace_root / "project"
            project_root.mkdir()
            corpus_store_root = workspace_root / "corpus-store"
            corpus_store_root.mkdir()
            register_corpus_store(project_root, corpus_store_root)
            source_drop_root = workspace_root / "source-drop"
            inbox = source_drop_root / "perplexity" / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            (inbox / "export.md").write_text(
                "# Routing Notes\n\nWe should build the provider import command next.\n",
                encoding="utf-8",
            )

            result = import_provider_corpus(
                project_root=project_root,
                provider="perplexity",
                source_drop_root=source_drop_root,
                corpus_store_root=corpus_store_root,
                register=True,
                build=True,
            )

            registry = list_registered_corpora(project_root)
            readiness = build_provider_readiness(project_root, source_drop_root)
            perplexity = next(
                item for item in readiness["providers"] if item["provider"] == "perplexity"
            )
            contract = json.loads(
                (Path(result["output_root"]) / "corpus" / "contract.json").read_text(
                    encoding="utf-8"
                ),
            )

            self.assertEqual(result["corpus_id"], "perplexity-history-memory")
            self.assertEqual(
                Path(result["output_root"]).resolve(),
                (corpus_store_root / "perplexity-history-memory").resolve(),
            )
            self.assertEqual(contract["adapter_type"], "perplexity-export")
            self.assertEqual(len(registry), 1)
            self.assertEqual(registry[0]["corpus_id"], "perplexity-history-memory")
            self.assertTrue((project_root / "federation" / "federation-summary.md").exists())
            self.assertTrue(Path(result["bootstrap_result"]["manual_guide_path"]).exists())
            self.assertTrue(Path(result["bootstrap_result"]["seeded_paths"]["answers"]).exists())
            self.assertEqual(perplexity["overall_state"], "manual-eval-pending")
            self.assertIn("cce evaluation run --root", perplexity["next_command"])

    def test_import_denial_creates_no_destination_or_control_plane_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir)
            project_root = workspace_root / "project"
            project_root.mkdir()
            corpus_store_root = workspace_root / "corpus-store"
            corpus_store_root.mkdir()
            register_corpus_store(project_root, corpus_store_root)
            source_drop_root = workspace_root / "source-drop"
            inbox = source_drop_root / "perplexity" / "inbox"
            inbox.mkdir(parents=True)
            (inbox / "export.md").write_text("# Export\n\nPrivate custody required.\n")
            outside_destination = workspace_root / "visible-output"
            registry_path = project_root / "state" / "federation-registry.json"
            registry_before = registry_path.read_bytes()

            with self.assertRaises(CorpusStoreError):
                import_provider_corpus(
                    project_root=project_root,
                    provider="perplexity",
                    source_drop_root=source_drop_root,
                    corpus_store_root=corpus_store_root,
                    output_root=outside_destination,
                )

            self.assertFalse(outside_destination.exists())
            self.assertEqual(registry_path.read_bytes(), registry_before)
            self.assertFalse((project_root / "reports").exists())


class PerplexityLocalSessionResolutionTests(unittest.TestCase):
    """Resolver + output-root wiring for perplexity local-session (no network)."""

    def test_resolver_defaults_to_catalog_cookie_jar(self) -> None:
        resolved, meta = resolve_provider_import_source(
            provider="perplexity",
            mode="local-session",
            project_root=Path("/tmp/project"),
        )
        self.assertEqual(meta["resolution"], "local-session-cookie-jar")
        self.assertEqual(resolved, DEFAULT_PERPLEXITY_COOKIE_JAR.resolve())

    def test_resolver_maps_httpstorages_dir_to_cookie_jar(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            http_storages = Path(tmpdir) / "HTTPStorages"
            http_storages.mkdir()
            resolved, meta = resolve_provider_import_source(
                provider="perplexity",
                mode="local-session",
                project_root=Path("/tmp/project"),
                local_root=http_storages,
            )
            self.assertEqual(meta["resolution"], "local-session-cookie-jar")
            self.assertEqual(resolved.name, DEFAULT_PERPLEXITY_COOKIE_JAR.name)
            self.assertEqual(resolved.parent, http_storages.resolve())

    def test_resolver_accepts_explicit_cookie_jar_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            jar = Path(tmpdir) / "ai.perplexity.macv3.binarycookies"
            jar.write_bytes(b"cook")
            resolved, _meta = resolve_provider_import_source(
                provider="perplexity",
                mode="local-session",
                project_root=Path("/tmp/project"),
                local_root=jar,
            )
            self.assertEqual(resolved, jar.resolve())

    def test_default_output_root_uses_default_corpus_id(self) -> None:
        # Post corpus-store-custody refactor (#64), default_output_root takes an
        # already-resolved (store_root, corpus_id); corpus_id resolution moved up
        # into import_provider_corpus, which uses the catalog default_corpus_id for
        # perplexity local-session. Assert the invariant at both its new homes.
        corpus_id = get_provider_config("perplexity")["default_corpus_id"]
        self.assertEqual(corpus_id, "perplexity-history-memory")
        with tempfile.TemporaryDirectory() as tmpdir:
            store_root = Path(tmpdir) / "store"
            root = default_output_root(corpus_store_root=store_root, corpus_id=corpus_id)
            self.assertTrue(str(root).endswith("perplexity-history-memory"))


if __name__ == "__main__":
    unittest.main()
