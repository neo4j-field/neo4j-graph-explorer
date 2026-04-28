"""Chainlit chat handlers for the Neo4j Graph Explorer."""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

import chainlit as cl
from dotenv import load_dotenv

from cypher_translator import CypherSafetyError, CypherTranslator
from graph_renderer import GraphStats, render_records_to_html
from neo4j_client import Neo4jClient

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("app")

OUTPUT_DIR = Path("public/graphs")
MAX_NODES = int(os.environ.get("MAX_NODES", "200"))


@cl.on_chat_start
async def on_chat_start() -> None:
    """Connect to Neo4j, introspect the schema, prime the translator, greet the user."""
    try:
        client = Neo4jClient.from_env()
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
        f"- `/schema` to see labels and relationship types\n"
        f"- `/cypher <query>` to run raw read-only Cypher\n"
        f"- `/sample` to load a small subgraph"
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


async def _handle_schema() -> None:
    """Show the schema as text AND as a meta-graph visualization.

    Neo4j ships db.schema.visualization() out of the box: it returns
    synthetic nodes (one per label) and synthetic relationships (one per
    rel type), which we can pipe through the same PyVis renderer.
    """
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
            output_path = OUTPUT_DIR / f"schema_{uuid.uuid4().hex[:8]}.html"
            stats = render_records_to_html(records=records, output_path=output_path, max_nodes=MAX_NODES)
            iframe_url = f"/public/graphs/{output_path.name}"
            elements.append(cl.CustomElement(name="GraphViz", props={"url": iframe_url, "height": 640}))
            visual_summary = (
                f"\n\n**Data model:** {stats.node_count} labels, "
                f"{stats.edge_count} relationship types."
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

    output_path = OUTPUT_DIR / f"graph_{uuid.uuid4().hex[:8]}.html"
    try:
        stats = render_records_to_html(records=records, output_path=output_path, max_nodes=MAX_NODES)
    except Exception as e:
        logger.exception("Render failed")
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
    # Chainlit serves files under public/ at the root of its origin.
    # public/graphs/abc.html  -->  /public/graphs/abc.html
    iframe_url = f"/public/graphs/{output_path.name}"
    await cl.Message(
        content=summary,
        elements=[
            cl.CustomElement(
                name="GraphViz",
                props={"url": iframe_url, "height": 640},
            )
        ],
    ).send()


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
        f"**Graph:** {stats.node_count} nodes, {stats.edge_count} edges.\n\n"
        f"**By label:**\n{label_lines or '  (none)'}\n\n"
        f"**By relationship type:**\n{rel_lines or '  (none)'}"
        f"{truncated_note}"
    )


@cl.on_chat_end
async def on_chat_end() -> None:
    client: Neo4jClient | None = cl.user_session.get("client")
    if client is not None:
        client.close()
