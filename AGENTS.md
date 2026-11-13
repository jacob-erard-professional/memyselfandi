# memyselfandi: notes for agents working on this repo

- Python 3.12+, uv. Run `uv run pytest` before committing.
- MCP SDK is **mcp 2.x**: `from mcp.server.mcpserver import MCPServer` (FastMCP was renamed). Client: `from mcp import Client`.
  Unhandled tool exceptions reach agents only as "Error executing tool X". The `@tool` wrapper in `hub.py`
  converts our domain errors (NotFound/Forbidden/Conflict/ValueError) to `ToolError` so the message gets through.
- All state rules live in `store.py`. `hub.py` stays thin: every MCP tool is registered with `@tool`, which
  also exposes it at `POST /api/tool/<name>` for the CLI. CLI-only admin ops go at `/api/admin/<op>`.
- Roles: workers (any kind), `organizer` and `council` identities are created by the hub/supervisor only.
  Organizer ids are `org-<project>-<n>`, council ids `cn-<hex>`. Permission checks live in the store.
- Organizer/council sessions are launched by `supervisor.py` as `env MMI_ROLE=... claude <prompt> --model ...
  --permission-mode dontAsk --strict-mcp-config --mcp-config <run/mcp.json> --allowedTools mcp__memyselfandi,Read(//<project>/**)`.
  Keep the prompt positional and first, because `--allowedTools`/`--mcp-config` are variadic. NEVER grant a bare
  `Read`: verified that it reads any file on the machine, and tickets can come from other people (invites).
- Profiles (`--profile collab`) are fully separate hubs (config, data dir, tmux session, MCP server/skill name).
- Auth: the hub token can do everything; invite tokens (`mmi_...`, stored hashed, expiring) can't hit `/api/admin`.
- Never auto-answer Claude Code's folder-trust prompt. The supervisor flags it and notifies the user.
- The hub runs on the user's always-on desktop, reached over Tailscale with a bearer token. No public endpoints.
- Schema changes: append a migration to `MIGRATIONS` in `store.py` (version, description, SQL statements). Never edit
  `SCHEMA` (that's v1) or a released migration. Add a test that opens an older database.
- Tickets are closed (`closed_at`), never deleted, except by the retention purge or `delete_project`.
- Mind disk writes: never write to SQLite on a polling or tick path unless state changed (throttle heartbeats).
