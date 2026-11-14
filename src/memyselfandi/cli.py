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
PROFILE = os.environ.get("MMI_PROFILE")


def use_profile(name: str) -> None:
    """A profile is a fully separate hub on the same machine (own config, database, tmux session),
    e.g. `--profile collab` for the hub you share with other people."""
    global CONFIG_PATH, PROFILE
    PROFILE = name
    os.environ["MMI_PROFILE"] = name
    os.environ.setdefault("MMI_CONFIG", str(Path.home() / f".config/memyselfandi/{name}.json"))
    os.environ.setdefault("MMI_DATA", str(Path.home() / f".local/share/memyselfandi/{name}"))
    os.environ.setdefault("MMI_TMUX_SESSION", f"mmi-{name}")
    CONFIG_PATH = Path(os.environ["MMI_CONFIG"])
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


def call(cfg: dict[str, Any], tool: str, _path: str | None = None, **args: Any) -> Any:
    args = {k: v for k, v in args.items() if v is not None}
    req = urllib.request.Request(
        cfg["url"].rstrip("/") + (_path or f"/api/tool/{tool}"),
        data=json.dumps(args).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if cfg.get("token"):
        req.add_header("Authorization", f"Bearer {cfg['token']}")
    try:
        with urllib.request.urlopen(req, timeout=args.get("timeout_s", 0) + 15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", e.reason)
        except Exception:
            msg = e.reason
        raise HubError(f"{e.code}: {msg}") from None
    except urllib.error.URLError as e:
        raise HubError(f"can't reach hub at {cfg['url']} ({e.reason}). Is it running? Check `mmi config`.") from None


def admin(cfg: dict[str, Any], op: str, **args: Any) -> Any:
    return call(cfg, op, _path=f"/api/admin/{op}", **args)


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
    serve(args.host, args.port or cfg.get("port") or DEFAULT_PORT, Path(args.db or DEFAULT_DB), token, supervise=not args.no_supervise,
          tiers=cfg.get("tiers"), retention_days=cfg.get("retention_days"), unix_socket=cfg.get("socket"),
          machine=cfg["machine"], invite_limits=cfg.get("invite_limits"))


def cmd_up(args, cfg):
    """Start the hub in a tmux session (`mmi`), where organizers and council will also live."""
    import shutil
    import subprocess

    from .supervisor import TMUX_SESSION, TmuxRunner

    if not shutil.which("tmux"):
        sys.exit("mmi: tmux is required for `mmi up`")
    runner = TmuxRunner()
    if runner.has_window("hub"):
        print(f"hub already running in tmux session '{TMUX_SESSION}' (window 'hub')")
        return
    port = args.port or cfg.get("port") or DEFAULT_PORT
    argv = [sys.executable, "-m", "memyselfandi.cli", *(["--profile", PROFILE] if PROFILE else []),
            "serve", "--host", args.host, "--port", str(port)]
    runner.launch("hub", str(Path.cwd()), argv)
    print(f"started hub in tmux session '{TMUX_SESSION}'. `mmi attach` to watch, `mmi attach <window>` for one agent")


def cmd_attach(args, cfg):
    from .supervisor import TMUX_SESSION, window_name

    target = TMUX_SESSION + (f":{window_name(args.window)}" if args.window else "")
    os.execvp("tmux", ["tmux", "attach", "-t", target])


def cmd_config(args, cfg):
    stored = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    if args.new_hub_token:
        stored["hub_token"] = stored["token"] = secrets.token_urlsafe(24)
    for key in ("url", "token", "machine", "public_url", "port", "socket"):
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
    print("PROJECTS")
    for p in board.get("projects", []):
        print(f"  {p['name']:<20} {p['queued']} queued")
    print("\nAGENTS (online)")
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


def _fmt_folders(folders: list[dict], hub_machine: str | None = None, indent: str = "    ") -> str:
    if not folders:
        return f"{indent}(no folders)"
    tag = lambda m: f"{m} (hub)" if m == hub_machine else m  # noqa: E731
    return "\n".join(f"{indent}{tag(f['machine'])}: {f['path']}" for f in folders)


def cmd_project_add(args, cfg):
    """Create a project and/or add folders to it. Folders are on this machine (default: the current folder);
    git isn't involved, except that --git also clones a copy onto the hub for the organizer to read."""
    folders = [str(Path(f).expanduser().resolve()) for f in args.folders]
    if not folders and not args.git and not args.no_folder:
        folders = [os.getcwd()]
    missing = [f for f in folders if not Path(f).is_dir()]
    if missing:
        sys.exit(f"mmi: not a folder on this machine: {', '.join(missing)}")
    p = admin(cfg, "add_project", name=args.name, folders=folders, machine=cfg["machine"], git_url=args.git,
              description=args.description, organizers=args.organizers, trust=True if args.trust else None)
    print(f"project {p['name']}:")
    print(_fmt_folders(p["folders"], p["hub_machine"]))
    if p.get("cloned"):
        print(f"  (cloned {args.git} into {p['cloned']} on the hub)")
    if not p["hub_folders"]:
        print(f"  note: no folders on the hub ({p['hub_machine']}), so the organizer can coordinate but can't read "
              "code. Add one there, or pass --git <url> to clone a copy onto the hub.")
    elif args.trust:
        print("  the hub will accept Claude Code's folder-trust prompt for it (you passed --trust)")
    if not args.trust and p["hub_folders"]:
        print(f"  first time only: Claude Code may ask whether to trust {p['hub_folders'][0]}. "
              f"`mmi attach org-{p['name']}-1` on the hub and answer it (or re-run with --trust)")
    print("  sessions in these folders connect to it with /memyselfandi")


def cmd_project_remove_folder(args, cfg):
    folders = [str(Path(f).expanduser().resolve()) for f in args.folders] or [os.getcwd()]
    p = admin(cfg, "remove_folders", name=args.name, folders=folders, machine=cfg["machine"])
    for f in folders:
        print(f"{'removed' if f in p['removed'] else 'not in the project'}: {cfg['machine']}: {f}")
    print(f"project {p['name']} now:")
    print(_fmt_folders(p["folders"], p["hub_machine"]))


def cmd_project_show(args, cfg):
    p = next((p for p in call(cfg, "list_projects") if p["name"] == args.name), None)
    if not p:
        sys.exit(f"mmi: no project {args.name!r}")
    print(f"{p['name']}  ({p['organizers']} organizer(s), {p['queued']} queued"
          f"{', auto-trust' if p.get('auto_trust') else ''})")
    if p.get("description"):
        print(f"  {p['description']}")
    print(_fmt_folders(p["folders"], p["hub_machine"], indent="  "))


def cmd_project_delete(args, cfg):
    rows = admin(cfg, "project_rows", name=args.name)
    print(f"This permanently deletes project '{args.name}': " + ", ".join(f"{n} {k}" for k, n in rows.items())
          + ", plus its ledger. Its organizer and council sessions will be closed.")
    if not args.yes:
        if not sys.stdin.isatty() or input(f"Type the project name to confirm: ").strip() != args.name:
            sys.exit("mmi: not deleted")
    admin(cfg, "delete_project", name=args.name)
    print(f"deleted {args.name}")


def cmd_invite(args, cfg):
    inv = admin(cfg, "create_invite", name=args.name, days=args.days)
    until = time.strftime("%Y-%m-%d %H:%M", time.localtime(inv["expires_at"]))
    url = cfg.get("public_url") or "<hub url: set with `mmi config --public-url ...`>"
    print(f"invite for {inv['name']}, valid until {until} (shown once; `mmi revoke {inv['name']}` to cut off early)\n")
    print("Send them this (after you share the hub's Tailscale node with them):\n")
    print("  git clone https://github.com/jacob-erard-professional/memyselfandi && cd memyselfandi && uv sync")
    print(f"  uv run mmi config --url {url} --token {inv['token']} --machine {args.name}")
    print("  uv run mmi install-claude      # then /memyselfandi in a Claude Code session")


def cmd_invites(args, cfg):
    invites = admin(cfg, "list_invites")
    if not invites:
        print("  (no invites)")
    for i in invites:
        state = "active" if i["active"] else ("revoked" if i["revoked_at"] else "expired")
        until = time.strftime("%Y-%m-%d %H:%M", time.localtime(i["expires_at"]))
        print(f"  {i['name']:<20} {state:<8} until {until}")


def cmd_revoke(args, cfg):
    admin(cfg, "revoke_invite", name=args.name)
    print(f"revoked {args.name}: their token stops working now. Also remove their Tailscale share "
          "(admin console > Machines > the shared node > Share).")


def cmd_projects(args, cfg):
    for p in call(cfg, "list_projects"):
        print(f"{p['name']}  ({p['queued']} queued)")
        print(_fmt_folders(p["folders"], p["hub_machine"]))


def cmd_ls(args, cfg):
    """Organizers per project: who's running, what they're doing, queue, council."""
    rows = admin(cfg, "organizers")
    if not rows:
        print("  (no projects; `mmi project add` in a repo)")
        return
    for r in rows:
        live = [o for o in r["organizers"] if o["online"]]
        print(f"{r['project']}  ({len(live)}/{r['wanted']} organizers up, {r['queued']} queued"
              f"{', ' + str(len(r['council'])) + ' council' if r['council'] else ''})")
        if not r["organizers"]:
            print("    ○ not started yet")
        for o in r["organizers"]:
            dot = "●" if o["online"] else "○"
            doing = f"handling {o['handling']}" if o["handling"] else (o.get("status") or "")
            if not o["online"]:
                doing = "offline"
            print(f"    {dot} {o['id']:<24} {doing:<40} seen {ago(o['last_seen_ago_s'])} ago")
        for c in r["council"]:
            print(f"    ◆ {c['id']:<24} council {c['tier']} ({c['status']}): {c['role']}")
        if args.folders:
            print(_fmt_folders(r["folders"], r["hub_machine"], indent="      "))


def fmt_tickets(tickets: list[dict]) -> str:
    if not tickets:
        return "  (queue empty)"
    out = []
    for t in tickets:
        where = f"#{t['position']} in line" if t["status"] == "queued" else f"{t['status']} by {t['handler']}"
        flag = " BLOCKING" if t["blocking"] else ""
        out.append(f"  {t['id']:<11} {t['kind']:<14} from {t['asker']:<16} {where}{flag}\n      {t['body'][:120]}")
    return "\n".join(out)


def cmd_queue(args, cfg):
    print(fmt_tickets(call(cfg, "list_queue", project=args.project)))


def cmd_ask(args, cfg):
    t = call(cfg, "ask_organizer", agent_id=me(cfg), project=args.project, body=" ".join(args.body),
             kind=args.kind, blocking=args.wait)
    print(f"queued {t['id']} (#{t['position']} in line)")
    if args.wait:
        while t["status"] not in ("answered", "cancelled"):
            t = call(cfg, "wait_for_reply", ticket_id=t["id"], timeout_s=60)
            if t["status"] != "answered":
                print(f"  ...{t['status']}" + (f" (#{t['position']})" if t.get("position") else ""))
        print(f"\n{t['answered_by']}:\n{t['reply']}")


def cmd_ticket(args, cfg):
    t = call(cfg, "check_ticket", ticket_id=args.ticket)
    print(fmt_tickets([t]) if t["status"] != "answered" else f"{t['answered_by']}:\n{t['reply']}")


def cmd_ledger(args, cfg):
    led = call(cfg, "get_ledger", project=args.project)
    print(led["summary"] or "(no summary yet)")
    if led["log"]:
        print("\nRECENT LOG")
        for e in led["log"]:
            print(f"  {time.strftime('%m-%d %H:%M', time.localtime(e['created_at']))} [{e['kind']}] "
                  f"{e['author']}: {e['body'][:200]}")


def cmd_council(args, cfg):
    members = call(cfg, "list_council", project=args.project, include_finished=args.all)
    if not members:
        print("  (no council members)")
    for c in members:
        print(f"  {c['id']:<12} {c['tier']:<8} {c['model']:<18} {c['status']:<8} {c['role']}\n      {c['brief'][:120]}")


def cmd_connect(args, cfg):
    from .claude_hooks import connect

    try:
        info = connect(cfg, call, args.cwd, args.project)
    except LookupError as e:
        sys.exit(f"mmi: {e}")
    print(info["instructions"] if not args.json else json.dumps(info))


def cmd_install_claude(args, cfg):
    from .claude_hooks import install, uninstall

    for line in (uninstall() if args.uninstall else install(cfg, dry_run=args.dry_run)):
        print(("would set: " if args.dry_run else "") + line)
    if not args.uninstall:
        print("sessions connect only when you invoke /memyselfandi (or ask to check in with the organizer)")


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
    p.add_argument("--profile", help="use a separate hub (own config/db/tmux), e.g. 'collab' for the shared one")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the hub (on the always-on machine)")
    s.add_argument("--host", default=os.environ.get("MMI_HOST", "127.0.0.1"), help="bind address (use your tailscale IP or 0.0.0.0)")
    s.add_argument("--port", type=int, default=int(os.environ["MMI_PORT"]) if os.environ.get("MMI_PORT") else None)
    s.add_argument("--db", default=None)
    s.add_argument("--no-supervise", action="store_true", help="don't launch organizers/council in tmux")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("up", help="start the hub inside tmux session 'mmi' (organizers + council live there too)")
    s.add_argument("--host", default=os.environ.get("MMI_HOST", "0.0.0.0"))
    s.add_argument("--port", type=int, default=int(os.environ["MMI_PORT"]) if os.environ.get("MMI_PORT") else None)
    s.set_defaults(fn=cmd_up)

    s = sub.add_parser("attach", help="attach to the tmux session (optionally one window, e.g. org-myproj-1)")
    s.add_argument("window", nargs="?")
    s.set_defaults(fn=cmd_attach)

    s = sub.add_parser("project", help="register a project (gives it an organizer)")
    psub = s.add_subparsers(dest="pcmd", required=True)
    pa = psub.add_parser("add", help="create a project and/or add folders on this machine to it")
    pa.add_argument("name")
    pa.add_argument("folders", nargs="*", help="folders on this machine (default: the current folder)")
    pa.add_argument("--git", help="also clone this URL onto the hub as one of the project's folders")
    pa.add_argument("--no-folder", action="store_true", help="just create the project, no folder")
    pa.add_argument("--trust", action="store_true",
                    help="you trust its hub folders: let the hub accept Claude Code's folder-trust prompt")
    pa.add_argument("-d", "--description")
    pa.add_argument("--organizers", type=int)
    pa.set_defaults(fn=cmd_project_add)
    pr = psub.add_parser("remove-folder", help="take folders on this machine out of a project")
    pr.add_argument("name")
    pr.add_argument("folders", nargs="*", help="default: the current folder")
    pr.set_defaults(fn=cmd_project_remove_folder)
    ps = psub.add_parser("show", help="a project's folders on every machine")
    ps.add_argument("name")
    ps.set_defaults(fn=cmd_project_show)
    psub.add_parser("list").set_defaults(fn=cmd_projects)
    pd = psub.add_parser("delete", help="permanently delete a project and every row tagged with it")
    pd.add_argument("name")
    pd.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    pd.set_defaults(fn=cmd_project_delete)

    s = sub.add_parser("ls", help="organizers per project: running?, doing what, queue")
    s.add_argument("-f", "--folders", action="store_true", help="also list each project's folders")
    s.set_defaults(fn=cmd_ls)

    s = sub.add_parser("invite", help="give someone a token that expires (use with --profile collab)")
    s.add_argument("name", help="who it's for (also their machine name)")
    s.add_argument("--days", type=float, required=True)
    s.set_defaults(fn=cmd_invite)
    sub.add_parser("invites", help="list invites and when they expire").set_defaults(fn=cmd_invites)
    s = sub.add_parser("revoke", help="revoke someone's invite token now")
    s.add_argument("name")
    s.set_defaults(fn=cmd_revoke)

    s = sub.add_parser("queue", help="a project's organizer queue")
    s.add_argument("project")
    s.set_defaults(fn=cmd_queue)

    s = sub.add_parser("ask", help="queue a ticket for a project's organizer (as you)")
    s.add_argument("project")
    s.add_argument("body", nargs="+")
    s.add_argument("-k", "--kind", default="question", choices=["checkin", "question", "report", "handoff"])
    s.add_argument("-w", "--wait", action="store_true", help="wait for the reply")
    s.set_defaults(fn=cmd_ask)

    s = sub.add_parser("ticket", help="check a ticket / read its reply")
    s.add_argument("ticket")
    s.set_defaults(fn=cmd_ticket)

    s = sub.add_parser("ledger", help="a project's living summary + recent log")
    s.add_argument("project")
    s.set_defaults(fn=cmd_ledger)

    s = sub.add_parser("council", help="council members on a project")
    s.add_argument("project")
    s.add_argument("-a", "--all", action="store_true", help="include finished")
    s.set_defaults(fn=cmd_council)

    s = sub.add_parser("config", help="show/set hub url, token, machine name")
    s.add_argument("--url")
    s.add_argument("--token")
    s.add_argument("--machine")
    s.add_argument("--public-url", help="hub URL invitees use (e.g. the shared node's tailnet address)")
    s.add_argument("--port", type=int, help="port this profile's hub listens on")
    s.add_argument("--socket", help="also listen on this unix socket (the collab sidecar connects through it)")
    s.add_argument("--new-hub-token", action="store_true", help="generate a token for the hub you serve here")
    s.add_argument("--show-token", action="store_true")
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("mcp-setup", help="print/apply config that connects Claude Code + Codex to the hub")
    s.add_argument("--apply", action="store_true", help="run the `claude mcp add` command")
    s.set_defaults(fn=cmd_mcp_setup)

    s = sub.add_parser("connect", help="register this session as a worker on a project and print the protocol")
    s.add_argument("--cwd", default=os.getcwd())
    s.add_argument("--project")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_connect)

    s = sub.add_parser("install-claude", help="set up this machine's Claude Code: MCP server + /memyselfandi skill")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--uninstall", action="store_true")
    s.set_defaults(fn=cmd_install_claude)

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
    if args.profile:
        use_profile(args.profile)
    try:
        args.fn(args, load_config())
    except HubError as e:
        sys.exit(f"mmi: {e}")


if __name__ == "__main__":
    main()
