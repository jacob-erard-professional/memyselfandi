# memyselfandi: notes for agents working on this repo

- Python 3.12+, uv. Run `uv run pytest` before committing; tests include an end-to-end hub over real MCP.
- MCP SDK is **mcp 2.x**: `from mcp.server.mcpserver import MCPServer` (FastMCP was renamed). Client: `from mcp import Client`.
- All state rules live in `store.py`. `hub.py` should stay a thin layer: every MCP tool is registered with
  `@tool`, which also exposes it at `POST /api/tool/<name>` for the CLI. Add new capabilities as a store
  method plus a `@tool` wrapper, and they show up in both places.
- The hub is meant to run on the user's always-on desktop and be reached over Tailscale with a bearer token.
  Don't add features that need a public endpoint.
