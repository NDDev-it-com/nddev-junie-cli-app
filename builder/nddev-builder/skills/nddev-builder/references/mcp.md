# MCP Workflow

Junie MCP configuration is JSON with a top-level `mcpServers` object.

Creator workflow:

1. Keep public managed MCP config empty unless a server is fully specified without
   secrets.
2. Use explicit command/args or URL fields only when the server is intentionally
   bundled.
3. Do not embed API keys, tokens, or personal headers.

Checker workflow:

1. Verify top-level JSON shape is `{"mcpServers": {...}}`.
2. Verify local commands are deterministic and bounded.
3. Verify remote server headers do not contain credentials.
