"""End-to-end smoke test. Run before launching Chainlit.

Verifies:
1. Aura connection works
2. Schema introspection returns something
3. Anthropic translator returns valid, read-only Cypher
4. The query executes
5. PyVis renders an HTML file
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Make sibling modules importable when running from the scripts/ folder.
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

from cypher_translator import CypherTranslator
from graph_renderer import render_records_to_html
from neo4j_client import Neo4jClient


def main() -> int:
    load_dotenv(Path(__file__).parent.parent / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("smoke")

    log.info("Step 1: connect to Aura")
    client = Neo4jClient.from_env()
    client.verify_connectivity()
    log.info("  connectivity ok")

    log.info("Step 2: fetch schema")
    schema = client.fetch_schema()
    log.info("  labels=%s rel_types=%s prop_keys=%s",
             len(schema.labels), len(schema.relationship_types), len(schema.property_keys))

    log.info("Step 3: translate a question")
    translator = CypherTranslator(schema=schema, node_cap=25)
    question = "Show me 10 nodes and how they connect"
    if not schema.labels:
        question = "Return any 5 nodes, even if disconnected"
    translation = translator.translate(question)
    log.info("  cypher: %s", translation.cypher)
    log.info("  explanation: %s", translation.explanation)

    log.info("Step 4: execute the query")
    records = client.run_read(translation.cypher)
    log.info("  returned %d record(s)", len(records))

    log.info("Step 5: render HTML")
    out = Path(__file__).parent.parent / "output" / "smoke.html"
    stats = render_records_to_html(records, out, max_nodes=200)
    log.info("  graph: %d nodes, %d edges", stats.node_count, stats.edge_count)
    log.info("  wrote %s", out)

    client.close()
    log.info("Smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
