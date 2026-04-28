# Neo4j Graph Explorer

A Chainlit chat interface that translates natural language to Cypher with Claude, runs queries read-only against a Neo4j Aura instance, and renders the result as an interactive PyVis graph.

## Architecture

```
User question
    |
    v
Chainlit chat handler  (app.py)
    |
    +--> NL to Cypher translator  (cypher_translator.py)
    |       Anthropic SDK, claude-opus-4-7, adaptive thinking,
    |       prompt-cached schema, structured JSON output
    |
    +--> Neo4j read-only executor  (neo4j_client.py)
    |       Aura driver, READ access mode, query timeout, node cap
    |
    +--> PyVis graph renderer  (graph_renderer.py)
            Extracts nodes and relationships from result records,
            colors by label, writes interactive HTML
```

## Trade-offs

- Read-only by design. Cypher that mutates state is rejected before execution. This protects the database from accidental writes through the chat UI.
- Node cap of 200 by default. PyVis becomes sluggish past a few hundred nodes. Override with `MAX_NODES` in `.env`.
- The schema is fetched once at session start and pinned into the system prompt. Any changes to the database schema during the session are not picked up until restart.
- The Anthropic system prompt is cached for the duration of a session, so subsequent translations are cheap and fast.

## Run locally

1. Copy `.env.example` to `.env` and fill in Aura credentials and your Anthropic key.
2. Create a virtualenv and install dependencies:
   ```
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Start the app:
   ```
   chainlit run app.py -w
   ```
4. Open the URL Chainlit prints (default `http://localhost:8000`).

## Deployment notes

- The `.env` file is gitignored and must never be committed. Use a secret manager in production (AWS Secrets Manager, GCP Secret Manager, Azure Key Vault).
- The Anthropic API key is workspace-scoped. Rotate via the Anthropic Console if exposed.
- Neo4j Aura credentials should rotate per the customer policy. The application uses the standard `neo4j+s://` TLS connection.

## Project layout

```
visualization/
  app.py                  Chainlit handlers
  cypher_translator.py    Anthropic NL to Cypher
  graph_renderer.py       Neo4j result to PyVis HTML
  neo4j_client.py         Driver wrapper, schema, read-only execution
  chainlit.md             Welcome page
  requirements.txt
  .env.example
  prompts/
    initial_design.md     Original design prompt
```

## Prompt provenance

Per the principal architect guidance, the prompts that shaped this build are checked in under `prompts/`. They are reproducible and reviewable.
