"""Reproducible length-skew acceptance library for issue #132 (synthetic text only).

Twenty long conversations contain 80, 160 or 320 ordinary messages plus one late, short
known-item paragraph. Each query is a pair of real distinctive words, without a record ID or
message hint. Twelve medium-length distractors per target also contain that pair once, in a
much longer paragraph. Four hundred short conversations provide background vocabulary. Thus
whole-conversation BM25 dilutes the target while passage BM25 can rank its focused paragraph.
Repeated background passages exercise deduplication; targets and distractors remain distinct.

The benchmark uses the actual MCP handlers and their returned read arguments. The timing gate
is warm connection-per-call p95, not a claim about cold disk, remote transport or the real archive.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.library_store import Library

PAIRS = (
    ("azimuth", "orchid"),
    ("quartz", "harbor"),
    ("lattice", "meadow"),
    ("cobalt", "compass"),
    ("velvet", "nebula"),
    ("tundra", "prism"),
    ("basalt", "canopy"),
    ("saffron", "telescope"),
    ("granite", "lantern"),
    ("lichen", "pendulum"),
    ("marble", "estuary"),
    ("topaz", "sonata"),
    ("cypress", "gimbal"),
    ("coral", "zephyr"),
    ("falcon", "isotope"),
    ("juniper", "spectrum"),
    ("mosaic", "aperture"),
    ("pewter", "acacia"),
    ("raven", "solstice"),
    ("willow", "tangent"),
)
BACKGROUND = (
    "The working notes retain the sequence of ordinary observations, source context and careful "
    "discussion of the practical work. Each entry preserves its wording for later comparison. "
)


def build_length_skew_library(directory: Path) -> tuple[Path, Path, dict[str, int]]:
    """Return database, benchmark query file and explicit generator dimensions."""
    database, query_path = directory / "library.sqlite", directory / "queries.json"
    queries = []
    lengths = []
    with Library.create(database) as library:
        # Fixture construction only; the benchmark itself opens a read-only connection per call.
        library.connection.execute("PRAGMA synchronous=OFF")
        library.connection.execute("PRAGMA journal_mode=MEMORY")

        def put(identity: str, messages: list[str]) -> None:
            item = Conversation(
                provider="chatgpt",
                source_id=identity,
                title=f"Synthetic study {identity}",
                messages=[Message(role="assistant", text=text) for text in messages],
            )
            library.put(item)
            lengths.append(sum(map(len, messages)))

        for index, pair in enumerate(PAIRS):
            phrase = " ".join(pair)
            target = (
                f"The {phrase} observation is the specific finding under examination. "
                "This compact account preserves its evidence, provenance, interpretation and "
                "uncertainty in the original sequence of the extended discussion."
            )
            count = (80, 160, 320)[index % 3]
            messages = [f"{BACKGROUND} Discussion step {step}." for step in range(count)]
            messages.insert(count - 7, target)
            put(f"target-{index:02d}", messages)
            queries.append(
                {
                    "id": f"q{index:02d}",
                    "query": phrase,
                    "expected_uid": f"chatgpt-target-{index:02d}",
                    "expected_text": target,
                }
            )
            for distractor in range(12):
                text = (
                    BACKGROUND * (8 + distractor % 3)
                    + f"A passing reference mentions {phrase} in inventory entry {distractor}. "
                    + BACKGROUND * 2
                )
                put(f"distractor-{index:02d}-{distractor:02d}", [text])
        for index in range(400):
            put(f"background-{index:03d}", [f"{BACKGROUND} Notebook number {index}."])
        library.index_blocks()
        assert library.verify() == []
        dimensions = {
            "records": len(lengths),
            "queries": len(queries),
            "min_characters": min(lengths),
            "max_characters": max(lengths),
            "eligible_unique_blocks": library.status()["blocks"],
        }
    query_path.write_text(json.dumps({"schema": "cce.bench-queries.v1", "queries": queries}))
    return database, query_path, dimensions


def test_length_skew_known_items_reached_in_two_calls_within_2500_tokens(tmp_path, capsys):
    database, queries, dimensions = build_length_skew_library(tmp_path)
    assert dimensions["max_characters"] > 200 * dimensions["min_characters"]
    with Library(database) as library:
        query = " ".join(PAIRS[0])
        coarse = library.search(query, unit="conversation", limit=1)
        focused = library.search(query, unit="passage", limit=1)
        assert coarse["hits"][0]["uid"] != "chatgpt-target-00"
        assert focused["hits"][0]["uid"] == "chatgpt-target-00"
    script = Path(__file__).resolve().parents[1] / "scripts" / "bench_mcp.py"
    spec = importlib.util.spec_from_file_location("passage_benchmark", script)
    benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(benchmark)
    code = benchmark.main(
        [
            "--library",
            str(database),
            "--queries",
            str(queries),
            "--cold",
            "inprocess",
            "--repeats",
            "3",
            "--max-calls",
            "2",
            "--hash",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 0 and report["pass"], report
    assert report["units"]["within_budget_rate"] >= 0.9
    assert report["levels"]["L0"]["warm"]["wall_s"]["p95"] < 1
    assert report["library"]["integrity"]["unchanged"]
    assert all(row["calls_to_unit"] == 2 for row in report["queries"] if row["reached"])
    print(
        json.dumps({"dimensions": dimensions, "checks": report["checks"], "units": report["units"]})
    )
