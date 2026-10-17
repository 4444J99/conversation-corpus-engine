from __future__ import annotations

import re
from collections import Counter
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from .archive_ingest import ArchivePaths, load_index, require_inside
from .archive_model import write_json_atomic, write_jsonl_atomic, write_text_atomic
from .archive_topics import load_enrichment
from .corpus_store import CorpusWriteAuthorization

XML_ILLEGAL_RE = re.compile("[^\u0009\u000a\u000d -퟿-�]")
GRAPHML_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">\n'
    '  <key id="label" for="node" attr.name="label" attr.type="string"/>\n'
    '  <key id="kind" for="node" attr.name="kind" attr.type="string"/>\n'
    '  <key id="provider" for="node" attr.name="provider" attr.type="string"/>\n'
    '  <key id="created" for="node" attr.name="created" attr.type="string"/>\n'
    '  <key id="size" for="node" attr.name="size" attr.type="int"/>\n'
    '  <key id="ekind" for="edge" attr.name="kind" attr.type="string"/>\n'
    '  <key id="weight" for="edge" attr.name="weight" attr.type="double"/>\n'
    '  <graph id="archive" edgedefault="undirected">\n'
)
GRAPHML_TAIL = "  </graph>\n</graphml>\n"


def _xml(text: str) -> str:
    return XML_ILLEGAL_RE.sub("", text)


def _components(nodes: set[str], edges: list[dict[str, Any]]) -> list[int]:
    parent = {node: node for node in nodes}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for edge in edges:
        a, b = edge["source"], edge["target"]
        if a in parent and b in parent:
            parent[find(a)] = find(b)
    sizes = Counter(find(node) for node in nodes)
    return sorted(sizes.values(), reverse=True)


def build_graph_tables(paths: ArchivePaths) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = load_index(paths)
    enrichment = load_enrichment(paths)
    providers = {row["uid"]: row["provider"] for row in rows}
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    similar: dict[tuple[str, str], float] = {}
    topic_docs: Counter[str] = Counter()
    for row in rows:
        nodes[row["uid"]] = {
            "id": row["uid"],
            "kind": "conversation",
            "label": row["title"],
            "provider": row["provider"],
            "created": row["created"],
            "size": row["message_count"],
        }
        provider_id = f"provider:{row['provider']}"
        nodes.setdefault(
            provider_id,
            {
                "id": provider_id,
                "kind": "provider",
                "label": row["provider"],
                "provider": row["provider"],
                "created": "",
                "size": 0,
            },
        )
        edges.append(
            {"source": row["uid"], "target": provider_id, "kind": "member_of", "weight": 1.0}
        )
        if row["collection"]:
            collection_id = f"collection:{row['provider']}:{row['collection']}"
            nodes.setdefault(
                collection_id,
                {
                    "id": collection_id,
                    "kind": "collection",
                    "label": row["collection"],
                    "provider": row["provider"],
                    "created": "",
                    "size": 0,
                },
            )
            edges.append(
                {
                    "source": row["uid"],
                    "target": collection_id,
                    "kind": "in_collection",
                    "weight": 1.0,
                }
            )
        extra = enrichment.get(row["uid"], {})
        for term in extra.get("topics", []):
            topic_id = f"topic:{term}"
            nodes.setdefault(
                topic_id,
                {
                    "id": topic_id,
                    "kind": "topic",
                    "label": term,
                    "provider": "",
                    "created": "",
                    "size": 0,
                },
            )
            topic_docs[topic_id] += 1
            edges.append({"source": row["uid"], "target": topic_id, "kind": "about", "weight": 1.0})
        for neighbour in extra.get("similar", []):
            if neighbour["uid"] not in providers:
                continue
            pair = tuple(sorted((row["uid"], neighbour["uid"])))
            similar[pair] = max(similar.get(pair, 0.0), float(neighbour["score"]))
    for topic_id, count in topic_docs.items():
        nodes[topic_id]["size"] = count
    for (a, b), score in sorted(similar.items()):
        edges.append({"source": a, "target": b, "kind": "similar", "weight": round(score, 4)})
    ordered_nodes = [nodes[key] for key in sorted(nodes)]
    ordered_edges = sorted(edges, key=lambda e: (e["kind"], e["source"], e["target"]))
    return ordered_nodes, ordered_edges


def render_graphml(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> str:
    out = [GRAPHML_HEAD]
    for node in nodes:
        out.append(f"    <node id={quoteattr(_xml(node['id']))}>\n")
        for key in ("label", "kind", "provider", "created"):
            out.append(f'      <data key="{key}">{escape(_xml(str(node[key])))}</data>\n')
        out.append(f'      <data key="size">{int(node["size"])}</data>\n    </node>\n')
    for index, edge in enumerate(edges):
        out.append(
            f'    <edge id="e{index}" source={quoteattr(_xml(edge["source"]))} '
            f"target={quoteattr(_xml(edge['target']))}>\n"
            f'      <data key="ekind">{edge["kind"]}</data>\n'
            f'      <data key="weight">{edge["weight"]}</data>\n    </edge>\n'
        )
    out.append(GRAPHML_TAIL)
    return "".join(out)


def build_graph(paths: ArchivePaths, authorization: CorpusWriteAuthorization) -> dict[str, Any]:
    require_inside(authorization, paths.graph)
    nodes, edges = build_graph_tables(paths)
    write_jsonl_atomic(paths.graph_nodes, nodes)
    write_jsonl_atomic(paths.graph_edges, edges)
    write_text_atomic(paths.graph_graphml, render_graphml(nodes, edges))
    by_id = {node["id"]: node for node in nodes}
    degree: Counter[str] = Counter()
    for edge in edges:
        degree[edge["source"]] += 1
        degree[edge["target"]] += 1
    cross = sum(
        1
        for edge in edges
        if edge["kind"] == "similar"
        and by_id[edge["source"]]["provider"] != by_id[edge["target"]]["provider"]
    )
    bridging_topics = 0
    topic_providers: dict[str, set[str]] = {}
    for edge in edges:
        if edge["kind"] == "about":
            topic_providers.setdefault(edge["target"], set()).add(by_id[edge["source"]]["provider"])
    bridging_topics = sum(1 for provs in topic_providers.values() if len(provs) > 1)
    content_nodes = {n["id"] for n in nodes if n["kind"] != "provider"}
    content_edges = [e for e in edges if e["kind"] != "member_of"]
    components = _components(content_nodes, content_edges)
    summary = {
        "nodes": len(nodes),
        "edges": len(edges),
        "node_kinds": dict(sorted(Counter(n["kind"] for n in nodes).items())),
        "edge_kinds": dict(sorted(Counter(e["kind"] for e in edges).items())),
        "cross_provider_similar_edges": cross,
        "topics_bridging_providers": bridging_topics,
        "components": len(components),
        "largest_component": components[0] if components else 0,
        "isolated_conversations": sum(
            1 for n in nodes if n["kind"] == "conversation" and degree[n["id"]] <= 1
        ),
        "top_hubs": [
            {"id": key, "label": by_id[key]["label"], "degree": count}
            for key, count in sorted(degree.items(), key=lambda kv: (-kv[1], kv[0]))
            if by_id[key]["kind"] != "provider"
        ][:15],
    }
    write_json_atomic(paths.graph_summary, summary)
    return summary
