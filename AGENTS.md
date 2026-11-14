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
  --permission-mode dontAsk --strict-mcp-config --mcp-config <run/mcp.json> --allowedTools mcp__memyselfandi,Read(//<hub folder>/**),...`.
  Keep the prompt positional and first, because `--allowedTools`/`--mcp-config` are variadic. NEVER grant a bare
  `Read`: verified that it reads any file on the machine, and tickets can come from other people (invites).
- Profiles (`--profile collab`) are fully separate hubs (config, data dir, tmux session, MCP server/skill name).
- Auth: the hub token can do everything; invite tokens (`mmi_...`, stored hashed, expiring) can't hit `/api/admin`
  or `/api/snapshot`. `TokenAuth` stamps each request's ASGI scope with the caller (`mmi.auth`); the `@tool` wrapper
  injects a `Context`, reads it (fail-closed), and runs `guard()`: an invitee may only pass an `agent_id` it registered
  (`agents.owner`), so never an organizer/council id; plus per-invite limits (`DEFAULT_INVITE_LIMITS`, hub config
  `invite_limits`). Any new tool that acts for someone must take `agent_id` so the guard covers it.
  Invitees get only `INVITEE_TOOLS`: the organizer, their own tickets/assigned tasks, and messages to agents on
  their own project. Every inbox message carries hub-assigned `from`/`trust` ("advice" = another person); that
  label is the defense, the guard is the hard line (as in Quant Code's sender tags).
- Never auto-answer Claude Code's folder-trust prompt. The supervisor flags it and notifies the user.
- The hub runs on the user's always-on desktop, reached over Tailscale with a bearer token. No public endpoints.
- Schema changes: append a migration to `MIGRATIONS` in `store.py` (version, description, SQL statements). Never edit
  `SCHEMA` (that's v1) or a released migration. Add a test that opens an older database.
- Tickets are closed (`closed_at`), never deleted, except by the retention purge or `delete_project`.
- Mind disk writes: never write to SQLite on a polling or tick path unless state changed (throttle heartbeats).
- deploy/collab: the sidecar must keep its own network namespace (bridge). Tailscale userspace mode forwards
  unserved ports to the namespace's localhost; with `network_mode: host` that exposed the PC's localhost services.
  The hub is reached via the unix socket (`config --socket`) + socat relay.
- A project is a name plus folders on any machines (`project_folders`: project, machine, path). It is not tied to
  git. A session belongs to the project with a folder on its machine containing its cwd (`resolve_project`).
  Organizer/council get `Read` on the project's *hub* folders only, and are restarted (when idle) if those change.
  `projects.path` is derived (first hub folder); `projects.remote` (v5) is unused.
