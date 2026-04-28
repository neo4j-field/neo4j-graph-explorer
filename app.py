"""Chainlit chat handlers for the Neo4j Graph Explorer."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

import chainlit as cl
from dotenv import load_dotenv

from cypher_agent import CypherAgent
from cypher_translator import CypherSafetyError, CypherTranslator
from graph_renderer import GraphStats, to_nvl_json
from neo4j_client import Neo4jClient, get_default_client

# Side-effect import: registers POST /api/cypher on Chainlit's FastAPI app.
import cypher_api  # noqa: F401

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("app")

OUTPUT_DIR = Path("public/graphs")  # legacy PyVis dumps (still gitignored)
NVL_DATA_DIR = Path("public/nvl/data")  # per-message seed JSON for the NVL viewer
MAX_NODES = int(os.environ.get("MAX_NODES", "200"))


@cl.on_chat_start
async def on_chat_start() -> None:
    """Connect to Neo4j, introspect the schema, prime the translator, greet the user."""
    try:
        client = get_default_client()
        client.verify_connectivity()
    except Exception as e:
        await cl.Message(
            content=f"**Could not connect to Neo4j.**\n\n```\n{e}\n```\n\n"
            "Check your `.env` file. Aura takes about 60 seconds to come online "
            "after creation, so retry shortly if the instance is fresh."
        ).send()
        raise

    await cl.Message(content="Connected to Aura. Fetching schema...").send()

    try:
        schema = client.fetch_schema()
    except Exception as e:
        await cl.Message(content=f"Schema introspection failed: `{e}`").send()
        raise

    translator = CypherTranslator(schema=schema, node_cap=MAX_NODES)

    cl.user_session.set("client", client)
    cl.user_session.set("schema", schema)
    cl.user_session.set("translator", translator)

    summary = (
        f"**Schema loaded.** {len(schema.labels)} labels, "
        f"{len(schema.relationship_types)} relationship types, "
        f"{len(schema.property_keys)} property keys.\n\n"
        f"Ask a question in plain English, or use:\n"
        f"- `/schema` to see the data model (text + graph)\n"
        f"- `/cypher <query>` to run raw read-only Cypher\n"
        f"- `/sample` to load a small subgraph\n"
        f"- `/agent <question>` for the eval-and-retry agent that can refine its own query"
    )
    await cl.Message(content=summary).send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    text = message.content.strip()
    client: Neo4jClient = cl.user_session.get("client")
    translator: CypherTranslator = cl.user_session.get("translator")

    if client is None or translator is None:
        await cl.Message(content="Session not initialized. Reload the page to reconnect.").send()
        return

    if text.startswith("/schema"):
        await _handle_schema()
        return

    if text.startswith("/sample"):
        await _run_and_visualize(
            cypher="MATCH (n)-[r]->(m) RETURN n, r, m LIMIT 25",
            explanation="Random subgraph sample.",
            user_question="(sample subgraph)",
        )
        return

    if text.startswith("/cypher"):
        cypher = text[len("/cypher"):].strip()
        if not cypher:
            await cl.Message(content="Provide a query: `/cypher MATCH (n) RETURN n LIMIT 10`").send()
            return
        try:
            translator._reject_if_mutating(cypher)
        except CypherSafetyError as e:
            await cl.Message(content=f"**Rejected.** {e}").send()
            return
        await _run_and_visualize(cypher=cypher, explanation="Raw query (user-supplied).", user_question=text)
        return

    if text.startswith("/agent"):
        question = text[len("/agent"):].strip()
        if not question:
            await cl.Message(content="Usage: `/agent <natural language question>`").send()
            return
        await _run_agent(question)
        return

    async with cl.Step(name="Translate to Cypher", type="llm") as step:
        try:
            translation = translator.translate(text)
        except CypherSafetyError as e:
            step.output = str(e)
            await cl.Message(content=f"**Rejected.** {e}").send()
            return
        except Exception as e:
            logger.exception("Translation failed")
            step.output = f"Translation error: {e}"
            await cl.Message(content=f"Translation failed: `{e}`").send()
            return

        step.output = (
            f"**Cypher:**\n```cypher\n{translation.cypher}\n```\n\n"
            f"_{translation.explanation}_"
        )

    await _run_and_visualize(
        cypher=translation.cypher,
        explanation=translation.explanation,
        user_question=text,
    )


def _write_nvl_seed(records) -> tuple[str, GraphStats]:
    """Serialize records to /public/nvl/data/<uuid>.json and return (data_url, stats)."""
    graph = to_nvl_json(records, max_nodes=MAX_NODES)
    NVL_DATA_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex[:8]}.json"
    path = NVL_DATA_DIR / name
    path.write_text(
        json.dumps({"nodes": graph.nodes, "relationships": graph.relationships})
    )
    return f"/public/nvl/data/{name}", graph.stats


def _viewer_element(data_url: str, height: int = 640) -> cl.CustomElement:
    """Build the GraphViz CustomElement pointing at the NVL viewer with seed data."""
    viewer_url = f"/public/nvl/viewer.html?data={data_url}"
    return cl.CustomElement(name="GraphViz", props={"url": viewer_url, "height": height})


async def _handle_schema() -> None:
    """Show the schema as text and as an interactive data-model graph (NVL)."""
    schema = cl.user_session.get("schema")
    client: Neo4jClient = cl.user_session.get("client")
    if schema is None or client is None:
        await cl.Message(content="Schema not loaded.").send()
        return

    elements: list = []
    visual_summary = ""
    try:
        records = client.run_read("CALL db.schema.visualization()")
        if records:
            data_url, stats = _write_nvl_seed(records)
            elements.append(_viewer_element(data_url))
            visual_summary = (
                f"\n\n**Data model:** {stats.node_count} labels, "
                f"{stats.edge_count} relationship types. "
                f"_Double-click a label to expand its connected types._"
            )
    except Exception as e:
        logger.warning("Schema visualization failed: %s", e)
        visual_summary = f"\n\n_(schema visualization unavailable: {e})_"

    await cl.Message(
        content=f"```\n{schema.to_prompt_block()}\n```{visual_summary}",
        elements=elements,
    ).send()


async def _run_and_visualize(cypher: str, explanation: str, user_question: str) -> None:
    client: Neo4jClient = cl.user_session.get("client")

    async with cl.Step(name="Execute Cypher", type="tool") as step:
        try:
            records = client.run_read(cypher)
        except Exception as e:
            logger.exception("Query execution failed")
            step.output = f"Query failed: {e}"
            await cl.Message(
                content=f"Query failed:\n```\n{e}\n```\n\nCypher was:\n```cypher\n{cypher}\n```"
            ).send()
            return
        step.output = f"Returned {len(records)} record(s)."

    if not records:
        await cl.Message(
            content=f"Query returned no rows.\n\n```cypher\n{cypher}\n```"
        ).send()
        return

    try:
        data_url, stats = _write_nvl_seed(records)
    except Exception as e:
        logger.exception("NVL serialization failed")
        await cl.Message(content=f"Rendering failed: `{e}`").send()
        return

    if stats.node_count == 0:
        await cl.Message(
            content=f"Query returned {len(records)} row(s) but no graph entities to visualize. "
            f"Try a query that returns nodes and relationships, e.g. `RETURN n, r, m`.\n\n"
            f"```cypher\n{cypher}\n```"
        ).send()
        return

    summary = _format_stats(stats, explanation, cypher)
    await cl.Message(content=summary, elements=[_viewer_element(data_url)]).send()


def _format_stats(stats: GraphStats, explanation: str, cypher: str) -> str:
    label_lines = "\n".join(f"  - {label}: {count}" for label, count in stats.label_distribution.items())
    rel_lines = "\n".join(f"  - {rel}: {count}" for rel, count in stats.rel_type_distribution.items())
    truncated_note = (
        f"\n\n_Result truncated at {MAX_NODES} nodes. Tighten the LIMIT for a complete view._"
        if stats.truncated
        else ""
    )
    return (
        f"_{explanation}_\n\n"
        f"**Cypher:**\n```cypher\n{cypher}\n```\n\n"
        f"**Graph:** {stats.node_count} nodes, {stats.edge_count} edges. "
        f"_Double-click any node to expand its neighbors._\n\n"
        f"**By label:**\n{label_lines or '  (none)'}\n\n"
        f"**By relationship type:**\n{rel_lines or '  (none)'}"
        f"{truncated_note}"
    )


async def _run_agent(question: str) -> None:
    client: Neo4jClient = cl.user_session.get("client")
    schema = cl.user_session.get("schema")
    if client is None or schema is None:
        await cl.Message(content="Session not initialized. Reload the page.").send()
        return

    agent = CypherAgent(neo4j_client=client, schema=schema, node_cap=MAX_NODES)

    async with cl.Step(name="Agent loop", type="run") as step:
        try:
            # Sync tool_runner; offload to a worker thread so we don't block
            # Chainlit's event loop while Anthropic + Aura roundtrips happen.
            result = await asyncio.to_thread(agent.answer, question)
        except Exception as e:
            logger.exception("Agent failed")
            step.output = f"Failed: {e}"
            await cl.Message(content=f"Agent failed: `{e}`").send()
            return

        log_lines: list[str] = [f"**{result.iterations} iteration(s)**"]
        for s in result.steps:
            if s.query:
                log_lines.append(f"\n**Iteration {s.index} query:**\n```cypher\n{s.query}\n```")
            if s.tool_summary:
                log_lines.append(f"_Tool result:_\n```\n{s.tool_summary}\n```")
            if s.text:
                log_lines.append(f"_Reasoning:_ {s.text}")
        step.output = "\n".join(log_lines)

    if not result.final_records:
        await cl.Message(
            content=(
                f"_{result.answer}_\n\n"
                f"**Final cypher:**\n```cypher\n{result.final_query or '(none)'}\n```\n\n"
                f"_(no graph entities to visualize)_"
            )
        ).send()
        return

    try:
        data_url, stats = _write_nvl_seed(result.final_records)
    except Exception as e:
        logger.exception("NVL serialization failed")
        await cl.Message(content=f"Rendering failed: `{e}`").send()
        return

    summary = (
        f"_{result.answer}_\n\n"
        f"**Final cypher** (after {result.iterations} iteration(s)):\n```cypher\n{result.final_query}\n```\n\n"
        f"**Graph:** {stats.node_count} nodes, {stats.edge_count} edges. "
        f"_Double-click any node to expand its neighbors._"
    )
    if stats.truncated:
        summary += f"\n\n_Result truncated at {MAX_NODES} nodes._"

    await cl.Message(content=summary, elements=[_viewer_element(data_url)]).send()


@cl.on_chat_end
async def on_chat_end() -> None:
    # The Neo4j driver is now a process-wide singleton (shared with /api/cypher
    # and any other concurrent chat sessions), so we don't close it on chat end.
    # It will be torn down naturally when the process exits.
    pass
