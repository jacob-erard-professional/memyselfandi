"""The hub: one MCP server (streamable HTTP) that every agent on every machine connects to."""

from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .store import Conflict, NotFound, Store

DEFAULT_DB = Path(os.environ.get("MMI_DB", Path.home() / ".local/share/memyselfandi/hub.db"))

INSTRUCTIONS = """\
memyselfandi is the shared coordination hub for all of the user's agents across their machines
(desktop PC, laptop, ...). Other agents may be working in parallel on other computers.

Protocol:
1. At the start of a session call `register` once (name, machine, kind, project). Keep the returned
   agent id and pass it as `agent_id` to every other call. Reuse an id you were given before if you have one.
2. Call `get_board` to see who else is active, what they're doing, and the open tasks.
3. Before starting work that someone else might also pick up, `claim_task` it. Claims are atomic;
   if it fails, someone else has it, so pick something else.
4. Call `heartbeat` with a one-line status whenever your focus changes (at least every ~10 minutes
   while active), and `update_task` with notes/status as you make progress. Mark tasks done.
5. Check `read_inbox` between steps; other agents and the user send requests and handoffs there.
6. To hand work to another machine/agent, `create_task` (optionally target_machine or assignee)
   and/or `send_message`.
"""


def build_server(store: Store) -> MCPServer:
    mcp = MCPServer("memyselfandi", instructions=INSTRUCTIONS, version="0.1.0")
    tools: dict[str, Any] = {}

    def tool(fn):
        tools[fn.__name__] = fn
        return mcp.tool()(fn)

    # -- agents --------------------------------------------------------------
    @tool
    def register(
        name: str,
        machine: str,
        kind: str = "claude-code",
        project: str | None = None,
        status: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Register this agent with the hub (or re-register an existing agent_id). Returns your agent record;
        keep `id` and pass it as agent_id to other tools. kind: claude-code | codex | human | other.
        project: repo or directory you're working in. status: one line on what you're doing."""
        return store.register(name, machine, kind, project, status, agent_id)

    @tool
    def heartbeat(agent_id: str, status: str | None = None) -> dict[str, Any]:
        """Mark yourself alive and optionally update your one-line status. Also reports unread message count."""
        agent = store.heartbeat(agent_id, status)
        agent["unread_messages"] = len(store.inbox(agent_id, mark_read=False))
        return agent

    @tool
    def list_agents(include_offline: bool = False) -> list[dict[str, Any]]:
        """List agents across all machines with their status, online flag, and active task ids."""
        return store.list_agents(include_offline)

    @tool
    def get_board(agent_id: str | None = None) -> dict[str, Any]:
        """One-call overview: online agents, open/active tasks, and (if agent_id given) your unread messages
        (without marking them read)."""
        board: dict[str, Any] = {
            "agents": store.list_agents(include_offline=False),
            "tasks": store.list_tasks(),
        }
        if agent_id:
            board["unread_messages"] = store.inbox(agent_id, mark_read=False)
        return board

    # -- tasks ---------------------------------------------------------------
    @tool
    def create_task(
        title: str,
        description: str = "",
        agent_id: str | None = None,
        project: str | None = None,
        priority: int = 2,
        assignee: str | None = None,
        target_machine: str | None = None,
    ) -> dict[str, Any]:
        """Create a shared task. priority 0=urgent..3=low. Set assignee (agent id) to hand it to a specific
        agent, or target_machine so only agents on that machine pick it up. agent_id = you (the creator)."""
        return store.create_task(title, description, project, priority, agent_id, assignee, target_machine)

    @tool
    def list_tasks(
        status: str | None = None,
        project: str | None = None,
        assignee: str | None = None,
        include_closed: bool = False,
    ) -> list[dict[str, Any]]:
        """List tasks. Defaults to everything not done/cancelled, sorted by priority."""
        return store.list_tasks(status, project, assignee, include_closed)

    @tool
    def get_task(task_id: str) -> dict[str, Any]:
        """Get one task including its full note history."""
        return store.get_task(task_id)

    @tool
    def claim_task(task_id: str, agent_id: str) -> dict[str, Any]:
        """Atomically claim an open task for yourself. Errors if another agent already has it."""
        return store.claim_task(task_id, agent_id)

    @tool
    def update_task(
        task_id: str,
        agent_id: str | None = None,
        status: str | None = None,
        note: str | None = None,
        assignee: str | None = None,
        title: str | None = None,
        description: str | None = None,
        priority: int | None = None,
    ) -> dict[str, Any]:
        """Update a task and/or append a progress note. status: open (releases it) | claimed | in_progress |
        blocked | done | cancelled. Leave a note when finishing or blocking so other agents have context."""
        return store.update_task(task_id, agent_id, status, note, assignee, title, description, priority)

    # -- messages ------------------------------------------------------------
    @tool
    def send_message(agent_id: str | None, to: str, body: str) -> dict[str, Any]:
        """Send a message to another agent (their agent id) or '*' to broadcast to everyone."""
        return store.send_message(agent_id, to, body)

    @tool
    def read_inbox(agent_id: str, include_read: bool = False) -> list[dict[str, Any]]:
        """Read messages sent to you (and broadcasts). Marks them read."""
        return store.inbox(agent_id, unread_only=not include_read)

    # -- plain HTTP ----------------------------------------------------------
    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> Response:
        return JSONResponse({"ok": True})

    @mcp.custom_route("/api/snapshot", methods=["GET"])
    async def snapshot(_: Request) -> Response:
        return Response(store.dump(), media_type="application/json")

    @mcp.custom_route("/api/tool/{name}", methods=["POST"])
    async def call(request: Request) -> Response:
        """Same tools as MCP, as plain JSON, for the `mmi` CLI and scripts."""
        fn = tools.get(request.path_params["name"])
        if fn is None:
            return JSONResponse({"error": "no such tool"}, status_code=404)
        try:
            args = await request.json() if await request.body() else {}
            result = fn(**args)
        except NotFound as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except Conflict as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        except (TypeError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return Response(json.dumps(result, default=str), media_type="application/json")

    return mcp


class TokenAuth:
    """Optional shared-secret bearer auth (MMI_TOKEN). /health stays public."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] != "/health":
            headers = dict(scope.get("headers") or [])
            got = headers.get(b"authorization", b"")
            if not hmac.compare_digest(got, b"Bearer " + self.token):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_app(store: Store, host: str, token: str | None):
    mcp = build_server(store)
    local = host in ("127.0.0.1", "localhost", "::1")
    security = None if local else TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app = mcp.streamable_http_app(host=host, transport_security=security)
    return TokenAuth(app, token) if token else app


def serve(host: str, port: int, db: Path, token: str | None) -> None:
    import uvicorn

    store = Store(db)
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        print("warning: listening beyond localhost without MMI_TOKEN; anyone on that network can use the hub")
    print(f"memyselfandi hub on http://{host}:{port}/mcp  (db: {db})")
    uvicorn.run(build_app(store, host, token), host=host, port=port, log_level="warning")
