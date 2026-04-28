"""Tool-runner-based Cypher agent.

Uses the Anthropic SDK's `client.beta.messages.tool_runner()` to let Claude
iterate on its own queries: generate a Cypher candidate, run it through the
`run_cypher` tool, evaluate the summary, refine and retry if needed, stop
when satisfied. The agent loop is managed entirely by the SDK; we just
declare the tool and read back the result.
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

import anthropic
from anthropic import beta_tool
from neo4j.graph import Node, Path, Relationship

from cypher_translator import CypherSafetyError, CypherTranslator
from neo4j_client import GraphSchema, Neo4jClient

logger = logging.getLogger(__name__)


_AGENT_SYSTEM_HEADER = """You are a Cypher expert helping a graph data analyst explore a Neo4j Aura database through a chat interface.

You have one tool: run_cypher(query). Call it to execute candidate Cypher against the live database and inspect the summary.

How to operate:
1. Generate a Cypher candidate. Always read-only (no CREATE, MERGE, DELETE, SET, REMOVE, DROP, DETACH, FOREACH, or LOAD CSV). Always include a LIMIT clause.
2. Prefer returning whole nodes and whole relationships, e.g. `RETURN n, r, m`, so the visualizer has structure to render. Avoid scalar projections unless the user explicitly asked for one.
3. Use exact label and relationship-type names from the schema below.
4. Call run_cypher with your candidate. Inspect the summary.
5. Refine and retry only when there is a real problem: an error came back, zero rows when the user clearly expected some, the wrong labels showed up, or the result is a scalar projection when a graph view was wanted. If the first query was correct, stop immediately.
6. After the final successful run, give the user one or two sentences explaining what you found. The harness handles visualization of the final result on its own; do not embed Cypher in your final message.

Hard rules:
- Do not loop unnecessarily. Most questions need exactly one run_cypher call.
- Do not invent labels or properties not present in the schema."""


@dataclass
class IterationStep:
    """One iteration of the agent loop, captured for UI display."""
    index: int
    query: str | None = None
    tool_summary: str | None = None
    text: str | None = None


@dataclass
class AgentResult:
    answer: str
    final_query: str | None
    final_records: list[Any]
    iterations: int
    steps: list[IterationStep] = field(default_factory=list)


class CypherAgent:
    """Anthropic tool-runner that iterates Cypher against Aura.

    Construct once per chat session. Each `answer()` call runs an independent
    agent loop. The agent shares the same prompt-cached schema as the single-shot
    translator.
    """

    def __init__(
        self,
        neo4j_client: Neo4jClient,
        schema: GraphSchema,
        node_cap: int,
        model: str | None = None,
    ) -> None:
        self._neo4j = neo4j_client
        self._client = anthropic.Anthropic()
        self._model = model or os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7")
        self._node_cap = node_cap
        self._last_query: str | None = None
        self._last_records: list[Any] = []

        self._system_blocks = [
            {"type": "text", "text": _AGENT_SYSTEM_HEADER},
            {"type": "text", "text": schema.to_prompt_block()},
            {
                "type": "text",
                "text": (
                    f"The visualization tool will display at most {node_cap} nodes. "
                    "Pick LIMIT clauses that respect this; LIMIT 25 is a good default for exploration."
                ),
                "cache_control": {"type": "ephemeral"},
            },
        ]

        # Closure: capture the agent and driver references for the tool body.
        agent = self

        @beta_tool
        def run_cypher(query: str) -> str:
            """Execute a read-only Cypher query against the Neo4j database and return a summary.

            The summary reports: row count, result keys, distinct node count by label,
            relationship count by type, and any errors. Use it to judge whether the
            query answered the user's question.

            Args:
                query: A read-only Cypher query. Must include a LIMIT clause.
            """
            try:
                CypherTranslator._reject_if_mutating(query)
            except CypherSafetyError as e:
                return f"REJECTED (safety): {e}"
            try:
                records = neo4j_client.run_read(query)
            except Exception as e:
                return (
                    f"ERROR executing query: {e}\n"
                    f"The query may have a syntax issue or reference a label/property that "
                    f"does not exist. Check the schema and revise."
                )
            agent._last_query = query
            agent._last_records = records
            return _summarize_records(records)

        self._tool = run_cypher

    def answer(self, question: str) -> AgentResult:
        """Run the agent loop synchronously to completion."""
        self._last_query = None
        self._last_records = []

        runner = self._client.beta.messages.tool_runner(
            model=self._model,
            max_tokens=4096,
            thinking={"type": "adaptive"},
            system=self._system_blocks,
            tools=[self._tool],
            messages=[{"role": "user", "content": question}],
        )

        steps: list[IterationStep] = []
        final_answer_parts: list[str] = []
        iteration = 0

        for message in runner:
            iteration += 1
            step = IterationStep(index=iteration)
            for block in message.content:
                if block.type == "text" and block.text:
                    step.text = block.text
                    final_answer_parts.append(block.text)
                elif block.type == "tool_use" and block.name == "run_cypher":
                    step.query = block.input.get("query", "")
                    logger.info("agent iter %d query: %s", iteration, step.query)
            steps.append(step)

        # Pair tool calls with their summary by walking again. The tool_result
        # blocks live on subsequent user-role messages emitted by the runner;
        # rather than parse those, we reuse what the tool itself returned via
        # the agent state for the *last* run.
        if self._last_query and steps:
            for step in reversed(steps):
                if step.query == self._last_query:
                    step.tool_summary = _summarize_records(self._last_records)
                    break

        return AgentResult(
            answer="\n\n".join(p for p in final_answer_parts if p) or "(no final text)",
            final_query=self._last_query,
            final_records=self._last_records,
            iterations=iteration,
            steps=steps,
        )


def _summarize_records(records: list[Any]) -> str:
    if not records:
        return (
            "Returned 0 rows. The query executed successfully but produced no data. "
            "Check that label and relationship names match the schema exactly (case-sensitive), "
            "and that the WHERE clause is not too restrictive."
        )

    label_counter: Counter[str] = Counter()
    rel_counter: Counter[str] = Counter()
    seen_node_ids: set[str] = set()

    for record in records:
        for value in record.values():
            for node, rel in _walk(value):
                if node is not None and node.element_id not in seen_node_ids:
                    seen_node_ids.add(node.element_id)
                    for label in node.labels:
                        label_counter[label] += 1
                if rel is not None:
                    rel_counter[rel.type] += 1

    lines = [
        f"Returned {len(records)} row(s).",
        f"Result keys: {list(records[0].keys())}",
    ]
    if label_counter:
        lines.append(f"Distinct nodes by label: {dict(label_counter)}")
    if rel_counter:
        lines.append(f"Relationships by type: {dict(rel_counter)}")
    if not label_counter and not rel_counter:
        lines.append(
            "Result contains no graph entities (looks like scalar projection). "
            "If a graph view is desired, return whole nodes and relationships instead."
        )
    return "\n".join(lines)


def _walk(value: Any) -> Iterable[tuple[Node | None, Relationship | None]]:
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
            yield from _walk(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
