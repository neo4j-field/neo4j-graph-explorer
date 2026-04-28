# Neo4j Graph Explorer

Ask questions in plain English. The app translates them to Cypher with Claude, runs them against your Neo4j Aura instance in read-only mode, and renders the result as an interactive PyVis graph.

## Try

- `Show me 25 nodes and how they connect`
- `What labels exist in this database?`
- `Find the 10 most connected nodes`

## Slash commands

- `/schema` show node labels and relationship types, plus the data-model graph
- `/cypher MATCH (n) RETURN n LIMIT 10` run raw Cypher (read-only)
- `/sample` auto-load a small subgraph
- `/agent <question>` use the eval-and-retry agent that runs its own Cypher, inspects results, and refines if needed

Queries are read-only. Results are capped at the configured node limit to keep visualizations responsive.
