"""The hub: one MCP server (streamable HTTP) that every agent on every machine connects to."""

from __future__ import annotations

import asyncio
import functools
import hmac
import json
import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .store import Conflict, Forbidden, NotFound, Store

DATA_DIR = Path(os.environ.get("MMI_DATA", Path.home() / ".local/share/memyselfandi"))
DEFAULT_DB = Path(os.environ.get("MMI_DB", DATA_DIR / "hub.db"))
MAX_WAIT_S = 240  # keep long-polls under typical MCP client tool timeouts

INSTRUCTIONS = """\
OPT-IN: don't use these tools unless the user asks this session to work with the memyselfandi organizer
(e.g. "check in with the organizer", or the /memyselfandi skill). Otherwise ignore this server.

memyselfandi coordinates all of the user's agents across their machines. Each project has an ORGANIZER
agent that owns the plan and the project's memory (the ledger). You talk to it by queueing tickets, and it
answers in order (first come, first served). That's how you find out what to work on and pick up where
another agent (maybe on another computer) left off.

Worker protocol:
1. `register` once (name, machine, kind, project). Keep the returned id and pass it as agent_id everywhere.
   If you were given an agent id before, reuse it. `list_projects` shows project names.
2. Check in: `ask_organizer(kind="checkin", body="who you are, what you see in the repo, what you're
   ready for")`, then `wait_for_reply(ticket_id)`. The reply is your briefing and assignment.
3. Work the assignment. Use `heartbeat` with a one-line status when your focus changes, and `update_task`
   for progress notes.
4. Questions: `ask_organizer(kind="question", blocking=...)`.
   - If the answer is the ONLY thing that unblocks you, set blocking=true and `wait_for_reply`
     (it returns after a few minutes at most, so call it again while status is queued/with_organizer/delegated).
   - If there's independent work you can do meanwhile, set blocking=false, keep working, and check back
     with `check_ticket` between steps. Stop and wait only when the answer is all that's left.
5. Done, stuck, or ending your session: `ask_organizer(kind="report" or "handoff")` with what you did,
   what's left, and where things are (paths, branches, task ids), so the next agent can continue.
6. Check `read_inbox` occasionally for direct messages.
"""


def build_server(store: Store) -> MCPServer:
    mcp = MCPServer("memyselfandi", instructions=INSTRUCTIONS, version="0.2.0")
    tools: dict[str, Any] = {}

    def tool(fn):
        """Register fn as an MCP tool (and for the CLI). Domain errors reach the agent with their message;
        otherwise mcp 2.x would report only 'Error executing tool X'."""
        tools[fn.__name__] = fn
        expected = (NotFound, Forbidden, Conflict, ValueError)

        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapped(*a, **kw):
                try:
                    return await fn(*a, **kw)
                except expected as e:
                    raise ToolError(str(e)) from e
        else:
            @functools.wraps(fn)
            def wrapped(*a, **kw):
                try:
                    return fn(*a, **kw)
                except expected as e:
                    raise ToolError(str(e)) from e
        return mcp.tool()(wrapped)

    # ======================= everyone =========================================
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
        project: the memyselfandi project name you're working on. status: one line on what you're doing."""
        if kind in ("organizer", "council"):
            raise Forbidden("organizer/council identities are assigned by the hub")
        return store.register(name, machine, kind, project, status, agent_id)

    @tool
    def heartbeat(agent_id: str, status: str | None = None) -> dict[str, Any]:
        """Mark yourself alive and optionally update your one-line status. Reports unread message count."""
        agent = store.heartbeat(agent_id, status)
        agent["unread_messages"] = len(store.inbox(agent_id, mark_read=False))
        return agent

    @tool
    def list_projects() -> list[dict[str, Any]]:
        """Projects that have an organizer, with queue depth."""
        return store.list_projects()

    @tool
    def list_agents(include_offline: bool = False) -> list[dict[str, Any]]:
        """Agents across all machines with their status, online flag, and active task ids."""
        return store.list_agents(include_offline)

    @tool
    def get_board(agent_id: str | None = None) -> dict[str, Any]:
        """Overview: projects + queue depth, online agents, open tasks, and (if agent_id) your unread messages."""
        board: dict[str, Any] = {
            "projects": store.list_projects(),
            "agents": store.list_agents(include_offline=False),
            "tasks": store.list_tasks(),
        }
        if agent_id:
            board["unread_messages"] = store.inbox(agent_id, mark_read=False)
        return board

    @tool
    def get_ledger(project: str) -> dict[str, Any]:
        """The project's living summary (state, decisions, who's on what) plus recent log entries."""
        return store.get_ledger(project)

    # ======================= workers: talking to the organizer ================
    @tool
    def ask_organizer(
        agent_id: str,
        project: str,
        body: str,
        kind: str = "question",
        blocking: bool = False,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Queue a ticket for the project's organizer. kind: checkin | question | report | handoff.
        blocking=true means you can't make ANY progress until it's answered. Returns the ticket with your
        position in line; then use wait_for_reply / check_ticket."""
        if kind == "council_report":
            raise Forbidden("council_report is for council members")
        store.heartbeat(agent_id)
        return store.enqueue(project, agent_id, body, kind, blocking, task_id)

    @tool
    def check_ticket(ticket_id: str) -> dict[str, Any]:
        """Non-blocking: current status, position in line, and the reply if answered."""
        return store.get_ticket(ticket_id)

    @tool
    async def wait_for_reply(ticket_id: str, timeout_s: int = MAX_WAIT_S) -> dict[str, Any]:
        """Wait (up to ~4 minutes) for the organizer's reply. If it comes back still queued, with_organizer,
        or delegated, call again. Use this only when the answer is the only thing unblocking you."""
        deadline = asyncio.get_running_loop().time() + max(1, min(timeout_s, MAX_WAIT_S))
        while True:
            t = store.get_ticket(ticket_id)
            if t["status"] in ("answered", "cancelled") or asyncio.get_running_loop().time() >= deadline:
                return t
            await asyncio.sleep(1)

    @tool
    def cancel_ticket(agent_id: str, ticket_id: str) -> dict[str, Any]:
        """Withdraw a ticket you no longer need answered."""
        return store.cancel_ticket(ticket_id, agent_id)

    # ======================= organizer =======================================
    @tool
    def organizer_next(agent_id: str, project: str) -> dict[str, Any]:
        """ORGANIZER ONLY. Take the next ticket in line. Returns {ticket, ledger, queue_depth, active_council,
        open_tasks}; ticket is null when the queue is empty (then stop and wait to be woken)."""
        ticket = store.next_ticket(project, agent_id)
        queued = [t for t in store.list_tickets(project) if t["status"] == "queued"]
        out: dict[str, Any] = {"ticket": ticket, "queue_depth": len(queued)}
        if ticket:
            asker = store.get_agent(ticket["asker"])
            ticket["asker_info"] = {k: asker[k] for k in ("name", "kind", "machine", "status", "online")}
            out["ledger"] = store.get_ledger(project, log_limit=15)
            out["active_council"] = store.list_council(project)
            out["open_tasks"] = store.list_tasks(project=project)
        return out

    @tool
    def answer_ticket(agent_id: str, ticket_id: str, reply: str) -> dict[str, Any]:
        """ORGANIZER or the COUNCIL MEMBER it was delegated to. Answer a ticket; the asker gets `reply`.
        Make it self-contained (context, paths, task ids, definition of done)."""
        return store.answer_ticket(ticket_id, agent_id, reply)

    @tool
    def update_ledger(
        agent_id: str, project: str, summary: str | None = None, entry: str | None = None, kind: str = "note"
    ) -> dict[str, Any]:
        """ORGANIZER (or council). `summary` REPLACES the living project summary (rewrite it, don't append).
        `entry` appends to the log; kind: note | decision | handoff."""
        agent = store.get_agent(agent_id)
        if agent["kind"] not in ("organizer", "council") or agent["project"] != project:
            raise Forbidden("only this project's organizer/council can edit its ledger")
        if summary is not None and agent["kind"] != "organizer":
            raise Forbidden("only the organizer rewrites the summary; council members add entries")
        return store.update_ledger(project, agent_id, summary, entry, kind)

    @tool
    def summon_council(
        agent_id: str,
        project: str,
        role: str,
        tier: str,
        brief: str,
        justification: str = "",
        ticket_id: str | None = None,
    ) -> dict[str, Any]:
        """ORGANIZER ONLY. Summon a temporary council member. tier: routine (Sonnet) | senior (Opus 5) |
        elder (Fable; only for extreme, ground-breaking changes). senior/elder need a justification.
        Pass ticket_id to delegate a ticket you're holding; the member answers it and reports back to you."""
        return store.summon_council(project, agent_id, role, tier, brief, justification, ticket_id)

    @tool
    def list_council(project: str, include_finished: bool = False) -> list[dict[str, Any]]:
        """Council members on a project (active only by default)."""
        return store.list_council(project, active_only=not include_finished)

    @tool
    def list_queue(project: str) -> list[dict[str, Any]]:
        """Open tickets for a project in queue order (queued, with_organizer, delegated)."""
        return store.list_tickets(project)

    # ======================= council ==========================================
    @tool
    def council_report(agent_id: str, report: str) -> dict[str, Any]:
        """COUNCIL ONLY. Final report to the organizer (decisions, answers given, tasks created, risks).
        Ends your session; you'll be dismissed shortly after."""
        store.get_council(agent_id)
        return store.council_report(agent_id, report)

    # ======================= tasks + messages ==================================
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
        """Create a task. priority 0=urgent..3=low. assignee = agent id to hand it to; target_machine limits
        who should pick it up. agent_id = you (the creator)."""
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
        """One task with its full note history."""
        return store.get_task(task_id)

    @tool
    def claim_task(task_id: str, agent_id: str) -> dict[str, Any]:
        """Atomically claim an open task (only when the organizer told you to pick from open tasks)."""
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
        blocked | done | cancelled."""
        return store.update_task(task_id, agent_id, status, note, assignee, title, description, priority)

    @tool
    def send_message(agent_id: str | None, to: str, body: str) -> dict[str, Any]:
        """Direct message to another agent id, or '*' to broadcast. For anything about what to work on, go
        through ask_organizer instead."""
        return store.send_message(agent_id, to, body)

    @tool
    def read_inbox(agent_id: str, include_read: bool = False) -> list[dict[str, Any]]:
        """Messages sent to you (and broadcasts). Marks them read."""
        return store.inbox(agent_id, unread_only=not include_read)

    # ======================= plain HTTP ========================================
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
            if asyncio.iscoroutine(result):
                result = await result
        except NotFound as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except Forbidden as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except Conflict as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        except (TypeError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return Response(json.dumps(result, default=str), media_type="application/json")

    # Admin-only (CLI) operations that agents shouldn't have as MCP tools.
    @mcp.custom_route("/api/admin/{op}", methods=["POST"])
    async def admin(request: Request) -> Response:
        args = await request.json() if await request.body() else {}
        ops = {
            "add_project": lambda: store.add_project(**args),
            "remove_agent": lambda: store.remove_agent(**args) or {"ok": True},
            "project_rows": lambda: store.project_rows(**args),
            "delete_project": lambda: store.delete_project(**args),
            "create_invite": lambda: store.create_invite(**args),
            "list_invites": lambda: store.list_invites(),
            "revoke_invite": lambda: store.revoke_invite(**args),
        }
        if request.path_params["op"] not in ops:
            return JSONResponse({"error": "no such op"}, status_code=404)
        try:
            return Response(json.dumps(ops[request.path_params["op"]](), default=str), media_type="application/json")
        except (NotFound, TypeError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    return mcp


class TokenAuth:
    """Bearer auth. The hub token (yours) can do everything; a live invite token (`mmi invite`) can use the
    agent tools but not /api/admin. /health stays public. With no hub token configured, auth is off."""

    def __init__(self, app, token: str | None, store: Store):
        self.app, self.token, self.store = app, token.encode() if token else None, store

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.token and scope["path"] != "/health":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, b"Bearer " + self.token):
                bearer = got[7:].decode(errors="replace") if got.startswith(b"Bearer ") else ""
                invitee = self.store.check_invite(bearer) if bearer.startswith("mmi_") else None
                if invitee is None:
                    await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                    return
                if scope["path"].startswith("/api/admin"):
                    await JSONResponse({"error": "invite tokens can't do admin operations"},
                                       status_code=403)(scope, receive, send)
                    return
        await self.app(scope, receive, send)


def build_app(store: Store, host: str, token: str | None):
    mcp = build_server(store)
    local = host in ("127.0.0.1", "localhost", "::1")
    security = None if local else TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app = mcp.streamable_http_app(host=host, transport_security=security)
    return TokenAuth(app, token, store)


def start_retention(store: Store, retention_days: float) -> None:
    """Purge closed tickets older than the retention window: at startup, then daily."""
    import logging
    import threading

    log = logging.getLogger("memyselfandi.retention")

    def loop():
        while True:
            try:
                n = store.purge_closed_tickets(retention_days)
                if n:
                    log.info("purged %d tickets closed more than %s days ago", n, retention_days)
            except Exception:
                log.exception("retention purge failed")
            threading.Event().wait(86400)

    threading.Thread(target=loop, name="mmi-retention", daemon=True).start()


def serve(host: str, port: int, db: Path, token: str | None, supervise: bool = True,
          tiers: dict[str, str] | None = None, retention_days: float | None = None,
          unix_socket: str | None = None) -> None:
    import logging
    import shutil

    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    from .store import DEFAULT_RETENTION_DAYS

    store = Store(db, tiers=tiers)
    start_retention(store, retention_days or DEFAULT_RETENTION_DAYS)
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        print("warning: listening beyond localhost without a token; anyone on that network can use the hub")
    if supervise:
        if shutil.which("tmux") is None:
            print("warning: tmux not found; organizers/council won't be launched (use --no-supervise to silence)")
        else:
            from .supervisor import Supervisor

            local_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
            Supervisor(store, f"http://{local_host}:{port}", token, DATA_DIR / "run").start()
            print("supervisor: organizers and council run in tmux session 'mmi' (`mmi attach`)")
    print(f"memyselfandi hub on http://{host}:{port}/mcp  (db: {db})")
    import socket

    sockets = [socket.create_server((host, port))]
    if unix_socket:
        # Also listen on a Unix socket, so a container can reach the hub through a mounted file instead of
        # the network (see deploy/collab). Only processes that can open the file can connect.
        path = Path(unix_socket)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        uds = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        uds.bind(str(path))
        path.chmod(0o660)
        sockets.append(uds)
        print(f"also listening on unix socket {path}")
    uvicorn.Server(uvicorn.Config(build_app(store, host, token), log_level="warning")).run(sockets=sockets)
