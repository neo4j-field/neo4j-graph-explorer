"""Render Neo4j result records to interactive PyVis HTML.

Walks every value in every record and pulls out Node and Relationship
instances. This handles the common shapes returned by typical exploration
queries: RETURN n, RETURN n, r, m, RETURN path, etc.
"""

from __future__ import annotations

import colorsys
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from neo4j.graph import Node, Relationship, Path
from pyvis.network import Network

logger = logging.getLogger(__name__)


@dataclass
class GraphStats:
    node_count: int
    edge_count: int
    label_distribution: dict[str, int]
    rel_type_distribution: dict[str, int]
    truncated: bool


def render_records_to_html(
    records: Iterable[Any],
    output_path: Path,
    max_nodes: int,
) -> GraphStats:
    """Extract nodes and relationships from records and write a PyVis HTML file."""
    nodes: dict[int, Node] = {}
    edges: dict[int, Relationship] = {}
    truncated = False

    for record in records:
        for value in record.values():
            for n, r in _walk_for_graph_objects(value):
                if n is not None:
                    if n.element_id not in nodes:
                        if len(nodes) >= max_nodes:
                            truncated = True
                            continue
                        nodes[n.element_id] = n
                if r is not None:
                    edges[r.element_id] = r

    label_to_color = _assign_label_colors({label for n in nodes.values() for label in n.labels})

    net = Network(
        height="600px",
        width="100%",
        bgcolor="#1f1f23",
        font_color="#f0f0f0",
        directed=True,
        notebook=False,
        cdn_resources="remote",
    )
    net.set_options(_NETWORK_OPTIONS)

    for node in nodes.values():
        primary_label = next(iter(node.labels), "Node")
        net.add_node(
            n_id=str(node.element_id),
            label=_node_caption(node),
            title=_node_tooltip(node),
            color=label_to_color.get(primary_label, "#888888"),
            group=primary_label,
        )

    for rel in edges.values():
        start = str(rel.start_node.element_id)
        end = str(rel.end_node.element_id)
        if start in {str(k) for k in nodes} and end in {str(k) for k in nodes}:
            net.add_edge(start, end, label=rel.type, title=_rel_tooltip(rel))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    net.write_html(str(output_path), notebook=False, open_browser=False)

    label_distribution: dict[str, int] = {}
    for n in nodes.values():
        for label in n.labels:
            label_distribution[label] = label_distribution.get(label, 0) + 1

    rel_type_distribution: dict[str, int] = {}
    for r in edges.values():
        rel_type_distribution[r.type] = rel_type_distribution.get(r.type, 0) + 1

    return GraphStats(
        node_count=len(nodes),
        edge_count=len(edges),
        label_distribution=dict(sorted(label_distribution.items(), key=lambda kv: -kv[1])),
        rel_type_distribution=dict(sorted(rel_type_distribution.items(), key=lambda kv: -kv[1])),
        truncated=truncated,
    )


def _walk_for_graph_objects(value: Any):
    """Yield (node, relationship) tuples found anywhere in a result value.

    One of the two will be None per yield. Recurses into lists, dicts, and
    Path objects so nested return shapes work without the caller pre-flattening.
    """
    if value is None:
        return
    if isinstance(value, Node):
        yield value, None
    elif isinstance(value, Relationship):
        yield value.start_node, None
        yield value.end_node, None
        yield None, value
    elif isinstance(value, Path):
        for n in value.nodes:
            yield n, None
        for r in value.relationships:
            yield None, r
    elif isinstance(value, list):
        for item in value:
            yield from _walk_for_graph_objects(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_for_graph_objects(item)


def _node_caption(node: Node) -> str:
    for key in ("name", "title", "id", "code", "email"):
        if key in node:
            return f"{str(node[key])[:40]}"
    primary_label = next(iter(node.labels), "Node")
    return f"{primary_label}"


def _node_tooltip(node: Node) -> str:
    labels = ":".join(node.labels)
    props = "\n".join(f"  {k}: {_truncate(v)}" for k, v in node.items())
    return f":{labels}\n{props}" if props else f":{labels}"


def _rel_tooltip(rel: Relationship) -> str:
    props = "\n".join(f"  {k}: {_truncate(v)}" for k, v in rel.items())
    return f"[:{rel.type}]\n{props}" if props else f"[:{rel.type}]"


def _truncate(value: Any, length: int = 120) -> str:
    text = str(value)
    return text if len(text) <= length else text[: length - 3] + "..."


def _assign_label_colors(labels: set[str]) -> dict[str, str]:
    """Deterministic, evenly-spaced HSL palette per label.

    Keeps color stable across queries in the same session by hashing the label.
    """
    colors: dict[str, str] = {}
    for label in sorted(labels):
        h = int(hashlib.sha1(label.encode()).hexdigest(), 16) % 360 / 360.0
        r, g, b = colorsys.hls_to_rgb(h, 0.55, 0.65)
        colors[label] = f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"
    return colors


_NETWORK_OPTIONS = """
{
  "physics": {
    "enabled": true,
    "stabilization": {"iterations": 200},
    "barnesHut": {
      "gravitationalConstant": -8000,
      "springLength": 120,
      "springConstant": 0.04,
      "damping": 0.3
    }
  },
  "edges": {
    "smooth": {"type": "continuous"},
    "arrows": {"to": {"enabled": true, "scaleFactor": 0.6}},
    "color": {"color": "#9aa0a6"},
    "font": {"color": "#cfd2d6", "size": 11, "strokeWidth": 0}
  },
  "nodes": {
    "shape": "dot",
    "size": 18,
    "font": {"color": "#f0f0f0", "size": 13}
  },
  "interaction": {
    "hover": true,
    "navigationButtons": true,
    "tooltipDelay": 100
  }
}
"""
