"""Keeps organizers and council members running as Claude Code sessions in tmux.

Runs inside the hub process. Every few seconds it:
  - makes sure each project's organizer window(s) exist (relaunching if one died, and putting
    any ticket it was holding back in line)
  - wakes an idle organizer when its queue has something in it, by typing into its session, so an
    idle organizer costs no tokens
  - launches pending council members, and cleans up / fails ones that finished, died, or overran

Attach with `mmi attach` (or `tmux attach -t mmi`) to watch or steer any of them.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
from importlib import resources
from pathlib import Path
from typing import Protocol

from .store import ORGANIZER_MODEL, Store

log = logging.getLogger("memyselfandi.supervisor")

TMUX_SESSION = os.environ.get("MMI_TMUX_SESSION", "mmi")
TICK_S = 3
STARTUP_GRACE_S = 20        # let a fresh session boot before typing into it
NUDGE_EVERY_S = 30          # don't re-nudge an idle organizer more often than this
STALE_HOLD_S = 15 * 60      # organizer has held a ticket this long -> remind it
COUNCIL_MAX_S = 90 * 60     # council members are dismissed after this long
COUNCIL_LINGER_S = 30       # keep a finished council window around briefly so you can glance at it

SESSION_TOOLS = "mcp__memyselfandi,Read,Grep,Glob"


class Runner(Protocol):
    def has_window(self, name: str) -> bool: ...
    def launch(self, name: str, cwd: str, argv: list[str]) -> None: ...
    def send(self, name: str, text: str) -> None: ...
    def kill(self, name: str) -> None: ...
    def capture(self, name: str) -> str: ...


class TmuxRunner:
    def __init__(self, session: str = TMUX_SESSION):
        self.session = session

    def _tmux(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(["tmux", *args], capture_output=True, text=True, check=check)

    def _session_exists(self) -> bool:
        return self._tmux("has-session", "-t", f"={self.session}").returncode == 0

    def has_window(self, name: str) -> bool:
        r = self._tmux("list-windows", "-t", f"={self.session}", "-F", "#{window_name}")
        return r.returncode == 0 and name in r.stdout.split()

    def launch(self, name: str, cwd: str, argv: list[str]) -> None:
        cmd = shlex.join(argv)
        if self._session_exists():
            self._tmux("new-window", "-d", "-t", f"={self.session}:", "-n", name, "-c", cwd, cmd, check=True)
        else:
            self._tmux("new-session", "-d", "-s", self.session, "-n", name, "-c", cwd, cmd, check=True)

    def send(self, name: str, text: str) -> None:
        target = f"={self.session}:{name}"
        self._tmux("send-keys", "-t", target, "-l", text)
        time.sleep(0.3)
        self._tmux("send-keys", "-t", target, "Enter")

    def kill(self, name: str) -> None:
        self._tmux("kill-window", "-t", f"={self.session}:{name}")

    def capture(self, name: str) -> str:
        return self._tmux("capture-pane", "-p", "-t", f"={self.session}:{name}").stdout


def window_name(agent_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", agent_id)


def _template(name: str) -> str:
    return resources.files("memyselfandi").joinpath("prompts", name).read_text()


class Supervisor:
    def __init__(
        self,
        store: Store,
        hub_url: str,
        token: str | None,
        run_dir: Path,
        runner: Runner | None = None,
        claude_bin: str | None = None,
        notify_cmd: str | None = None,
    ):
        self.store = store
        self.hub_url = hub_url.rstrip("/")
        self.token = token
        self.run_dir = Path(run_dir)
        self.runner = runner or TmuxRunner()
        self.claude_bin = claude_bin or os.environ.get("MMI_CLAUDE_BIN", "claude")
        self.notify_cmd = notify_cmd or os.environ.get("MMI_NOTIFY_CMD")
        self.launched_at: dict[str, float] = {}
        self.last_nudge: dict[str, float] = {}
        self.trust_flagged: set[str] = set()
        self._stop = threading.Event()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.mcp_config = self._write_mcp_config()

    # -- setup -------------------------------------------------------------
    def _write_mcp_config(self) -> Path:
        server: dict = {"type": "http", "url": f"{self.hub_url}/mcp"}
        if self.token:
            server["headers"] = {"Authorization": f"Bearer {self.token}"}
        path = self.run_dir / "mcp.json"
        path.write_text(json.dumps({"mcpServers": {"memyselfandi": server}}, indent=2))
        path.chmod(0o600)
        return path

    def _argv(self, agent_id: str, model: str, system_prompt: str, first_message: str, label: str) -> list[str]:
        sp = self.run_dir / f"{window_name(agent_id)}.system.md"
        sp.write_text(system_prompt)
        # Positional prompt first: --allowedTools / --mcp-config are variadic and would swallow it.
        return [
            self.claude_bin, first_message,
            "--model", model,
            "--name", label,
            "--permission-mode", "dontAsk",
            "--strict-mcp-config",
            "--append-system-prompt-file", str(sp),
            "--allowedTools", SESSION_TOOLS,
            "--mcp-config", str(self.mcp_config),
        ]

    def _notify(self, message: str) -> None:
        if not self.notify_cmd:
            return
        try:
            subprocess.run([*shlex.split(self.notify_cmd), message], timeout=15, capture_output=True)
        except Exception as e:  # notification is best-effort
            log.warning("notify failed: %s", e)

    def _stuck_on_trust(self, agent_id: str, project: str) -> bool:
        """Claude Code asks once per folder whether to trust it. That's the user's call, so we don't
        click it; we flag the agent and notify instead."""
        win = window_name(agent_id)
        if "trust this folder" not in self.runner.capture(win):
            self.trust_flagged.discard(agent_id)
            return False
        if agent_id not in self.trust_flagged:
            self.trust_flagged.add(agent_id)
            msg = (f"memyselfandi: {agent_id} ({project}) is waiting on Claude Code's 'trust this folder' "
                   f"prompt. Run `mmi attach {win}` and answer it (one time per repo).")
            log.warning(msg)
            self._notify(msg)
        try:
            self.store.heartbeat(agent_id, status="BLOCKED: waiting for you to accept the folder-trust prompt")
        except LookupError:
            pass
        return True

    # -- organizers ----------------------------------------------------------
    def _launch_organizer(self, project: dict, agent_id: str, now: float) -> None:
        # Anything the previous incarnation was holding goes back in line.
        for t in self.store.held_by(agent_id):
            if t["status"] == "with_organizer":
                self.store.requeue_ticket(t["id"])
        desc = f"\nAbout the project: {project['description']}\n" if project["description"] else ""
        system = _template("organizer.md").format(
            project=project["name"], agent_id=agent_id, path=project["path"], description=desc
        )
        first = (f"You're online as organizer {agent_id} for {project['name']}. "
                 f"Call organizer_next(agent_id='{agent_id}', project='{project['name']}') and work the queue "
                 "until it's empty, then wait.")
        argv = self._argv(agent_id, ORGANIZER_MODEL, system, first, f"organizer:{project['name']}")
        self.runner.launch(window_name(agent_id), project["path"], argv)
        self.launched_at[agent_id] = self.last_nudge[agent_id] = now
        log.info("launched organizer %s", agent_id)

    def _tend_organizer(self, project: dict, n: int, now: float) -> None:
        agent_id = f"org-{project['name']}-{n}"
        try:
            self.store.heartbeat(agent_id, status="organizer")
        except LookupError:
            self.store.register(f"organizer #{n}", "hub", "organizer", project=project["name"],
                                status="organizer", agent_id=agent_id)
        win = window_name(agent_id)
        if not self.runner.has_window(win):
            self._launch_organizer(project, agent_id, now)
            return
        if self._stuck_on_trust(agent_id, project["name"]):
            return
        if now - self.launched_at.get(agent_id, 0) < STARTUP_GRACE_S:
            return
        held = self.store.held_by(agent_id)
        queued = sum(1 for t in self.store.list_tickets(project["name"]) if t["status"] == "queued")
        since_nudge = now - self.last_nudge.get(agent_id, 0)
        if not held and queued and since_nudge >= NUDGE_EVERY_S:
            self.runner.send(win, f"[memyselfandi] {queued} waiting in the queue. Call organizer_next and "
                                  "work the queue until it's empty.")
            self.last_nudge[agent_id] = now
        elif held and now - min(t["picked_at"] or now for t in held) > STALE_HOLD_S and since_nudge >= 5 * 60:
            ids = ", ".join(t["id"] for t in held)
            self.runner.send(win, f"[memyselfandi] You're still holding {ids}. Finish each with answer_ticket "
                                  "(or summon_council with its ticket_id), then call organizer_next.")
            self.last_nudge[agent_id] = now

    # -- council -----------------------------------------------------------
    def _launch_council(self, c: dict) -> None:
        project = self.store.get_project(c["project"])
        ticket_section = ""
        if c["ticket_id"]:
            t = self.store.get_ticket(c["ticket_id"])
            ticket_section = (
                f"\n## Ticket delegated to you: {t['id']} ({t['kind']}, from {t['asker']}"
                f"{', BLOCKING: the asker is stopped until this is answered' if t['blocking'] else ''})\n\n"
                f"{t['body']}\n"
            )
        system = _template("council.md").format(
            tier=c["tier"], project=c["project"], role=c["role"], agent_id=c["id"], path=project["path"],
            brief=c["brief"], ticket_section=ticket_section,
        )
        first = f"You're council member {c['id']}. Do your brief now and finish with council_report."
        argv = self._argv(c["id"], c["model"], system, first, f"council:{c['role']}")
        self.runner.launch(window_name(c["id"]), project["path"], argv)
        self.store.set_council_status(c["id"], "running")
        if c["tier"] == "elder":
            self._notify(f"memyselfandi: elder ({c['model']}) summoned on {c['project']} as '{c['role']}'. "
                         f"Why: {c['justification'][:200]}")
        log.info("launched council %s (%s, %s)", c["id"], c["tier"], c["model"])

    def _fail_council(self, c: dict, why: str) -> None:
        self.store.set_council_status(c["id"], "failed")
        if c["ticket_id"]:
            t = self.store.get_ticket(c["ticket_id"])
            if t["status"] == "delegated" and t["handler"] == c["id"]:
                self.store.requeue_ticket(t["id"])
        self.store.enqueue(c["project"], c["id"], f"Council member '{c['role']}' ({c['tier']}) {why} without a "
                           f"report. Its delegated ticket (if any) is back in your queue.", kind="council_report")
        self.runner.kill(window_name(c["id"]))

    def _tend_council(self, now: float) -> None:
        for c in self.store.list_council(active_only=True):
            win = window_name(c["id"])
            if c["status"] == "pending":
                self._launch_council(c)
            elif not self.runner.has_window(win):
                self._fail_council(c, "exited")
            elif self._stuck_on_trust(c["id"], c["project"]):
                continue
            elif now - (c["started_at"] or now) > COUNCIL_MAX_S:
                self._fail_council(c, f"ran past {COUNCIL_MAX_S // 60} minutes and was dismissed")
        # Dismiss members that reported.
        for c in self.store.list_council(active_only=False):
            if c["status"] == "done" and now - (c["finished_at"] or now) > COUNCIL_LINGER_S:
                win = window_name(c["id"])
                if self.runner.has_window(win):
                    self.runner.kill(win)

    # -- loop --------------------------------------------------------------
    def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        for project in self.store.list_projects():
            for n in range(1, project["organizers"] + 1):
                self._tend_organizer(project, n, now)
        self._tend_council(now)

    def run_forever(self, initial_delay_s: float = 3) -> None:
        self._stop.wait(initial_delay_s)  # let the HTTP server come up first
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("supervisor tick failed")
            self._stop.wait(TICK_S)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run_forever, name="mmi-supervisor", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
