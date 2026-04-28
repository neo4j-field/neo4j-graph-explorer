"""Server-side Cypher proxy endpoint for the NVL viewer.

The browser must not hold Neo4j credentials. Instead, the NVL viewer running
inside the iframe POSTs a Cypher query to /api/cypher and receives back NVL-
shaped {nodes, relationships}. The query is validated read-only here, executed
through the same Neo4jClient the chat handlers use, and serialized via the
shared graph_renderer logic.

This endpoint is mounted onto Chainlit's underlying FastAPI application, so it
shares the same uvicorn process and origin as the chat UI. Same-origin means
no CORS gymnastics for the iframe.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from chainlit.server import app as chainlit_app
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from cypher_translator import CypherSafetyError, CypherTranslator
from graph_renderer import to_nvl_json
from neo4j_client import Neo4jClient

logger = logging.getLogger(__name__)

# Lazy-initialized so the endpoint module can be imported before the env is loaded.
_client: Neo4jClient | None = None


def _get_client() -> Neo4jClient:
    global _client
    if _client is None:
        _client = Neo4jClient.from_env()
    return _client


class CypherRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=10_000)
    params: dict[str, Any] = Field(default_factory=dict)
    max_nodes: int = Field(default=200, ge=1, le=1000)


@chainlit_app.post("/api/cypher")
async def cypher_endpoint(req: CypherRequest, request: Request) -> dict[str, Any]:
    # Block remote callers in production. The viewer iframe lives on the same
    # origin as Chainlit, so legitimate requests are always same-origin.
    # In dev (no auth), we still want to keep this strict.
    origin = request.headers.get("origin")
    host = request.headers.get("host")
    if origin and host and origin not in (f"http://{host}", f"https://{host}"):
        # Different origin -> reject. (Caller should hit /api/cypher from the
        # same Chainlit origin that served the iframe.)
        logger.warning("Rejected /api/cypher from foreign origin: %s (host=%s)", origin, host)
        raise HTTPException(status_code=403, detail="Cross-origin requests are not allowed.")

    try:
        CypherTranslator._reject_if_mutating(req.query)
    except CypherSafetyError as e:
        raise HTTPException(status_code=400, detail=f"Mutating Cypher rejected: {e}")

    try:
        client = _get_client()
        records = client.run_read(req.query, req.params)
    except Exception as e:
        logger.exception("Cypher execution failed: %s", req.query[:200])
        raise HTTPException(status_code=500, detail=f"Cypher execution failed: {e}")

    graph = to_nvl_json(records, max_nodes=req.max_nodes)
    return {
        "nodes": graph.nodes,
        "relationships": graph.relationships,
        "stats": {
            "node_count": graph.stats.node_count,
            "edge_count": graph.stats.edge_count,
            "label_distribution": graph.stats.label_distribution,
            "rel_type_distribution": graph.stats.rel_type_distribution,
            "truncated": graph.stats.truncated,
        },
    }
