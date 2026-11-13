# memyselfandi: notes for agents working on this repo

- Python 3.12+, uv. Run `uv run pytest` before committing.
- MCP SDK is **mcp 2.x**: `from mcp.server.mcpserver import MCPServer` (FastMCP was renamed). Client: `from mcp import Client`.
  Unhandled tool exceptions reach agents only as "Error executing tool X". The `@tool` wrapper in `hub.py`
  converts our domain errors (NotFound/Forbidden/Conflict/ValueError) to `ToolError` so the message gets through.
- All state rules live in `store.py`. `hub.py` stays thin: every MCP tool is registered with `@tool`, which
  also exposes it at `POST /api/tool/<name>` for the CLI. CLI-only admin ops go at `/api/admin/<op>`.
- Roles: workers (any kind), `organizer` and `council` identities are created by the hub/supervisor only.
  Organizer ids are `org-<project>-<n>`, council ids `cn-<hex>`. Permission checks live in the store.
- Organizer/council sessions are launched by `supervisor.py` as `claude <prompt> --model ... --permission-mode dontAsk
  --strict-mcp-config --mcp-config <run/mcp.json> --allowedTools mcp__memyselfandi,Read,Grep,Glob`. Keep the prompt
  positional and first, because `--allowedTools`/`--mcp-config` are variadic.
- Never auto-answer Claude Code's folder-trust prompt. The supervisor flags it and notifies the user.
- The hub runs on the user's always-on desktop, reached over Tailscale with a bearer token. No public endpoints.
