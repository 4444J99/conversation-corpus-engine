from __future__ import annotations

import json
import xml.etree.ElementTree as ET

from archive_support import conv, topical_corpus, write_store

from conversation_corpus_engine.archive_graph import build_graph, build_graph_tables, render_graphml
from conversation_corpus_engine.archive_model import iter_jsonl
from conversation_corpus_engine.archive_topics import build_analysis


def _prepare(env):
    write_store(env.paths, topical_corpus())
    build_analysis(env.paths)


def test_graph_tables_link_conversations_topics_and_providers(archive_env) -> None:
    _prepare(archive_env)
    nodes, edges = build_graph_tables(archive_env.paths)
    kinds = {n["kind"] for n in nodes}
    assert kinds == {"conversation", "provider", "topic"}
    ids = {n["id"] for n in nodes}
    assert {"topic:kubernetes", "topic:sourdough", "provider:chatgpt"} <= ids
    for edge in edges:
        assert edge["source"] in ids and edge["target"] in ids
    about = [e for e in edges if e["kind"] == "about" and e["target"] == "topic:kubernetes"]
    assert len(about) == 4
    similar = {(e["source"], e["target"]) for e in edges if e["kind"] == "similar"}
    assert all(a < b for a, b in similar), "undirected similar pairs are stored once, sorted"
    assert ("chatgpt-k0", "claude-k1") in similar


def test_graph_files_summary_and_graphml_are_valid(archive_env) -> None:
    _prepare(archive_env)
    summary = build_graph(archive_env.paths, archive_env.auth)
    root = archive_env.paths.graph
    kinds = summary["node_kinds"]
    assert kinds["conversation"] == 12 and kinds["provider"] == 5 and kinds["topic"] >= 2
    assert summary["topics_bridging_providers"] == kinds["topic"]
    assert summary["cross_provider_similar_edges"] >= 4
    assert summary["isolated_conversations"] == 5
    assert summary["largest_component"] == 6, "four kubernetes conversations plus two topic hubs"
    assert summary["components"] == 2 + 5
    degrees = [hub["degree"] for hub in summary["top_hubs"]]
    assert degrees == sorted(degrees, reverse=True) and degrees[0] > 1
    assert not any(hub["id"].startswith("provider:") for hub in summary["top_hubs"])
    tree = ET.parse(root / "archive.graphml")
    ns = {"g": "http://graphml.graphdrawing.org/xmlns"}
    assert len(tree.findall(".//g:node", ns)) == summary["nodes"]
    assert len(tree.findall(".//g:edge", ns)) == summary["edges"]
    assert sum(1 for _ in iter_jsonl(root / "nodes.jsonl")) == summary["nodes"]
    assert json.loads((root / "summary.json").read_text(encoding="utf-8")) == summary


def test_graphml_survives_hostile_labels() -> None:
    nodes = [
        {
            "id": 'a"<b>',
            "kind": "conversation",
            "label": "x & <y> \x00\x08 ok",
            "provider": "p",
            "created": "",
            "size": 1,
        },
        {"id": "c", "kind": "topic", "label": "t", "provider": "", "created": "", "size": 0},
    ]
    edges = [{"source": 'a"<b>', "target": "c", "kind": "about", "weight": 1.0}]
    root = ET.fromstring(render_graphml(nodes, edges))
    labels = [
        d.text
        for d in root.iter("{http://graphml.graphdrawing.org/xmlns}data")
        if d.get("key") == "label"
    ]
    assert labels[0] == "x & <y>  ok"


def test_graph_is_deterministic(archive_env) -> None:
    _prepare(archive_env)
    build_graph(archive_env.paths, archive_env.auth)
    first = {p.name: p.read_bytes() for p in archive_env.paths.graph.iterdir()}
    build_graph(archive_env.paths, archive_env.auth)
    assert {p.name: p.read_bytes() for p in archive_env.paths.graph.iterdir()} == first


def test_graph_handles_collections_and_missing_enrichment(archive_env) -> None:
    write_store(
        archive_env.paths,
        [
            conv("claude", "p1", "Project", kind="notebook", collection="proj-1"),
            conv("claude", "c1", "Chat", collection="proj-1"),
        ],
    )
    nodes, edges = build_graph_tables(archive_env.paths)
    assert any(n["id"] == "collection:claude:proj-1" for n in nodes)
    assert sum(1 for e in edges if e["kind"] == "in_collection") == 2
