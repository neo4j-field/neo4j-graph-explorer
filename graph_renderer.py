"""Convert Neo4j result records to graph data for NVL or PyVis.

Two output formats:
- `to_nvl_json` returns a dict shaped for the Neo4j NVL JS library
  (nodes: [{id, caption, color, properties}], relationships: [{id, from, to, caption, properties}])
- `render_records_to_html` writes a self-contained PyVis HTML file (legacy path)

Both walk every value in every record to pull out Node, Relationship, and Path
objects. Handles the common shapes returned by exploration queries: RETURN n,
RETURN n, r, m, RETURN path, etc.
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


@dataclass
class NvlGraph:
    """NVL-shaped graph data plus computed stats, ready for the viewer."""
    nodes: list[dict]
    relationships: list[dict]
    stats: GraphStats


def to_nvl_json(records: Iterable[Any], max_nodes: int) -> NvlGraph:
    """Walk records and produce data shaped for the @neo4j-nvl/base library.

    NVL nodes need at minimum {id}; relationships need {id, from, to}. We add
    caption (the value rendered on the node), color (deterministic from primary
    label), labels (list), and a flat properties dict for tooltip display.
    """
    nodes: dict[str, Any] = {}
    edges: dict[str, Any] = {}
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

    nvl_nodes: list[dict] = []
    for node in nodes.values():
        primary_label = next(iter(node.labels), "Node")
        nvl_nodes.append({
            "id": str(node.element_id),
            "caption": _node_caption(node),
            "color": label_to_color.get(primary_label, "#888888"),
            "labels": list(node.labels),
            "properties": _safe_props(node),
        })

    nvl_rels: list[dict] = []
    for rel in edges.values():
        start = str(rel.start_node.element_id)
        end = str(rel.end_node.element_id)
        if start in {str(k) for k in nodes} and end in {str(k) for k in nodes}:
            nvl_rels.append({
                "id": str(rel.element_id),
                "from": start,
                "to": end,
                "caption": rel.type,
                "type": rel.type,
                "properties": _safe_props(rel),
            })

    label_distribution: dict[str, int] = {}
    for n in nodes.values():
        for label in n.labels:
            label_distribution[label] = label_distribution.get(label, 0) + 1

    rel_type_distribution: dict[str, int] = {}
    for r in edges.values():
        rel_type_distribution[r.type] = rel_type_distribution.get(r.type, 0) + 1

    stats = GraphStats(
        node_count=len(nodes),
        edge_count=len(nvl_rels),
        label_distribution=dict(sorted(label_distribution.items(), key=lambda kv: -kv[1])),
        rel_type_distribution=dict(sorted(rel_type_distribution.items(), key=lambda kv: -kv[1])),
        truncated=truncated,
    )
    return NvlGraph(nodes=nvl_nodes, relationships=nvl_rels, stats=stats)


def _safe_props(entity: Any) -> dict[str, str]:
    """Stringify property values for safe JSON serialization and tooltip display."""
    out: dict[str, str] = {}
    try:
        items = entity.items()
    except Exception:
        return out
    for k, v in items:
        try:
            out[k] = _truncate(v)
        except Exception:
            out[k] = "<unprintable>"
    return out


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
