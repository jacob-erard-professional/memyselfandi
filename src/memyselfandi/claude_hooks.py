"""Claude Code integration: a SessionStart hook that connects every new session to its project's organizer,
plus an installer that wires the MCP server, the hook, and a permission rule into ~/.claude/settings.json."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

CLAUDE_SETTINGS = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "settings.json"
HOOK_MARKER = "memyselfandi.cli hook session-start"


def _git_root(cwd: str) -> Path | None:
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                             timeout=3)
        return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None
    except Exception:
        return None


def resolve_project(cwd: str, projects: list[dict[str, Any]], mapping: dict[str, str]) -> str | None:
    """Which memyselfandi project is this directory? First match wins:
    1. config "projects" mapping (local path prefix -> project name), for when names differ per machine
    2. the project's hub path (sessions on the hub machine itself)
    3. the git repo's folder name equals the project name (same repo cloned on another machine)"""
    here = Path(cwd).resolve()
    names = {p["name"] for p in projects}

    def under(base: str) -> bool:
        try:
            here.relative_to(Path(base).expanduser().resolve())
            return True
        except ValueError:
            return False

    for base, name in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
        if name in names and under(base):
            return name
    for p in sorted(projects, key=lambda p: -len(p["path"])):
        if under(p["path"]):
            return p["name"]
    root = _git_root(cwd)
    if root and root.name in names:
        return root.name
    return None


def context_for(agent_id: str, project: str, machine: str, source: str) -> str:
    resumed = source in ("resume", "compact")
    return f"""\
[memyselfandi] This Claude Code session is connected to the memyselfandi hub as worker agent `{agent_id}`
on machine `{machine}`, in project `{project}`. That project has an ORGANIZER agent that coordinates all
agents across the user's machines and remembers where work left off. Use the `memyselfandi` MCP tools,
passing agent_id="{agent_id}" and project="{project}". You're already registered.

{"You were resumed or compacted. If you already checked in this session, continue your assignment; otherwise check in."
 if resumed else "Before starting work, check in:"}
- ask_organizer(kind="checkin", blocking=true, body=<what the user asked you to do in this session, or "ready
  for work" if nothing yet, plus anything notable you see>) then wait_for_reply until answered. The reply
  is your briefing: context from earlier agents and your assignment.
- The user's direct instructions in this session take precedence over the organizer's. If the user asked
  for something specific, tell the organizer in your check-in and do what the user asked.
- Questions for the organizer: blocking=true only if the answer is the ONLY thing unblocking you; otherwise
  blocking=false, keep working, and check_ticket later.
- Before you finish or the session ends: ask_organizer(kind="report" or "handoff") with what's done, what's
  left, and where (paths, branches, task ids), so the next agent can continue.
"""


def session_start(stdin_text: str, cfg: dict[str, Any], call) -> str:
    """Hook entrypoint. Returns the JSON for stdout, or "" when this session isn't ours. Must never block a
    session from starting, so callers swallow errors."""
    if os.environ.get("MMI_ROLE"):  # organizer/council sessions launched by the supervisor
        return ""
    data = json.loads(stdin_text or "{}")
    cwd = data.get("cwd") or os.getcwd()
    project = resolve_project(cwd, call(cfg, "list_projects"), cfg.get("projects", {}))
    if not project:
        return ""
    session = (data.get("session_id") or str(time.time_ns()))[:8]
    agent_id = f"cc-{cfg['machine']}-{session}"
    call(cfg, "register", name=f"claude-code@{cfg['machine']}", machine=cfg["machine"], kind="claude-code",
         project=project, status=f"session in {cwd}", agent_id=agent_id)
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": context_for(agent_id, project, cfg["machine"], data.get("source", "startup")),
    }})


# -- installer ---------------------------------------------------------------
def hook_command() -> str:
    return f'"{sys.executable}" -m {HOOK_MARKER}'


def install(cfg: dict[str, Any], dry_run: bool = False) -> list[str]:
    """Add the hub as a user-scoped MCP server, the SessionStart hook, and allow its tools without prompts.
    Idempotent; backs up settings.json before changing it."""
    done = []
    url = cfg["url"].rstrip("/") + "/mcp"
    mcp = ["claude", "mcp", "add", "--scope", "user", "--transport", "http", "memyselfandi", url]
    if cfg.get("token"):
        mcp += ["--header", f"Authorization: Bearer {cfg['token']}"]
    if not dry_run:
        if not shutil.which("claude"):
            raise RuntimeError("claude CLI not found on PATH")
        subprocess.run(["claude", "mcp", "remove", "--scope", "user", "memyselfandi"], capture_output=True)
        subprocess.run(mcp, check=True, capture_output=True)
    done.append(f"MCP server 'memyselfandi' -> {url} (user scope)")

    settings = json.loads(CLAUDE_SETTINGS.read_text()) if CLAUDE_SETTINGS.exists() else {}
    before = json.dumps(settings, sort_keys=True)
    entries = settings.setdefault("hooks", {}).setdefault("SessionStart", [])
    entries[:] = [e for e in entries if not any(HOOK_MARKER in h.get("command", "") for h in e.get("hooks", []))]
    entries.append({"matcher": "*", "hooks": [{"type": "command", "command": hook_command(), "timeout": 10}]})
    allow = settings.setdefault("permissions", {}).setdefault("allow", [])
    if "mcp__memyselfandi" not in allow:
        allow.append("mcp__memyselfandi")
    if json.dumps(settings, sort_keys=True) != before and not dry_run:
        if CLAUDE_SETTINGS.exists():
            shutil.copy2(CLAUDE_SETTINGS, CLAUDE_SETTINGS.with_suffix(f".json.bak-mmi-{time.strftime('%Y%m%d%H%M%S')}"))
        CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        CLAUDE_SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    done.append(f"SessionStart hook + allow rule 'mcp__memyselfandi' in {CLAUDE_SETTINGS}")
    return done


def uninstall() -> list[str]:
    subprocess.run(["claude", "mcp", "remove", "--scope", "user", "memyselfandi"], capture_output=True)
    if CLAUDE_SETTINGS.exists():
        settings = json.loads(CLAUDE_SETTINGS.read_text())
        entries = settings.get("hooks", {}).get("SessionStart", [])
        entries[:] = [e for e in entries if not any(HOOK_MARKER in h.get("command", "") for h in e.get("hooks", []))]
        allow = settings.get("permissions", {}).get("allow", [])
        if "mcp__memyselfandi" in allow:
            allow.remove("mcp__memyselfandi")
        CLAUDE_SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    return ["removed MCP server, hook, and allow rule"]
