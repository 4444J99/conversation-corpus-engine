from __future__ import annotations

import hashlib
import json
from pathlib import Path

from conversation_corpus_engine import archive_read_grok
from conversation_corpus_engine.archive_ingest import ingest, iter_store
from conversation_corpus_engine.archive_model import SourceTracker


def test_chatgpt_attachment_rename_changes_source_fingerprint(archive_env) -> None:
    kwargs = {
        "export_root": archive_env.export,
        "paths": archive_env.paths,
        "authorization": archive_env.auth,
        "providers": ["chatgpt"],
    }
    before = ingest(**kwargs)
    original = next(iter_store(archive_env.paths, ["chatgpt"]))
    assert original.messages[0].attachments[0].name == "diagram.png"

    asset_map = archive_env.export / "chatgpt" / "conversation_asset_file_names.json"
    asset_map.write_text(json.dumps({"file-IMG1.dat": "renamed-diagram.png"}), encoding="utf-8")
    after = ingest(**kwargs)
    renamed = next(iter_store(archive_env.paths, ["chatgpt"]))

    assert renamed.uid == original.uid
    assert renamed.messages[0].attachments[0].name == "renamed-diagram.png"
    assert after["source_fingerprint"] != before["source_fingerprint"]
    assert (
        after["providers"]["chatgpt"]["source_files"][asset_map.name]
        == hashlib.sha256(asset_map.read_bytes()).hexdigest()
    )


def _media_post(identifier: str, prompt: str, timestamp: str) -> dict[str, str]:
    return {
        "id": identifier,
        "original_prompt": prompt,
        "media_type": "image",
        "create_time": timestamp,
        "link": f"https://example.invalid/{identifier}",
    }


def _write_backend(path: Path, posts: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"conversations": [], "projects": [], "tasks": [], "media_posts": posts}),
        encoding="utf-8",
    )


def test_ingest_preserves_grok_media_union_across_overlapping_exports(archive_env) -> None:
    later_export = archive_env.export / "grok" / "90d" / "export_data" / "wsid"
    backend = later_export / "prod-grok-backend.json"
    _write_backend(
        backend,
        [
            _media_post("mp5", "a paper lantern", "2025-05-07T00:00:00Z"),
            _media_post("mp4", "a loaf of bread", "2025-05-05T00:00:00Z"),
            {
                **_media_post("mp1", "a loaf of bread", "2025-05-05T00:00:00Z"),
                "link": "https://example.invalid/1",
            },
        ],
    )
    manifest = ingest(
        export_root=archive_env.export,
        paths=archive_env.paths,
        authorization=archive_env.auth,
        providers=["grok"],
    )
    media = [c for c in iter_store(archive_env.paths, ["grok"]) if c.collection == "media"]

    assert [c.source_id for c in media] == ["media-2025-05", "media-2025-06"]
    may = media[0]
    assert [m.attachments[0].name for m in may.messages] == ["mp1", "mp4", "mp5"]
    assert [m.text for m in may.messages] == [
        "a loaf of bread",
        "a loaf of bread",
        "a paper lantern",
    ]
    assert may.extra["posts"] == 3
    sources = {item["source_file"]: item["source_sha256"] for item in may.provenance["sources"]}
    assert set(sources) == {
        "30d/export_data/wsid/prod-grok-backend.json",
        "90d/export_data/wsid/prod-grok-backend.json",
    }
    assert sources == manifest["providers"]["grok"]["source_files"]
    assert manifest["providers"]["grok"]["duplicates_folded_differing"] == 0


def test_grok_media_union_preserves_differing_observations_of_one_post(tmp_path: Path) -> None:
    _write_backend(
        tmp_path / "first" / "prod-grok-backend.json",
        [_media_post("post-1", "a blue lantern", "2025-05-05T00:00:00Z")],
    )
    _write_backend(
        tmp_path / "second" / "prod-grok-backend.json",
        [_media_post("post-1", "a red lantern", "2025-05-05T00:00:00Z")],
    )
    tracker = SourceTracker(tmp_path, archive_read_grok.READER)

    conversations = list(archive_read_grok.iter_conversations(tmp_path, tracker))

    assert len(conversations) == 1
    assert [m.text for m in conversations[0].messages] == ["a blue lantern", "a red lantern"]


def test_grok_media_union_deduplicates_idless_observations_by_content(tmp_path: Path) -> None:
    first = _media_post("", "a blue lantern", "2025-05-05T00:00:00Z")
    second = _media_post("", "a red lantern", "2025-05-05T00:00:00Z")
    _write_backend(tmp_path / "first" / "prod-grok-backend.json", [first])
    _write_backend(tmp_path / "second" / "prod-grok-backend.json", [second, first])
    tracker = SourceTracker(tmp_path, archive_read_grok.READER)

    conversations = list(archive_read_grok.iter_conversations(tmp_path, tracker))

    assert len(conversations) == 1
    assert [m.text for m in conversations[0].messages] == ["a blue lantern", "a red lantern"]
