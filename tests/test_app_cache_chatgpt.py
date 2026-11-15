from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app_cache_support import write_entry  # noqa: E402

from conversation_corpus_engine import app_cache_commands, archive_read_chatgpt  # noqa: E402
from conversation_corpus_engine.app_cache_chatgpt import scan, write_staging  # noqa: E402
from conversation_corpus_engine.archive_model import SourceTracker  # noqa: E402

CID = "11111111-2222-3333-4444-555555555555"
OTHER = "66666666-7777-8888-9999-000000000000"
LIST_ONLY = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
HOST = "https://chatgpt.com/backend-api"


def conversation(update: float, title: str = "Synthetic title") -> dict:
    return {
        "title": title,
        "create_time": 1_790_000_000.0,
        "update_time": update,
        "conversation_id": CID,
        "current_node": "m2",
        "mapping": {
            "root": {"id": "root", "parent": None, "children": ["m1"], "message": None},
            "m1": {
                "id": "m1",
                "parent": "root",
                "children": ["m2"],
                "message": {
                    "author": {"role": "user"},
                    "create_time": 1_790_000_001.0,
                    "content": {"content_type": "text", "parts": ["synthetic question"]},
                    "metadata": {},
                },
            },
            "m2": {
                "id": "m2",
                "parent": "m1",
                "children": [],
                "message": {
                    "author": {"role": "assistant"},
                    "create_time": 1_790_000_002.0,
                    "content": {"content_type": "text", "parts": ["synthetic answer"]},
                    "metadata": {"model_slug": "synthetic-model"},
                },
            },
        },
    }


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    root = tmp_path / "Cache_Data"
    root.mkdir()
    write_entry(
        root,
        "0000000000000001_0",
        f"{HOST}/conversation/{CID}?x=1",
        json.dumps(conversation(1_790_000_100.0)).encode(),
    )
    write_entry(
        root,
        "0000000000000002_0",
        f"{HOST}/conversation/{CID}",
        json.dumps(conversation(1_790_000_900.0, "Newer")).encode(),
    )
    meta = {k: v for k, v in conversation(1_790_000_500.0).items() if k != "mapping"}
    write_entry(
        root,
        "0000000000000003_0",
        f"{HOST}/conversations/{OTHER}?num_turns=3",
        json.dumps({**meta, "conversation_id": OTHER}).encode(),
    )
    listing = {
        "items": [
            {
                "id": LIST_ONLY,
                "title": "Listed",
                "create_time": "2026-10-20T10:00:00Z",
                "update_time": "2026-10-25T10:00:00Z",
            },
            {"id": CID, "title": "Stale list title", "update_time": "2026-01-01T00:00:00Z"},
        ]
    }
    write_entry(
        root, "0000000000000004_0", f"{HOST}/conversations?limit=28", json.dumps(listing).encode()
    )
    write_entry(
        root,
        "0000000000000005_0",
        f"{HOST}/conversations/{OTHER}/messages?cursor=c",
        b'{"messages":[]}',
    )
    write_entry(
        root, "0000000000000006_0", "https://other.test/backend-api/conversation/" + CID, b"{}"
    )
    write_entry(
        root, "0000000000000007_0", f"{HOST}/conversation/{OTHER}", b"x", {"Content-Encoding": "br"}
    )
    return root


def test_scan_keeps_newest_full_body_and_indexes_the_rest(cache: Path) -> None:
    result = scan([cache], brotli=None)
    assert set(result.conversations) == {CID}
    assert result.conversations[CID]["title"] == "Newer"
    assert result.index[CID].body_cached and result.index[CID].title == "Newer"
    assert not result.index[OTHER].body_cached
    assert not result.index[LIST_ONLY].body_cached
    assert result.message_pages == 1 and result.message_page_conversations == {OTHER}
    assert result.bodies_unavailable["unsupported:br"] == 1


def test_staging_is_ingestible_by_the_existing_reader_and_coverage_has_no_titles(
    cache: Path, tmp_path: Path
) -> None:
    out = tmp_path / "stage"
    report = write_staging(scan([cache], brotli=None), out)
    assert (out.stat().st_mode & 0o777) == 0o700
    assert report["conversations_with_full_body"] == 1
    assert report["conversations_known_total"] == 3
    assert report["conversations_known_by_metadata_only"] == 2
    coverage_text = (out / "coverage.json").read_text()
    assert "Newer" not in coverage_text and CID not in coverage_text
    assert archive_read_chatgpt.detect(out)
    conversations = list(
        archive_read_chatgpt.iter_conversations(
            out, SourceTracker(out, archive_read_chatgpt.READER)
        )
    )
    assert [c.source_id for c in conversations] == [CID]
    assert [m.text for m in conversations[0].messages] == ["synthetic question", "synthetic answer"]


def test_staging_refuses_a_non_empty_directory(cache: Path, tmp_path: Path) -> None:
    out = tmp_path / "stage"
    out.mkdir()
    (out / "keep").write_text("x")
    with pytest.raises(FileExistsError):
        write_staging(scan([cache], brotli=None), out)
    assert (out / "keep").read_text() == "x"


def test_cli_stages_and_reports(cache: Path, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="group")
    app_cache_commands.add_app_cache_parser(sub)
    out = tmp_path / "cli-stage"
    args = parser.parse_args(
        ["app-cache", "chatgpt", "--root", str(cache), "--out", str(out), "--json"]
    )
    assert app_cache_commands.handle_app_cache(args) == 0
    assert json.loads(capsys.readouterr().out)["conversations_with_full_body"] == 1
    assert app_cache_commands.handle_app_cache(args) == 2


def test_existing_empty_directory_is_made_owner_only(cache: Path, tmp_path: Path) -> None:
    out = tmp_path / "open"
    out.mkdir(mode=0o755)
    out.chmod(0o755)
    write_staging(scan([cache], brotli=None), out)
    assert (out.stat().st_mode & 0o777) == 0o700
    assert all((p.stat().st_mode & 0o077) == 0 for p in out.iterdir())


def test_messages_only_conversation_is_indexed_and_missing_roots_are_counted(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Cache_Data"
    root.mkdir()
    write_entry(
        root, "0000000000000001_0", f"{HOST}/conversations/{OTHER}/messages", b'{"messages":[]}'
    )
    result = scan([root, tmp_path / "absent"], brotli=None)
    assert result.index[OTHER].sources == {"messages"}
    assert result.roots_missing == 1
    report = write_staging(result, tmp_path / "o")
    assert report["conversations_known_total"] == 1 and report["roots_missing"] == 1


def test_cli_refuses_missing_roots_and_a_git_visible_out(
    tmp_path: Path, cache: Path, capsys
) -> None:
    parser = argparse.ArgumentParser()
    app_cache_commands.add_app_cache_parser(parser.add_subparsers(dest="group"))
    missing = parser.parse_args(
        ["app-cache", "chatgpt", "--root", str(tmp_path / "nope"), "--out", str(tmp_path / "m")]
    )
    assert app_cache_commands.handle_app_cache(missing) == 2
    assert not (tmp_path / "m").exists()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    visible = parser.parse_args(
        ["app-cache", "chatgpt", "--root", str(cache), "--out", str(repo / "stage")]
    )
    assert app_cache_commands.handle_app_cache(visible) == 2
    assert not (repo / "stage").exists()
