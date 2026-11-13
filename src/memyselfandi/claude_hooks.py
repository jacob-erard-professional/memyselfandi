"""Claude Code integration. Opt-in: a session only connects to an organizer when you invoke the
`/memyselfandi` skill (or ask for it). The installer adds the MCP server, the skill, and a permission rule."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from importlib import resources
from pathlib import Path
from typing import Any

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
CLAUDE_SETTINGS = CLAUDE_DIR / "settings.json"
SKILL_DIR = CLAUDE_DIR / "skills" / "memyselfandi"
HOOK_MARKER = "memyselfandi.cli hook session-start"  # earlier versions installed an auto-check-in hook


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


def protocol(agent_id: str, project: str, machine: str) -> str:
    return f"""\
Connected to memyselfandi as worker `{agent_id}` on `{machine}`, project `{project}`.
Pass agent_id="{agent_id}" and project="{project}" to the memyselfandi MCP tools.

1. Check in: ask_organizer(kind="checkin", blocking=true, body=<what the user wants from this session, or
   "ready for work", plus anything notable>), then wait_for_reply until status is answered (call it again
   while it's queued / with_organizer / delegated). The reply is your briefing and assignment.
2. The user's instructions in this session take precedence over the organizer's. If they asked for
   something specific, say so in the check-in and do what they asked.
3. Questions: blocking=true only if the answer is the ONLY thing unblocking you; otherwise blocking=false,
   keep working, and check_ticket later. heartbeat(status=...) when your focus changes.
4. Before you finish: ask_organizer(kind="report" or "handoff") with what's done, what's left, and where
   (paths, branches, task ids), so the next agent can continue.
"""


def connect(cfg: dict[str, Any], call, cwd: str, project: str | None = None) -> dict[str, Any]:
    """Register this session as a worker on the project for `cwd` (or the named one)."""
    projects = call(cfg, "list_projects")
    names = sorted(p["name"] for p in projects)
    if project and project not in names:
        raise LookupError(f"no project {project!r}; projects: {', '.join(names) or '(none)'}")
    project = project or resolve_project(cwd, projects, cfg.get("projects", {}))
    if not project:
        raise LookupError(f"{cwd} isn't a registered project. Projects: {', '.join(names) or '(none)'}. "
                          "Pass one explicitly, or the user can run `mmi project add` on the hub.")
    agent_id = f"cc-{cfg['machine']}-{os.urandom(4).hex()}"
    call(cfg, "register", name=f"claude-code@{cfg['machine']}", machine=cfg["machine"], kind="claude-code",
         project=project, status=f"session in {cwd}", agent_id=agent_id)
    return {"agent_id": agent_id, "project": project, "machine": cfg["machine"],
            "instructions": protocol(agent_id, project, cfg["machine"])}


def render_skill() -> str:
    """The /memyselfandi skill as installed: prompts/skill.md with this machine's mmi command filled in."""
    template = resources.files("memyselfandi").joinpath("prompts", "skill.md").read_text()
    return template.replace("{command}", mmi_command())


# -- installer ---------------------------------------------------------------
def mmi_command() -> str:
    return f'"{sys.executable}" -m memyselfandi.cli'


def install(cfg: dict[str, Any], dry_run: bool = False) -> list[str]:
    """Add the hub as a user-scoped MCP server, the /memyselfandi skill, and allow its tools without prompts.
    Removes the auto-check-in hook older versions installed. Idempotent; backs up settings.json first."""
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
    if not entries:
        del settings["hooks"]["SessionStart"]
    if not settings["hooks"]:
        del settings["hooks"]
    allow = settings.setdefault("permissions", {}).setdefault("allow", [])
    if "mcp__memyselfandi" not in allow:
        allow.append("mcp__memyselfandi")
    if json.dumps(settings, sort_keys=True) != before and not dry_run:
        if CLAUDE_SETTINGS.exists():
            shutil.copy2(CLAUDE_SETTINGS, CLAUDE_SETTINGS.with_suffix(f".json.bak-mmi-{time.strftime('%Y%m%d%H%M%S')}"))
        CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        CLAUDE_SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    done.append(f"allow rule 'mcp__memyselfandi' in {CLAUDE_SETTINGS} (no auto-check-in hook)")
    if not dry_run:
        SKILL_DIR.mkdir(parents=True, exist_ok=True)
        (SKILL_DIR / "SKILL.md").write_text(render_skill())
    done.append(f"skill /memyselfandi in {SKILL_DIR}")
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
    shutil.rmtree(SKILL_DIR, ignore_errors=True)
    return ["removed MCP server, skill, allow rule (and any old hook)"]
