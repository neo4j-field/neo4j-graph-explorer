"""Neo4j Aura client: driver lifecycle, schema introspection, read-only execution."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from neo4j import GraphDatabase, READ_ACCESS, Driver, Record
from neo4j.exceptions import Neo4jError, ServiceUnavailable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GraphSchema:
    labels: list[str]
    relationship_types: list[str]
    property_keys: list[str]
    node_label_properties: dict[str, list[str]] = field(default_factory=dict)
    rel_type_properties: dict[str, list[str]] = field(default_factory=dict)

    def to_prompt_block(self) -> str:
        """Render the schema as a stable, deterministic block for the LLM prompt.

        Stable ordering matters: the schema is part of the prompt-cache prefix,
        so any non-deterministic serialization would silently invalidate cache hits.
        """
        lines = ["# Neo4j Schema", ""]

        lines.append("## Node labels")
        for label in sorted(self.labels):
            props = sorted(self.node_label_properties.get(label, []))
            if props:
                lines.append(f"- :{label} (properties: {', '.join(props)})")
            else:
                lines.append(f"- :{label}")
        lines.append("")

        lines.append("## Relationship types")
        for rel in sorted(self.relationship_types):
            props = sorted(self.rel_type_properties.get(rel, []))
            if props:
                lines.append(f"- [:{rel}] (properties: {', '.join(props)})")
            else:
                lines.append(f"- [:{rel}]")
        lines.append("")

        lines.append("## All property keys")
        lines.append(", ".join(sorted(self.property_keys)) or "(none)")
        return "\n".join(lines)


class Neo4jClient:
    """Thin wrapper around the official driver, configured for read-only chat use."""

    def __init__(
        self,
        uri: str,
        username: str,
        password: str,
        database: str = "neo4j",
        query_timeout_seconds: int = 30,
    ) -> None:
        self._uri = uri
        self._database = database
        self._query_timeout_seconds = query_timeout_seconds
        self._driver: Driver = GraphDatabase.driver(uri, auth=(username, password))

    @classmethod
    def from_env(cls) -> "Neo4jClient":
        return cls(
            uri=_required("NEO4J_URI"),
            username=_required("NEO4J_USERNAME"),
            password=_required("NEO4J_PASSWORD"),
            database=os.environ.get("NEO4J_DATABASE", "neo4j"),
            query_timeout_seconds=int(os.environ.get("QUERY_TIMEOUT_SECONDS", "30")),
        )

    def verify_connectivity(self) -> None:
        try:
            self._driver.verify_connectivity()
        except ServiceUnavailable as e:
            raise RuntimeError(
                f"Cannot reach Neo4j at {self._uri}. Verify the instance is running "
                f"and credentials are correct."
            ) from e

    def fetch_schema(self) -> GraphSchema:
        """Introspect labels, relationship types, and properties.

        Uses APOC-free Cypher so it works on any Aura instance. The detailed
        per-label property breakdown uses db.schema.nodeTypeProperties() which
        is built into Neo4j 5.x.
        """
        with self._driver.session(database=self._database, default_access_mode=READ_ACCESS) as session:
            labels = [r["label"] for r in session.run("CALL db.labels() YIELD label RETURN label")]
            rel_types = [
                r["relationshipType"]
                for r in session.run("CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType")
            ]
            prop_keys = [
                r["propertyKey"]
                for r in session.run("CALL db.propertyKeys() YIELD propertyKey RETURN propertyKey")
            ]

            node_label_properties: dict[str, list[str]] = {}
            try:
                for record in session.run(
                    "CALL db.schema.nodeTypeProperties() "
                    "YIELD nodeLabels, propertyName "
                    "RETURN nodeLabels, propertyName"
                ):
                    prop_name = record["propertyName"]
                    if not prop_name:
                        # Labels with no properties surface as a row with propertyName=null.
                        continue
                    for label in record["nodeLabels"] or []:
                        if label:
                            node_label_properties.setdefault(label, []).append(prop_name)
            except Neo4jError as e:
                logger.warning("nodeTypeProperties introspection failed: %s", e)

            rel_type_properties: dict[str, list[str]] = {}
            try:
                for record in session.run(
                    "CALL db.schema.relTypeProperties() "
                    "YIELD relType, propertyName "
                    "RETURN relType, propertyName"
                ):
                    rel = (record["relType"] or "").strip(":`")
                    prop_name = record["propertyName"]
                    if rel and prop_name:
                        rel_type_properties.setdefault(rel, []).append(prop_name)
            except Neo4jError as e:
                logger.warning("relTypeProperties introspection failed: %s", e)

            for v in node_label_properties.values():
                v[:] = sorted(set(v))
            for v in rel_type_properties.values():
                v[:] = sorted(set(v))

            return GraphSchema(
                labels=sorted(set(labels)),
                relationship_types=sorted(set(rel_types)),
                property_keys=sorted(set(prop_keys)),
                node_label_properties=node_label_properties,
                rel_type_properties=rel_type_properties,
            )

    def run_read(self, cypher: str, parameters: dict[str, Any] | None = None) -> list[Record]:
        """Execute a Cypher query in a read-only session with a timeout."""
        with self._driver.session(database=self._database, default_access_mode=READ_ACCESS) as session:
            result = session.run(cypher, parameters or {}, timeout=self._query_timeout_seconds)
            return list(result)

    def close(self) -> None:
        self._driver.close()


# Process-wide default client. Both Chainlit chat sessions and the /api/cypher
# endpoint share this so we don't accumulate per-session drivers (each carries
# its own routing table and can grow stale if it sits idle while another path
# stays warm). Created lazily on first access; closed when the process exits.
_default_client: "Neo4jClient | None" = None


def get_default_client() -> "Neo4jClient":
    global _default_client
    if _default_client is None:
        _default_client = Neo4jClient.from_env()
    return _default_client


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value
