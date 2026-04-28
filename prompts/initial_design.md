# Initial design prompt

A few days before building this, the user asked for a way to point a chat interface at a Neo4j Aura instance and see results as a graph rather than a result table. The original idea was to call the hosted Neo4j MCP agent endpoint, but its OAuth flow expects an interactive user authorization that the launch SDK does not yet expose for direct service-to-service use. We confirmed this by probing `/.well-known/oauth-protected-resource` on `mcp.neo4j.io/agent` and trying client-credentials against both the `aura-mcp.eu.auth0.com` and `aura-api.eu.auth0.com` tenants.

We pivoted to a direct Aura connection plus Anthropic for natural language to Cypher translation. This trades the polish of the hosted agent for full visibility into every query, which fits the production-first posture better and avoids depending on an undocumented OAuth path.

## Build prompt

> Build a Chainlit chat interface that lets a user ask questions in plain English about a Neo4j Aura instance. Translate the question to Cypher using Claude (Opus 4.7, adaptive thinking, prompt cache the schema). Execute the Cypher in read-only mode. Render the result as an interactive PyVis HTML graph and present it inline in the chat. Cap result size at 200 nodes by default. Reject any Cypher that mutates state. Treat the Anthropic key and Aura credentials as secrets that live only in `.env`. Match production-grade quality: structured exception handling, clear logging, README with architecture and trade-offs, and a small prompts folder for reproducibility.

## Iteration notes

- First implementation pass uses a structured JSON output schema (`output_config.format`) so the model returns `{cypher, explanation, node_cap_suggestion}`. This avoids prefill (which 400s on Opus 4.7) and gives a clean parse.
- Prompt cache marker placed on the last system block, after the schema dump. The schema and the safety instructions both live in the cached prefix; only the user question varies per request, so cache hits should be near 100 percent within a session.
- PyVis HTML is written to a per-message file in `output/` and attached as a Chainlit File element with `display="inline"`. Chainlit then surfaces it as a downloadable artifact and many browsers will preview it inline.
- Cypher safety check is a regex over the generated query. It rejects any token from the mutating set (CREATE, MERGE, DELETE, SET, REMOVE, DROP, FOREACH-with-mutation). This is a defense-in-depth layer on top of the read-only session, not a replacement for it.
