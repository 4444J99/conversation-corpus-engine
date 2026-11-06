"""The MCP-boundary retrieval benchmark harness (scripts/bench_mcp.py).

Every library here is synthetic: 220 invented conversations built through the real library build
and publish path inside pytest temp directories. No real archive, store or title is read.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conversation_corpus_engine import library_mcp
from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.corpus_store import register_corpus_store
from conversation_corpus_engine.library_build import build_library, publish_library
from conversation_corpus_engine.library_store import Library

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bench_mcp.py"
QUERIES = ROOT / "tests" / "bench" / "queries.json"
RECORDS = 220
TOPICS = ["lattice", "ledger", "orchard", "compass", "harbor", "quartz", "meadow", "signal"]


def synthetic_conversation(index: int) -> Conversation:
    topic = TOPICS[index % len(TOPICS)]
    other = TOPICS[(index + 3) % len(TOPICS)]
    filler = " ".join(f"{topic} detail{n}" for n in range(60))
    return Conversation(
        provider="chatgpt",
        source_id=f"bench-{index:04d}",
        title=f"Synthetic {topic} study {index:04d}",
        messages=[
            Message(
                role="user", text=f"Question about {topic} and {other}. Marker ma{index:04d}q."
            ),
            Message(
                role="assistant",
                text=f"Answer about {topic}. Evidence token ev{index:04d}q. {filler}",
            ),
            Message(role="user", text=f"Follow up fu{index:04d}q on {other}."),
        ],
    )


def synthetic_queries() -> dict:
    """The deterministic query set that tests/bench/queries.json must equal."""
    queries = []
    for position, index in enumerate(range(0, RECORDS, 9)):
        uid = synthetic_conversation(index).uid
        topic = TOPICS[index % len(TOPICS)]
        shape = position % 4
        if shape == 0:
            entry = {
                "query": f"ev{index:04d}q",
                "expected_text": f"Evidence token ev{index:04d}q",
                "expected_message": 1,
            }
        elif shape == 1:
            entry = {
                "query": f"ma{index:04d}q {topic}",
                "expected_text": f"Marker ma{index:04d}q",
                "expected_message": 0,
            }
        elif shape == 2:
            entry = {"query": f"fu{index:04d}q"}
        else:
            # No message hint: the reader starts at message 0, where this passage lives.
            entry = {
                "query": f"{topic} ma{index:04d}q",
                "expected_text": f"Marker ma{index:04d}q",
            }
        queries.append({"id": f"q{position:03d}", **entry, "expected_uid": uid})
    return {"schema": "cce.bench-queries.v1", "queries": queries}


def build_synthetic_store(tmp_path: Path) -> tuple[Path, Path]:
    """Build and publish a RECORDS-record library; return (store root, library.sqlite)."""
    project, store, archive = (tmp_path / name for name in ("project", "store", "archive"))
    for path in (project, store, archive):
        path.mkdir()
    register_corpus_store(project, store)
    provider = archive / "provider-chatgpt"
    provider.mkdir()
    lines = [json.dumps(synthetic_conversation(i).to_dict()) for i in range(RECORDS)]
    (provider / "collection-chatgpt-conversations.jsonl").write_text("\n".join(lines) + "\n")
    manifest = {"schema": "cce.library-sources.v1", "archives": [str(archive)]}
    result = build_library(manifest, project_root=project, store_root=store)
    generation = Path(result["generation"])
    publish_library(generation, project_root=project, store_root=store)
    return store, generation / "library.sqlite"


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory):
    store, database = build_synthetic_store(tmp_path_factory.mktemp("bench-library"))
    return {"store": store, "database": database}


@pytest.fixture(scope="module")
def bench():
    assert SCRIPT.is_file(), "scripts/bench_mcp.py does not exist"
    spec = importlib.util.spec_from_file_location("bench_mcp_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest_tree(root: Path) -> dict[str, str]:
    """sha256 of every file plus a marker for every directory beneath root."""
    state = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_dir():
            state[relative + "/"] = ""
        else:
            state[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return state


def run(bench, capsys, *arguments):
    code = bench.main([str(argument) for argument in arguments])
    return code, capsys.readouterr()


def fast(library, queries=QUERIES, *extra):
    return [
        "--library",
        library,
        "--queries",
        queries,
        "--repeats",
        "2",
        "--cold",
        "inprocess",
        *extra,
    ]


def test_synthetic_library_has_at_least_two_hundred_records(synthetic):
    with Library(synthetic["database"]) as library:
        assert library.status()["records"] >= 200


def test_committed_queries_match_the_synthetic_builder():
    assert json.loads(QUERIES.read_text()) == synthetic_queries()
    assert len(synthetic_queries()["queries"]) >= 20


def test_defaults_are_the_documented_thresholds(bench):
    args = bench.build_parser().parse_args(["--library", "x", "--queries", "y"])
    assert args.max_warm_l0_p95_s == 1.0
    assert args.max_cold_first_call_s == 3.0
    assert args.max_l0_tokens == 900
    assert args.max_unit_calls == 2
    assert args.max_unit_tokens == 2500
    assert args.min_unit_rate == 0.9


def test_percentile_is_nearest_rank(bench):
    values = [float(n) for n in range(1, 21)]
    assert bench.percentile(values, 50) == 10.0
    assert bench.percentile(values, 95) == 19.0
    assert bench.percentile([4.0], 95) == 4.0
    assert bench.percentile([], 95) is None


def test_report_has_levels_cold_checks_and_token_basis(bench, synthetic, capsys):
    code, out = run(bench, capsys, *fast(synthetic["database"]))
    report = json.loads(out.out)
    assert code in (0, 1) and code == (0 if report["pass"] else 1)
    assert report["token_estimate"].startswith("ceil(response_bytes / 4)")
    assert report["library"]["mode"] == "file"
    for level in ("L0", "L2"):
        for phase in ("first", "warm"):
            stats = report["levels"][level][phase]
            assert stats["count"] > 0
            assert stats["wall_s"]["p50"] <= stats["wall_s"]["p95"] <= stats["wall_s"]["max"]
            assert stats["response_bytes"]["p95"] > 0 and stats["est_tokens"]["p95"] > 0
    assert report["cold"]["mode"] == "inprocess" and report["cold"]["first_call_s"] > 0
    names = {check["name"] for check in report["checks"]}
    assert names == {
        "warm_l0_p95_s",
        "cold_first_call_s",
        "l0_response_tokens",
        "unit_within_budget_rate",
    }
    assert report["units"]["queries"] == len(synthetic_queries()["queries"])
    assert "partial" in report and report["partial"]["partial_calls"] == 0


def test_store_root_mode_runs_the_unpatched_handlers(bench, synthetic, capsys):
    code, out = run(bench, capsys, *fast(synthetic["store"]))
    report = json.loads(out.out)
    assert report["library"]["mode"] == "store"
    assert report["units"]["reached"] == report["units"]["queries"]
    assert code in (0, 1)


def test_default_synthetic_run_passes_and_exits_zero(bench, synthetic, capsys):
    code, out = run(bench, capsys, *fast(synthetic["database"]))
    report = json.loads(out.out)
    assert report["pass"] is True and code == 0
    assert all(check["ok"] for check in report["checks"])
    assert report["units"]["within_budget_rate"] >= 0.9


def test_impossible_threshold_exits_one_and_names_the_failed_check(bench, synthetic, capsys):
    code, out = run(bench, capsys, *fast(synthetic["database"]), "--max-l0-tokens", "1")
    report = json.loads(out.out)
    assert code == 1 and report["pass"] is False
    failed = [check["name"] for check in report["checks"] if not check["ok"]]
    assert failed == ["l0_response_tokens"]


def test_cold_threshold_is_strict_less_than(bench, synthetic, capsys):
    """A real cold call is always slower than a zero limit; the exact boundary is pinned by
    test_cold_threshold_boundary_is_strict and test_check_relation_at_the_limit."""
    code, out = run(bench, capsys, *fast(synthetic["database"]), "--max-cold-first-call-s", "0")
    report = json.loads(out.out)
    assert code == 1
    assert [c["name"] for c in report["checks"] if not c["ok"]] == ["cold_first_call_s"]


COLD_LIMIT = 0.5  # exactly representable, so "equal to the limit" is exact in binary floating point


def test_check_relation_at_the_limit(bench):
    limit = COLD_LIMIT
    below, above = math.nextafter(limit, 0.0), math.nextafter(limit, 1.0)
    assert below < limit < above
    # "<" is strict: only a value below the limit passes, the limit itself fails.
    assert [bench.check("c", v, limit, "<")["ok"] for v in (below, limit, above)] == [
        True,
        False,
        False,
    ]
    # "<=" and ">=" include the limit, so equality is what tells them apart from "<".
    assert [bench.check("c", v, limit, "<=")["ok"] for v in (below, limit, above)] == [
        True,
        True,
        False,
    ]
    assert [bench.check("c", v, limit, ">=")["ok"] for v in (below, limit, above)] == [
        False,
        True,
        True,
    ]
    # A missing measurement never passes, whatever the relation.
    assert [bench.check("c", None, limit, r)["ok"] for r in ("<", "<=", ">=")] == [False] * 3
    entry = bench.check("c", limit, limit, "<")
    assert (entry["name"], entry["value"], entry["limit"], entry["relation"]) == (
        "c",
        limit,
        limit,
        "<",
    )


@pytest.mark.parametrize(
    ("label", "measured", "passes"),
    [
        ("just-below", math.nextafter(COLD_LIMIT, 0.0), True),
        ("equal", COLD_LIMIT, False),
        ("just-above", math.nextafter(COLD_LIMIT, 1.0), False),
    ],
)
def test_cold_threshold_boundary_is_strict(
    bench, synthetic, capsys, monkeypatch, label, measured, passes
):
    """The benchmark applies the cold gate as measured < limit: the injected measured value is
    compared, never a real timing, so a value equal to the limit must fail (it would pass if the
    gate were measured <= limit)."""
    real = bench.cold_inprocess

    def with_measured_time(target, query, args):
        return {**real(target, query, args), "wall_s": measured}

    monkeypatch.setattr(bench, "cold_inprocess", with_measured_time)
    arguments = fast(synthetic["database"], QUERIES, "--max-cold-first-call-s", str(COLD_LIMIT))
    code, out = run(bench, capsys, *arguments)
    report = json.loads(out.out)
    cold = next(item for item in report["checks"] if item["name"] == "cold_first_call_s")
    assert (cold["limit"], cold["relation"]) == (COLD_LIMIT, "<"), label
    assert cold["ok"] is passes, label
    if not passes:
        assert code == 1 and report["pass"] is False, label
        failed = [item["name"] for item in report["checks"] if not item["ok"]]
        assert "cold_first_call_s" in failed, label


def test_calls_to_unit_counts_search_then_reads(bench, synthetic, capsys, tmp_path):
    uid = synthetic_conversation(5).uid
    queries = {
        "schema": "cce.bench-queries.v1",
        "queries": [
            {"id": "record-only", "query": "fu0005q", "expected_uid": uid},
            {
                "id": "hinted-passage",
                "query": "ev0005q",
                "expected_uid": uid,
                "expected_text": "Evidence token ev0005q",
                "expected_message": 1,
            },
            {
                "id": "scanned-passage",
                "query": "ev0005q",
                "expected_uid": uid,
                "expected_text": "Follow up fu0005q",
            },
            {
                "id": "absent",
                "query": "ev0005q",
                "expected_uid": "chatgpt-bench-9999",
            },
        ],
    }
    path = tmp_path / "queries.json"
    path.write_text(json.dumps(queries))
    code, out = run(bench, capsys, *fast(synthetic["database"], path), "--include-calls")
    report = json.loads(out.out)
    by_id = {item["id"]: item for item in report["queries"]}
    assert by_id["record-only"]["reached"] is True and by_id["record-only"]["calls_to_unit"] == 1
    assert by_id["hinted-passage"]["calls_to_unit"] == 2
    assert by_id["hinted-passage"]["within_budget"] is True
    # No hint: the reader starts at message 0 and follows next_message, one call per message.
    assert by_id["scanned-passage"]["reached"] is True
    assert by_id["scanned-passage"]["calls_to_unit"] == 4
    assert by_id["scanned-passage"]["within_budget"] is False
    assert by_id["absent"]["reached"] is False and by_id["absent"]["within_budget"] is False
    assert report["units"]["reached"] == 3 and report["units"]["within_budget"] == 2
    assert report["units"]["within_budget_rate"] == 0.5
    assert code == 1
    assert {call["tool"] for call in report["calls"]} == {
        library_mcp.SEARCH_TOOL,
        library_mcp.READ_TOOL,
    }


def test_partial_flag_is_recorded_per_call_and_counted(bench, synthetic, capsys, monkeypatch):
    original = library_mcp.call_tool

    def partial(store_root, name, arguments):
        result = original(store_root, name, arguments)
        if name == library_mcp.SEARCH_TOOL:
            result["partial"] = True
        return result

    monkeypatch.setattr(library_mcp, "call_tool", partial)
    code, out = run(bench, capsys, *fast(synthetic["database"]), "--include-calls")
    report = json.loads(out.out)
    searches = [c for c in report["calls"] if c["tool"] == library_mcp.SEARCH_TOOL]
    reads = [c for c in report["calls"] if c["tool"] == library_mcp.READ_TOOL]
    assert searches and all(c["partial"] is True for c in searches)
    assert reads and all(c["partial"] is False for c in reads)
    assert report["partial"]["partial_calls"] >= len(searches)
    assert code in (0, 1)


def test_calls_over_the_partial_budget_without_a_flag_are_reported(
    bench, synthetic, capsys, monkeypatch
):
    clock = iter(range(0, 100000, 6))
    monkeypatch.setattr(bench.time, "perf_counter", lambda: next(clock))
    code, out = run(bench, capsys, *fast(synthetic["database"]), "--repeats", "1")
    report = json.loads(out.out)
    assert report["partial"]["partial_after_s"] == 5.0
    assert report["partial"]["slow_calls_not_flagged_partial"] > 0
    assert code in (0, 1)


@pytest.mark.parametrize("target", ["database", "store"])
def test_run_never_changes_the_library_bytes_or_directory(bench, synthetic, capsys, target):
    root = synthetic["store"]
    before = digest_tree(root)
    sibling_names = sorted(path.name for path in synthetic["database"].parent.iterdir())
    code, out = run(bench, capsys, *fast(synthetic[target]), "--hash")
    report = json.loads(out.out)
    assert code in (0, 1)
    assert digest_tree(root) == before
    assert sorted(path.name for path in synthetic["database"].parent.iterdir()) == sibling_names
    integrity = report["library"]["integrity"]
    assert integrity["unchanged"] is True and integrity["content_hashed"] is True
    assert integrity["hashed_files"] >= 1 and integrity["watched_entries"] >= 1
    assert not any(name.endswith(("-wal", "-shm", "-journal")) for name in sibling_names)


def test_library_is_only_ever_opened_read_only(bench, synthetic, capsys, monkeypatch):
    opened = []
    original = Library.__init__

    def spy(self, path, *, writable=False):
        opened.append(writable)
        original(self, path, writable=writable)

    monkeypatch.setattr(Library, "__init__", spy)
    run(bench, capsys, *fast(synthetic["database"]))
    assert opened and not any(opened)


def test_a_tool_that_writes_beside_the_library_is_detected(bench, synthetic, capsys, monkeypatch):
    original = library_mcp.call_tool
    intruder = synthetic["database"].parent / "stray.tmp"

    def writes(store_root, name, arguments):
        intruder.write_text("x")
        return original(store_root, name, arguments)

    monkeypatch.setattr(library_mcp, "call_tool", writes)
    try:
        code, out = run(bench, capsys, *fast(synthetic["database"]))
    finally:
        intruder.unlink(missing_ok=True)
    report = json.loads(out.out)
    assert code == 2 and report["pass"] is False
    assert report["library"]["integrity"]["unchanged"] is False
    assert "stray.tmp" in report["library"]["integrity"]["changed_entries"]


def test_missing_library_and_bad_queries_are_usage_errors_and_create_nothing(
    bench, synthetic, capsys, tmp_path
):
    missing = tmp_path / "absent" / "library.sqlite"
    code, out = run(bench, capsys, *fast(missing))
    assert code == 2 and out.out == "" and "library" in out.err
    assert not missing.parent.exists()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": "cce.bench-queries.v1", "queries": [{"id": "x"}]}))
    code, out = run(bench, capsys, *fast(synthetic["database"], bad))
    assert code == 2 and "queries" in out.err
    bad.write_text(json.dumps({"schema": "other", "queries": []}))
    code, out = run(bench, capsys, *fast(synthetic["database"], bad))
    assert code == 2


def test_cli_prints_json_and_exits_by_threshold_with_a_fresh_cold_process(synthetic):
    environment = dict(os.environ)
    source = str(ROOT / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        [source, *filter(None, [environment.get("PYTHONPATH")])]
    )
    command = [
        sys.executable,
        str(SCRIPT),
        "--library",
        str(synthetic["database"]),
        "--queries",
        str(QUERIES),
        "--repeats",
        "1",
        "--json",
    ]
    done = subprocess.run(
        command, capture_output=True, text=True, env=environment, timeout=300, check=False
    )
    report = json.loads(done.stdout)
    assert done.returncode in (0, 1) and done.returncode == (0 if report["pass"] else 1)
    assert report["cold"]["mode"] == "subprocess"
    assert report["cold"]["first_call_s"] > 0 and report["cold"]["process_wall_s"] > 0


def test_the_first_search_asks_for_l0_explicitly(bench):
    arguments = bench.first_search_arguments({"query": "q", "filters": {"wing": "ai"}}, 8)
    assert arguments == {"query": "q", "limit": 8, "offset": 0, "depth": "L0", "wing": "ai"}


def test_every_search_sent_requests_l0_and_is_reported_at_the_level_requested(
    bench, synthetic, capsys, monkeypatch
):
    """The server answers a search without `depth` at L1 (full, unbudgeted hits), so a harness that
    omits it would label those responses L0 and hold them to the 900-token L0 gate."""
    seen = []
    original = library_mcp.call_tool

    def spy(store_root, name, arguments):
        result = original(store_root, name, arguments)
        seen.append((name, dict(arguments), result))
        return result

    monkeypatch.setattr(library_mcp, "call_tool", spy)
    code, out = run(bench, capsys, *fast(synthetic["database"]), "--include-calls")
    report = json.loads(out.out)
    searches = [item for item in seen if item[0] == library_mcp.SEARCH_TOOL]
    assert searches and all(arguments.get("depth") == "L0" for _, arguments, _ in searches)
    # What came back is an L0 response: only uid, title, score and read, inside the budget.
    for _, _, result in searches:
        assert result["token_estimate"]["tokens"] <= 900
        assert all(set(hit) == {"uid", "title", "score", "read"} for hit in result["hits"])
    labelled = [call for call in report["calls"] if call["tool"] == library_mcp.SEARCH_TOOL]
    assert labelled and {call["level"] for call in labelled} == {"L0"}
    assert report["cold"]["est_tokens"] <= 900
    assert code in (0, 1)


def test_a_search_is_labelled_by_the_depth_it_requested(bench):
    class Server:
        def call_tool(self, tool, arguments):
            return {"content": [{"type": "text", "text": "{}"}], "structuredContent": {}}

    def level(tool, arguments):
        return bench.invoke(Server(), tool, arguments, phase="first", query_id="q")["level"]

    assert level(library_mcp.SEARCH_TOOL, {"query": "x", "depth": "L0"}) == "L0"
    assert level(library_mcp.SEARCH_TOOL, {"query": "x", "depth": "L1"}) == "L1"
    # No depth is the server's L1, never L0: it must not satisfy or be held to the L0 gate.
    assert level(library_mcp.SEARCH_TOOL, {"query": "x"}) == "L1"
    assert level(library_mcp.READ_TOOL, {"uid": "x"}) == "L2"
    assert level(library_mcp.HISTORY_TOOL, {"uid": "x"}) == "meta"


def test_without_any_l0_search_the_l0_gate_fails_instead_of_passing_vacuously(
    bench, synthetic, capsys, monkeypatch
):
    monkeypatch.setattr(
        bench,
        "first_search_arguments",
        lambda query, limit: {
            "query": query["query"],
            "limit": limit,
            "offset": 0,
        },
    )
    code, out = run(bench, capsys, *fast(synthetic["database"]))
    report = json.loads(out.out)
    gate = next(item for item in report["checks"] if item["name"] == "l0_response_tokens")
    assert gate["ok"] is False and gate["value"] is None
    assert "L0" not in report["levels"] and code == 1
