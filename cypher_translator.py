"""Natural language to Cypher translation via the Anthropic API.

Design notes:
- claude-opus-4-7 with adaptive thinking. The model decides when to think.
- Schema is pinned into the system prompt and cached with a single ephemeral
  cache_control breakpoint. The schema is the largest stable chunk; caching it
  drives subsequent requests in a session to ~0.1x input cost.
- Output is constrained via output_config.format with a json_schema. This
  replaces the old prefill pattern (which 400s on Opus 4.7).
- Mutating Cypher is rejected here as a defense-in-depth layer on top of the
  read-only session in neo4j_client.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass

import anthropic

from neo4j_client import GraphSchema

logger = logging.getLogger(__name__)


_MUTATING_KEYWORDS = re.compile(
    r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|DETACH|FOREACH|LOAD\s+CSV)\b",
    flags=re.IGNORECASE,
)


_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "cypher": {
            "type": "string",
            "description": "A read-only Cypher query that answers the question. Must include LIMIT.",
        },
        "explanation": {
            "type": "string",
            "description": "One or two sentences describing what the query does.",
        },
        "expected_node_count": {
            "type": "integer",
            "description": "Best-guess upper bound on how many nodes the query returns.",
        },
    },
    "required": ["cypher", "explanation", "expected_node_count"],
    "additionalProperties": False,
}


_SYSTEM_HEADER = """You are a Cypher expert helping a graph data analyst explore a Neo4j Aura database through a chat interface.

Rules you must follow without exception:
1. Generate ONLY read-only Cypher. Never use CREATE, MERGE, DELETE, SET, REMOVE, DROP, DETACH, FOREACH, or LOAD CSV. If the user asks for a write, refuse and explain.
2. Always include a LIMIT clause. The visualization tool caps display at the configured node limit; pick a query LIMIT consistent with that.
3. Return data shaped for graph visualization: prefer returning whole nodes and whole relationships (e.g. RETURN n, r, m) rather than scalar projections, so the renderer can extract structure.
4. If the user asks something the schema cannot support, return a query that surfaces the limitation (for example, a query against a non-existent label should be rewritten against an existing one) and explain in the explanation.
5. Use case-correct label and relationship names exactly as listed in the schema below.

You will respond with a single JSON object matching the provided output schema. No prose outside the JSON."""


@dataclass
class TranslationResult:
    cypher: str
    explanation: str
    expected_node_count: int


class CypherSafetyError(ValueError):
    pass


class CypherTranslator:
    def __init__(
        self,
        schema: GraphSchema,
        node_cap: int,
        model: str | None = None,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self._client = client or anthropic.Anthropic()
        self._model = model or os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7")
        self._node_cap = node_cap
        self._system_blocks = self._build_system_blocks(schema, node_cap)

    @staticmethod
    def _build_system_blocks(schema: GraphSchema, node_cap: int) -> list[dict]:
        """Stable system prompt with one cache breakpoint on the last block.

        Render order: header, schema, cap reminder. Cap is small and stable,
        so it fits in the cached prefix. The user question varies per request
        and arrives outside the cached prefix as the message content.
        """
        return [
            {"type": "text", "text": _SYSTEM_HEADER},
            {"type": "text", "text": schema.to_prompt_block()},
            {
                "type": "text",
                "text": f"The visualization tool will display at most {node_cap} nodes. "
                "Choose a LIMIT that respects this. Prefer LIMIT 25 for general exploration "
                "unless the question implies a larger scan.",
                "cache_control": {"type": "ephemeral"},
            },
        ]

    def translate(self, question: str) -> TranslationResult:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=2048,
            thinking={"type": "adaptive"},
            system=self._system_blocks,
            output_config={"format": {"type": "json_schema", "schema": _OUTPUT_SCHEMA}},
            messages=[{"role": "user", "content": question}],
        )

        usage = response.usage
        logger.info(
            "translate: input=%s cached_read=%s cached_write=%s output=%s",
            usage.input_tokens,
            usage.cache_read_input_tokens,
            usage.cache_creation_input_tokens,
            usage.output_tokens,
        )

        text_block = next((b for b in response.content if b.type == "text"), None)
        if text_block is None:
            raise RuntimeError("Anthropic response contained no text block")

        try:
            payload = json.loads(text_block.text)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Anthropic response was not valid JSON: {text_block.text!r}"
            ) from e

        cypher = payload["cypher"].strip()
        self._reject_if_mutating(cypher)

        return TranslationResult(
            cypher=cypher,
            explanation=payload["explanation"],
            expected_node_count=int(payload["expected_node_count"]),
        )

    @staticmethod
    def _reject_if_mutating(cypher: str) -> None:
        # Strip string literals before checking, so the literal "CREATE" inside
        # a property value does not trip the guard.
        stripped = re.sub(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"", "''", cypher)
        match = _MUTATING_KEYWORDS.search(stripped)
        if match:
            raise CypherSafetyError(
                f"Generated query contains mutating keyword '{match.group(0)}'. "
                "This app only allows read-only queries."
            )
