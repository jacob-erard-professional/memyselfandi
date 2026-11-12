"""`mmi`: run the hub, and see/steer your agents from any machine's terminal."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(os.environ.get("MMI_CONFIG", Path.home() / ".config/memyselfandi/config.json"))
DEFAULT_PORT = 7377


# -- config ---------------------------------------------------------------
def load_config() -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text())
    cfg["url"] = os.environ.get("MMI_URL", cfg.get("url", f"http://127.0.0.1:{DEFAULT_PORT}"))
    cfg["token"] = os.environ.get("MMI_TOKEN", cfg.get("token"))
    cfg["machine"] = os.environ.get("MMI_MACHINE", cfg.get("machine", socket.gethostname()))
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
    CONFIG_PATH.chmod(0o600)


# -- http -----------------------------------------------------------------
class HubError(RuntimeError):
    pass


def call(cfg: dict[str, Any], tool: str, **args: Any) -> Any:
    args = {k: v for k, v in args.items() if v is not None}
    req = urllib.request.Request(
        cfg["url"].rstrip("/") + f"/api/tool/{tool}",
        data=json.dumps(args).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if cfg.get("token"):
        req.add_header("Authorization", f"Bearer {cfg['token']}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", e.reason)
        except Exception:
            msg = e.reason
        raise HubError(f"{e.code}: {msg}") from None
    except urllib.error.URLError as e:
        raise HubError(f"can't reach hub at {cfg['url']} ({e.reason}). Is it running? Check `mmi config`.") from None


def me(cfg: dict[str, Any]) -> str:
    """The human at this terminal is an agent too, so messages/tasks have an author."""
    agent_id = f"human-{cfg['machine']}"
    call(cfg, "register", name=f"you@{cfg['machine']}", machine=cfg["machine"], kind="human", agent_id=agent_id)
    return agent_id


# -- formatting -----------------------------------------------------------
def ago(seconds: float) -> str:
    s = int(seconds)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit}"
    return f"{s}s"


def fmt_agents(agents: list[dict]) -> str:
    if not agents:
        return "  (none)"
    lines = []
    for a in agents:
        dot = "●" if a["online"] else "○"
        tasks = f" [{', '.join(a.get('active_tasks') or [])}]" if a.get("active_tasks") else ""
        proj = f" in {a['project']}" if a.get("project") else ""
        lines.append(
            f"  {dot} {a['id']:<16} {a['name']} ({a['kind']}@{a['machine']}{proj}) "
            f"seen {ago(a['last_seen_ago_s'])} ago{tasks}"
        )
        if a.get("status"):
            lines.append(f"      ↳ {a['status']}")
    return "\n".join(lines)


def fmt_tasks(tasks: list[dict]) -> str:
    if not tasks:
        return "  (none)"
    lines = []
    for t in tasks:
        who = f" → {t['assignee']}" if t.get("assignee") else ""
        where = f" @{t['target_machine']}" if t.get("target_machine") else ""
        proj = f" ({t['project']})" if t.get("project") else ""
        lines.append(f"  P{t['priority']} {t['id']:<11} [{t['status']}] {t['title']}{proj}{where}{who}")
    return "\n".join(lines)


def fmt_messages(msgs: list[dict]) -> str:
    if not msgs:
        return "  (no messages)"
    return "\n".join(
        f"  #{m['id']} {time.strftime('%m-%d %H:%M', time.localtime(m['created_at']))} "
        f"{m['sender'] or '?'} → {m['recipient']}: {m['body']}"
        for m in msgs
    )


# -- commands -------------------------------------------------------------
def cmd_serve(args, cfg):
    from .hub import DEFAULT_DB, serve

    token = os.environ.get("MMI_TOKEN") or cfg.get("hub_token")
    serve(args.host, args.port, Path(args.db or DEFAULT_DB), token)


def cmd_config(args, cfg):
    stored = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    if args.new_hub_token:
        stored["hub_token"] = stored["token"] = secrets.token_urlsafe(24)
    for key in ("url", "token", "machine"):
        if getattr(args, key) is not None:
            stored[key] = getattr(args, key)
    if stored != (json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}):
        save_config(stored)
    cfg = load_config()
    print(f"config: {CONFIG_PATH}")
    print(f"  url:     {cfg['url']}")
    print(f"  machine: {cfg['machine']}")
    print(f"  token:   {'set' if cfg.get('token') else 'not set'}")
    if args.show_token and cfg.get("token"):
        print(f"           {cfg['token']}")


def cmd_status(args, cfg):
    agent_id = me(cfg)
    board = call(cfg, "get_board", agent_id=agent_id)
    print("AGENTS (online)")
    print(fmt_agents([a for a in board["agents"] if a["kind"] != "human"]))
    print("\nTASKS")
    print(fmt_tasks(board["tasks"]))
    if board.get("unread_messages"):
        print(f"\nINBOX ({len(board['unread_messages'])} unread; `mmi inbox` to read)")


def cmd_agents(args, cfg):
    print(fmt_agents(call(cfg, "list_agents", include_offline=args.all)))


def cmd_tasks(args, cfg):
    print(fmt_tasks(call(cfg, "list_tasks", include_closed=args.all, project=args.project, status=args.status)))


def cmd_show(args, cfg):
    t = call(cfg, "get_task", task_id=args.task)
    print(fmt_tasks([t]))
    if t["description"]:
        print("\n" + t["description"])
    for n in t["notes"]:
        print(f"\n  - {n['author'] or '?'} {time.strftime('%m-%d %H:%M', time.localtime(n['created_at']))}: {n['body']}")


def cmd_add(args, cfg):
    t = call(
        cfg,
        "create_task",
        title=args.title,
        description=args.description or "",
        agent_id=me(cfg),
        project=args.project,
        priority=args.priority,
        assignee=args.to,
        target_machine=args.machine,
    )
    print(f"created {t['id']}: {t['title']}")


def cmd_update(args, cfg, status=None):
    t = call(cfg, "update_task", task_id=args.task, agent_id=me(cfg), status=status or args.status, note=args.note)
    print(f"{t['id']} [{t['status']}] {t['title']}")


def cmd_send(args, cfg):
    m = call(cfg, "send_message", agent_id=me(cfg), to=args.to, body=" ".join(args.body))
    print(f"sent #{m['id']} → {m['recipient']}")


def cmd_inbox(args, cfg):
    print(fmt_messages(call(cfg, "read_inbox", agent_id=me(cfg), include_read=args.all)))


def cmd_mcp_setup(args, cfg):
    """Print (or run) the commands that point this machine's agents at the hub."""
    url = cfg["url"].rstrip("/") + "/mcp"
    token = cfg.get("token")
    claude = ["claude", "mcp", "add", "--scope", "user", "--transport", "http", "memyselfandi", url]
    if token:
        claude += ["--header", f"Authorization: Bearer {token}"]
    print("# Claude Code (all projects on this machine):")
    print(shlex.join(claude))
    print("\n# Codex: add to ~/.codex/config.toml")
    print("[mcp_servers.memyselfandi]")
    print(f'url = "{url}"')
    if token:
        print('bearer_token_env_var = "MMI_TOKEN"   # and export MMI_TOKEN in your shell profile')
    if args.apply:
        import subprocess

        subprocess.run(["claude", "mcp", "remove", "--scope", "user", "memyselfandi"], capture_output=True)
        subprocess.run(claude, check=True)
        print("\napplied Claude Code config.")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="mmi", description="memyselfandi: coordinate your agents across machines")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the hub (on the always-on machine)")
    s.add_argument("--host", default=os.environ.get("MMI_HOST", "127.0.0.1"), help="bind address (use your tailscale IP or 0.0.0.0)")
    s.add_argument("--port", type=int, default=int(os.environ.get("MMI_PORT", DEFAULT_PORT)))
    s.add_argument("--db", default=None)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("config", help="show/set hub url, token, machine name")
    s.add_argument("--url")
    s.add_argument("--token")
    s.add_argument("--machine")
    s.add_argument("--new-hub-token", action="store_true", help="generate a token for the hub you serve here")
    s.add_argument("--show-token", action="store_true")
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("mcp-setup", help="print/apply config that connects Claude Code + Codex to the hub")
    s.add_argument("--apply", action="store_true", help="run the `claude mcp add` command")
    s.set_defaults(fn=cmd_mcp_setup)

    sub.add_parser("status", help="board: online agents + open tasks").set_defaults(fn=cmd_status)

    s = sub.add_parser("agents", help="list agents")
    s.add_argument("-a", "--all", action="store_true", help="include offline")
    s.set_defaults(fn=cmd_agents)

    s = sub.add_parser("tasks", help="list tasks")
    s.add_argument("-a", "--all", action="store_true", help="include done/cancelled")
    s.add_argument("-p", "--project")
    s.add_argument("-s", "--status")
    s.set_defaults(fn=cmd_tasks)

    s = sub.add_parser("show", help="show a task with notes")
    s.add_argument("task")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("add", help="create a task")
    s.add_argument("title")
    s.add_argument("-d", "--description")
    s.add_argument("-p", "--project")
    s.add_argument("--priority", type=int, default=2, choices=[0, 1, 2, 3])
    s.add_argument("--machine", help="only agents on this machine should pick it up")
    s.add_argument("--to", help="assign directly to an agent id")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("update", help="change task status and/or add a note")
    s.add_argument("task")
    s.add_argument("-s", "--status")
    s.add_argument("-n", "--note")
    s.set_defaults(fn=cmd_update)

    s = sub.add_parser("done", help="mark a task done")
    s.add_argument("task")
    s.add_argument("-n", "--note")
    s.set_defaults(fn=lambda a, c: cmd_update(a, c, status="done"))

    s = sub.add_parser("send", help="message an agent id, or '*' for everyone")
    s.add_argument("to")
    s.add_argument("body", nargs="+")
    s.set_defaults(fn=cmd_send)

    s = sub.add_parser("inbox", help="read your messages")
    s.add_argument("-a", "--all", action="store_true")
    s.set_defaults(fn=cmd_inbox)

    args = p.parse_args(argv)
    try:
        args.fn(args, load_config())
    except HubError as e:
        sys.exit(f"mmi: {e}")


if __name__ == "__main__":
    main()
