from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from claude_code_to_bundle import (  # noqa: E402
    _ensure_timestamp,
    _extract_parts,
    _normalize_role,
    convert_directory,
    discover_input_files,
)

from conversation_corpus_engine.import_chatgpt_export_corpus import (  # noqa: E402
    import_chatgpt_export_corpus,
)
from conversation_corpus_engine.sharded_collection import collection_storage_path  # noqa: E402

SESSION_A = "aaaaaaaa-1111-2222-3333-444444444444"
SESSION_B = "bbbbbbbb-5555-6666-7777-888888888888"


def _user_event(
    text: str,
    *,
    session_id: str = SESSION_A,
    uuid: str = "",
    timestamp: str = "2026-06-05T19:50:52.002Z",
    sidechain: bool = False,
) -> dict:
    return {
        "type": "user",
        "isSidechain": sidechain,
        "sessionId": session_id,
        "timestamp": timestamp,
        "uuid": uuid or f"u-{abs(hash((text, session_id, timestamp))):x}",
        "message": {"role": "user", "content": text},
    }


def _assistant_event(
    blocks: list[dict],
    *,
    session_id: str = SESSION_A,
    uuid: str = "",
    timestamp: str = "2026-06-05T19:51:00.000Z",
    sidechain: bool = False,
) -> dict:
    return {
        "type": "assistant",
        "isSidechain": sidechain,
        "sessionId": session_id,
        "timestamp": timestamp,
        "uuid": uuid or f"a-{abs(hash((str(blocks), session_id, timestamp))):x}",
        "message": {"role": "assistant", "content": blocks},
    }


def _write_jsonl(directory: Path, name: str, events: list) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        "\n".join(json.dumps(ev, ensure_ascii=False) for ev in events) + "\n",
        encoding="utf-8",
    )
    return path


class HelpersTests(unittest.TestCase):
    def test_ensure_timestamp_parses_iso_zulu(self) -> None:
        ts = _ensure_timestamp("2026-06-05T19:50:52.002Z")
        self.assertGreater(ts, 0)

    def test_ensure_timestamp_parses_offset_and_epoch(self) -> None:
        self.assertGreater(_ensure_timestamp("2026-06-05T19:50:52+00:00"), 0)
        self.assertEqual(_ensure_timestamp(1750000000), 1750000000.0)
        self.assertEqual(_ensure_timestamp(1750000000.5), 1750000000.5)

    def test_ensure_timestamp_handles_garbage(self) -> None:
        self.assertEqual(_ensure_timestamp(None), 0.0)
        self.assertEqual(_ensure_timestamp(""), 0.0)
        self.assertEqual(_ensure_timestamp("not a date"), 0.0)
        self.assertEqual(_ensure_timestamp(["nope"]), 0.0)

    def test_normalize_role(self) -> None:
        self.assertEqual(_normalize_role("user"), "user")
        self.assertEqual(_normalize_role("Human"), "user")
        self.assertEqual(_normalize_role("assistant"), "assistant")
        self.assertEqual(_normalize_role("Claude"), "assistant")
        self.assertEqual(_normalize_role(None), "user")
        self.assertEqual(_normalize_role("weird"), "user")

    def test_extract_parts_string_is_verbatim(self) -> None:
        raw = "  spaced  \n\tprompt — with unicode ✓ and trailing space \n"
        self.assertEqual(_extract_parts(raw), [raw])

    def test_extract_parts_skips_blank_string(self) -> None:
        self.assertEqual(_extract_parts("   \n  "), [])
        self.assertEqual(_extract_parts(None), [])
        self.assertEqual(_extract_parts(42), [])

    def test_extract_parts_keeps_text_blocks_skips_others(self) -> None:
        blocks = [
            {"type": "thinking", "thinking": "internal reasoning"},
            {"type": "text", "text": "visible answer"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            {"type": "tool_result", "content": "file1\nfile2"},
            {"type": "text", "text": "second paragraph"},
        ]
        self.assertEqual(_extract_parts(blocks), ["visible answer", "second paragraph"])


class ConvertDirectoryTests(unittest.TestCase):
    def test_builds_linear_mapping_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "in"
            output = Path(tmpdir) / "out"
            events = [
                {"type": "mode", "mode": "normal", "sessionId": SESSION_A},
                _user_event("Hello", timestamp="2026-06-05T10:00:00Z"),
                _assistant_event(
                    [{"type": "text", "text": "Hi back"}],
                    timestamp="2026-06-05T10:00:05Z",
                ),
                _user_event("Tell me more", timestamp="2026-06-05T10:00:10Z"),
            ]
            _write_jsonl(input_dir, f"{SESSION_A}.jsonl", events)

            result = convert_directory([input_dir], output)
            self.assertEqual(result["conversations_written"], 1)
            self.assertEqual(result["messages_written"], 3)

            convs = json.loads((output / "conversations.json").read_text(encoding="utf-8"))
            self.assertEqual(len(convs), 1)
            conv = convs[0]
            self.assertEqual(conv["conversation_id"], SESSION_A)
            self.assertEqual(conv["title"], f"Claude Code Session {SESSION_A[:8]}")

            mapping = conv["mapping"]
            self.assertEqual(len(mapping), 4)  # 1 root + 3 message nodes
            roots = [n for n in mapping.values() if n["parent"] is None]
            self.assertEqual(len(roots), 1)
            root = roots[0]
            self.assertIsNone(root["message"])
            self.assertEqual(len(root["children"]), 1)

            cur = mapping[root["children"][0]]
            self.assertEqual(cur["message"]["author"]["role"], "user")
            self.assertEqual(cur["message"]["content"]["parts"], ["Hello"])
            cur = mapping[cur["children"][0]]
            self.assertEqual(cur["message"]["author"]["role"], "assistant")
            self.assertEqual(cur["message"]["content"]["parts"], ["Hi back"])
            cur = mapping[cur["children"][0]]
            self.assertEqual(cur["message"]["author"]["role"], "user")
            self.assertEqual(cur["children"], [])

            self.assertTrue((output / "user.json").exists())

    def test_user_prompt_text_is_byte_identical(self) -> None:
        prompt = "PACKET — Builder.\n\n  indented   block\t\ttabs\ntrailing space \nunicode: ✓→σ\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "in"
            output = Path(tmpdir) / "out"
            _write_jsonl(input_dir, f"{SESSION_A}.jsonl", [_user_event(prompt)])

            convert_directory([input_dir], output)
            convs = json.loads((output / "conversations.json").read_text(encoding="utf-8"))
            mapping = convs[0]["mapping"]
            nodes = [n for n in mapping.values() if n["message"] is not None]
            self.assertEqual(len(nodes), 1)
            self.assertEqual(nodes[0]["message"]["content"]["parts"], [prompt])

    def test_skips_sidechain_events_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "in"
            events = [
                _user_event("main thread", timestamp="2026-06-05T10:00:00Z"),
                _user_event(
                    "subagent packet",
                    timestamp="2026-06-05T10:00:01Z",
                    sidechain=True,
                ),
            ]
            _write_jsonl(input_dir, f"{SESSION_A}.jsonl", events)

            result = convert_directory([input_dir], Path(tmpdir) / "out")
            self.assertEqual(result["messages_written"], 1)
            self.assertEqual(result["skipped_sidechain_events"], 1)

            result_inc = convert_directory(
                [input_dir], Path(tmpdir) / "out2", include_sidechains=True
            )
            self.assertEqual(result_inc["messages_written"], 2)
            self.assertEqual(result_inc["skipped_sidechain_events"], 0)

    def test_skips_tool_result_only_user_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "in"
            tool_result_event = {
                "type": "user",
                "isSidechain": False,
                "sessionId": SESSION_A,
                "timestamp": "2026-06-05T10:00:02Z",
                "uuid": "tool-result-1",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "content": "stdout noise", "tool_use_id": "t1"}
                    ],
                },
            }
            events = [
                _user_event("real prompt", timestamp="2026-06-05T10:00:00Z"),
                tool_result_event,
            ]
            _write_jsonl(input_dir, f"{SESSION_A}.jsonl", events)

            result = convert_directory([input_dir], Path(tmpdir) / "out")
            self.assertEqual(result["messages_written"], 1)

    def test_dedupes_replayed_events_by_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "in"
            ev = _user_event("once only", uuid="fixed-uuid-1")
            _write_jsonl(input_dir, f"{SESSION_A}.jsonl", [ev, ev])
            # Resumed-session file replaying the same event uuid
            _write_jsonl(input_dir, "resume.jsonl", [ev])

            result = convert_directory([input_dir], Path(tmpdir) / "out")
            self.assertEqual(result["conversations_written"], 1)
            self.assertEqual(result["messages_written"], 1)

    def test_groups_by_session_id_across_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "in"
            _write_jsonl(
                input_dir,
                f"{SESSION_A}.jsonl",
                [_user_event("session a", uuid="ev-a")],
            )
            _write_jsonl(
                input_dir,
                f"{SESSION_B}.jsonl",
                [_user_event("session b", session_id=SESSION_B, uuid="ev-b")],
            )

            result = convert_directory([input_dir], Path(tmpdir) / "out")
            self.assertEqual(result["conversations_written"], 2)
            convs = json.loads(
                (Path(tmpdir) / "out" / "conversations.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                sorted(c["conversation_id"] for c in convs), sorted([SESSION_A, SESSION_B])
            )

    def test_session_id_falls_back_to_file_stem_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "in"
            ev = _user_event("orphan")
            del ev["sessionId"]
            _write_jsonl(input_dir, "orphan-file.jsonl", [ev])

            result = convert_directory([input_dir], Path(tmpdir) / "out")
            self.assertEqual(result["conversations_written"], 1)
            convs = json.loads(
                (Path(tmpdir) / "out" / "conversations.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(convs[0]["conversation_id"]), 36)

    def test_orders_messages_by_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "in"
            events = [
                _user_event("second", uuid="ev-2", timestamp="2026-06-05T10:00:10Z"),
                _user_event("first", uuid="ev-1", timestamp="2026-06-05T10:00:00Z"),
            ]
            _write_jsonl(input_dir, f"{SESSION_A}.jsonl", events)

            convert_directory([input_dir], Path(tmpdir) / "out")
            convs = json.loads(
                (Path(tmpdir) / "out" / "conversations.json").read_text(encoding="utf-8")
            )
            mapping = convs[0]["mapping"]
            root = next(n for n in mapping.values() if n["parent"] is None)
            first = mapping[root["children"][0]]
            self.assertEqual(first["message"]["content"]["parts"], ["first"])
            second = mapping[first["children"][0]]
            self.assertEqual(second["message"]["content"]["parts"], ["second"])

    def test_skips_invalid_lines_and_non_conversation_types(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "in"
            input_dir.mkdir(parents=True)
            path = input_dir / f"{SESSION_A}.jsonl"
            lines = [
                "not json at all {{{",
                json.dumps({"type": "file-history-snapshot", "snapshot": {}}),
                json.dumps({"type": "system", "content": "hook output"}),
                json.dumps(["a", "bare", "list"]),
                json.dumps(_user_event("kept", uuid="ev-k")),
            ]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            result = convert_directory([input_dir], Path(tmpdir) / "out")
            self.assertEqual(result["skipped_invalid_lines"], 2)
            self.assertEqual(result["messages_written"], 1)

    def test_raises_on_no_input_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "in"
            input_dir.mkdir()
            with self.assertRaises(FileNotFoundError):
                convert_directory([input_dir], Path(tmpdir) / "out")

    def test_raises_when_no_conversation_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "in"
            _write_jsonl(
                input_dir,
                "meta-only.jsonl",
                [{"type": "mode", "mode": "normal", "sessionId": SESSION_A}],
            )
            with self.assertRaises(ValueError):
                convert_directory([input_dir], Path(tmpdir) / "out")

    def test_discover_accepts_files_and_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "in"
            input_dir.mkdir()
            (input_dir / "b.jsonl").write_text("{}", encoding="utf-8")
            (input_dir / "a.jsonl").write_text("{}", encoding="utf-8")
            (input_dir / "notes.json").write_text("{}", encoding="utf-8")
            lone = Path(tmpdir) / "lone.jsonl"
            lone.write_text("{}", encoding="utf-8")

            files = discover_input_files([input_dir, lone])
            self.assertEqual([f.name for f in files], ["a.jsonl", "b.jsonl", "lone.jsonl"])


class EndToEndIntegrationTests(unittest.TestCase):
    """Verify the converter's bundle is ingestible by the standard adapter."""

    def test_converter_output_imports_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "transcripts"
            events = [
                {"type": "permission-mode", "permissionMode": "plan", "sessionId": SESSION_A},
                _user_event(
                    "What is the capital of France?",
                    uuid="e2e-1",
                    timestamp="2026-06-05T10:00:00Z",
                ),
                _assistant_event(
                    [
                        {"type": "thinking", "thinking": "easy one"},
                        {"type": "text", "text": "Paris."},
                    ],
                    uuid="e2e-2",
                    timestamp="2026-06-05T10:00:05Z",
                ),
                _user_event(
                    "Tell me one historical fact about it that surprises tourists.",
                    uuid="e2e-3",
                    timestamp="2026-06-05T10:00:10Z",
                ),
                _assistant_event(
                    [
                        {
                            "type": "text",
                            "text": "The Louvre was originally a fortress built in 1190.",
                        }
                    ],
                    uuid="e2e-4",
                    timestamp="2026-06-05T10:00:15Z",
                ),
            ]
            _write_jsonl(input_dir, f"{SESSION_A}.jsonl", events)

            bundle = Path(tmpdir) / "bundle"
            output = Path(tmpdir) / "corpus"

            convert_directory([input_dir], bundle)
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
