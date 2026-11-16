from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conversation_corpus_engine.corpus_candidates import stage_corpus_candidate
from conversation_corpus_engine.corpus_store import register_corpus_store
from conversation_corpus_engine.federation import build_federation, upsert_corpus
from conversation_corpus_engine.governance_candidates import stage_policy_candidate
from conversation_corpus_engine.governance_replay import (
    build_policy_replay_payload,
    write_policy_replay_artifacts,
)
from conversation_corpus_engine.import_markdown_document_corpus import (
    import_markdown_document_corpus,
)
from conversation_corpus_engine.provider_catalog import PROVIDER_CONFIG
from conversation_corpus_engine.schema_validation import validate_json_file, validate_payload
from conversation_corpus_engine.source_policy import set_source_policy
from conversation_corpus_engine.surface_exports import (
    CORPUS_STORE_ROOT_PLACEHOLDER,
    HISTORICAL_CORPUS_ROOT_PLACEHOLDER,
    build_mcp_context_payload,
    build_surface_manifest,
    export_surface_bundle,
    mcp_context_json_path,
    redact_corpus_store_paths,
    surface_bundle_json_path,
    surface_manifest_json_path,
)


def write_markdown_sources(root: Path, files: dict[str, str]) -> None:
    for relative_path, content in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def seed_surface_project(
    project_root: Path,
    source_drop_root: Path,
    *,
    corpus_store_root: Path | None = None,
) -> None:
    workspace_root = project_root.parent
    live_source = workspace_root / "perplexity-live-source"
    candidate_source = workspace_root / "perplexity-candidate-source"
    live_root = (corpus_store_root or workspace_root) / "perplexity-history-memory"
    candidate_root = workspace_root / "perplexity-history-memory-candidate"

    write_markdown_sources(
        live_source,
        {
            "baseline.md": "# Perplexity Baseline\n\nNeed to preserve the live corpus contract.\n\nMaybe keep source authority on the reviewed baseline until promotion.\n",
        },
    )
    write_markdown_sources(
        candidate_source,
        {
            "candidate.md": "# Perplexity Candidate\n\nNeed to expose Meta-facing surfaces from the governed corpus engine.\n\nMaybe promote the refreshed candidate after review.\n",
        },
    )
    write_markdown_sources(
        source_drop_root / "perplexity" / "inbox",
        {
            "export.md": "# Refreshed Export\n\nNeed to import a fresh Perplexity export.\n\nMaybe stage it through provider refresh.\n",
        },
    )

    import_markdown_document_corpus(
        live_source,
        live_root,
        corpus_id="perplexity-history-memory",
        name="Perplexity History Memory",
    )
    import_markdown_document_corpus(
        candidate_source,
        candidate_root,
        corpus_id="perplexity-history-memory",
        name="Perplexity History Memory",
    )
    upsert_corpus(
        project_root,
        live_root,
        corpus_id="perplexity-history-memory",
        name="Perplexity History Memory",
        make_default=True,
    )
    set_source_policy(
        project_root,
        "perplexity",
        primary_root=live_root,
        primary_corpus_id="perplexity-history-memory",
        decision="manual",
        note="Use the live Perplexity corpus.",
    )
    build_federation(project_root)
    write_policy_replay_artifacts(project_root, build_policy_replay_payload(project_root))
    stage_policy_candidate(
        project_root,
        threshold_overrides={"max_warn_corpora": 1.0},
        note="Stage a policy candidate for surface export coverage.",
    )
    stage_corpus_candidate(
        project_root,
        candidate_root=candidate_root,
        provider="perplexity",
        note="Stage a candidate corpus for surface export coverage.",
    )


class SurfaceExportsTests(unittest.TestCase):
    def test_redaction_is_boundary_aware_and_classifies_rotated_corpus_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir).resolve()
            active_store = workspace_root / "active corpus store"
            active_corpus = active_store / "perplexity history"
            sibling_prefix = Path(f"{active_store}house") / "public"
            rotated_corpus = workspace_root / "rotated corpus store" / "perplexity history"
            unrelated_root = workspace_root / "unrelated root"
            registry = {
                "corpus_store": {
                    "status": "active",
                    "root": str(active_store),
                },
                "corpora": [],
            }
            payload = {
                "registry": {
                    "corpora": [
                        {
                            "corpus_id": "perplexity-history-memory",
                            "root": str(rotated_corpus),
                        }
                    ]
                },
                "providers": [
                    {
                        "selected_targets": [{"root": str(active_corpus)}],
                        "source_policy": {"primary_root": str(rotated_corpus)},
                        "next_command": (
                            "cce provider bootstrap-eval "
                            f"--target-root='{active_corpus}' "
                            f"--legacy-root={rotated_corpus}"
                        ),
                    }
                ],
                "unrelated": {
                    "root": str(unrelated_root),
                    "sibling": str(sibling_prefix),
                },
            }

            redacted = redact_corpus_store_paths(payload, registry)
            serialized = json.dumps(redacted)

            self.assertEqual(
                redacted["providers"][0]["selected_targets"][0]["root"],
                f"{CORPUS_STORE_ROOT_PLACEHOLDER}/perplexity history",
            )
            self.assertIn(
                f"--target-root='{CORPUS_STORE_ROOT_PLACEHOLDER}/perplexity history'",
                redacted["providers"][0]["next_command"],
            )
            self.assertIn(
                f"--legacy-root={HISTORICAL_CORPUS_ROOT_PLACEHOLDER}",
                redacted["providers"][0]["next_command"],
            )
            self.assertEqual(
                redacted["registry"]["corpora"][0]["root"],
                HISTORICAL_CORPUS_ROOT_PLACEHOLDER,
            )
            self.assertEqual(redacted["unrelated"]["root"], str(unrelated_root))
            self.assertEqual(redacted["unrelated"]["sibling"], str(sibling_prefix))
            self.assertNotIn(str(active_corpus), serialized)
            self.assertNotIn(str(rotated_corpus), serialized)

    def test_exported_surface_artifacts_validate_against_published_schemas(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir)
            project_root = workspace_root / "project"
            source_drop_root = workspace_root / "source-drop"
            corpus_store_root = workspace_root / "private-corpus-store"
            corpus_store_root.mkdir()
            seed_surface_project(
                project_root,
                source_drop_root,
                corpus_store_root=corpus_store_root,
            )
            register_corpus_store(project_root, corpus_store_root)

            manifest = build_surface_manifest(
                project_root,
                source_drop_root=source_drop_root,
            )
            context = build_mcp_context_payload(
                project_root,
                source_drop_root=source_drop_root,
            )
            bundle = export_surface_bundle(
                project_root,
                source_drop_root=source_drop_root,
            )

            manifest_result = validate_json_file(
                "surface-manifest",
                surface_manifest_json_path(project_root),
            )
            context_result = validate_json_file(
                "mcp-context",
                mcp_context_json_path(project_root),
            )
            bundle_result = validate_json_file(
                "surface-bundle",
                surface_bundle_json_path(project_root),
            )
            commercial_result = validate_payload(
                "commercial-awareness",
                manifest["commercial_awareness"],
            )
            commercial_schema_names = {item["name"] for item in manifest["schemas"]}
            bridge_surfaces = {
                item["pipeline_surface"]: item
                for item in manifest["commercial_awareness"]["pipeline_bridges"]
            }

            self.assertEqual(manifest["registry"]["default_corpus_id"], "perplexity-history-memory")
            self.assertEqual(
                manifest["commercial_awareness"]["relationship"]["pipeline_repo"],
                "4444J99/application-pipeline",
            )
            self.assertEqual(
                context["commercial_awareness"]["relationship"]["relationship_type"],
                "symbiotic-income-surface",
            )
            self.assertTrue(
                manifest["commercial_awareness"]["relationship"]["same_income_equation"]
            )
            self.assertIn("commercial-awareness", commercial_schema_names)
            self.assertEqual(
                bridge_surfaces["consulting"]["cce_surface"],
                "CCE Ring 4 enterprise services",
            )
            self.assertTrue(
                Path(manifest["commercial_awareness"]["source_specs"][0]["path"]).exists()
            )
            self.assertEqual(context["summary"]["active_corpus_count"], 1)
            self.assertEqual(context["summary"]["provider_count"], len(PROVIDER_CONFIG))
            self.assertNotIn(str(corpus_store_root.resolve()), json.dumps(manifest))
            self.assertNotIn(str(corpus_store_root.resolve()), json.dumps(context))
            self.assertEqual(
                manifest["registry"]["corpora"][0]["root"],
                "${CCE_CORPUS_STORE_ROOT}/perplexity-history-memory",
            )
            self.assertEqual(
                context["registry"]["corpora"][0]["root"],
                "${CCE_CORPUS_STORE_ROOT}/perplexity-history-memory",
            )
            self.assertTrue(bundle["summary"]["valid"])
            self.assertTrue(manifest_result["valid"], manifest_result["errors"])
            self.assertTrue(context_result["valid"], context_result["errors"])
            self.assertTrue(bundle_result["valid"], bundle_result["errors"])
            self.assertTrue(commercial_result["valid"], commercial_result["errors"])


if __name__ == "__main__":
    unittest.main()
