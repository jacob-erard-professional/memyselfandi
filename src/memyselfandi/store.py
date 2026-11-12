"""SQLite-backed shared state: agents, tasks, messages.

Everything lives in one file on the hub machine. All writes go through this
module so the rules (atomic claims, heartbeat staleness) live in one place.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

# An agent that hasn't sent a heartbeat in this many seconds is shown as offline.
STALE_AFTER_S = 15 * 60

TASK_STATUSES = ("open", "claimed", "in_progress", "blocked", "done", "cancelled")

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    machine     TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- claude-code, codex, human, ...
    project     TEXT,                   -- repo / working dir the agent is in
    status      TEXT,                   -- free-form "what I'm doing right now"
    created_at  REAL NOT NULL,
    last_seen   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    project     TEXT,
    status      TEXT NOT NULL DEFAULT 'open',
    priority    INTEGER NOT NULL DEFAULT 2, -- 0 urgent .. 3 low
    created_by  TEXT,
    assignee    TEXT,                   -- agent id
    target_machine TEXT,                -- optional: only agents on this machine should claim
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS task_notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    author      TEXT,
    body        TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sender      TEXT,
    recipient   TEXT NOT NULL,          -- agent id, or '*' for broadcast
    body        TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reads (
    agent_id    TEXT NOT NULL,
    message_id  INTEGER NOT NULL,
    PRIMARY KEY (agent_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient);
"""


def _short_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class NotFound(LookupError):
    pass


class Conflict(RuntimeError):
    pass


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)

    # -- helpers -----------------------------------------------------------
    def _q(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def _x(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    @staticmethod
    def _decorate_agent(a: dict[str, Any], now: float) -> dict[str, Any]:
        a["online"] = (now - a["last_seen"]) < STALE_AFTER_S
        a["last_seen_ago_s"] = int(now - a["last_seen"])
        return a

    # -- agents ------------------------------------------------------------
    def register(
        self,
        name: str,
        machine: str,
        kind: str,
        project: str | None = None,
        status: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Create an agent, or re-register an existing id (e.g. after a restart)."""
        now = time.time()
        agent_id = agent_id or _short_id("ag")
        self._x(
            """INSERT INTO agents (id, name, machine, kind, project, status, created_at, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, machine=excluded.machine,
                   kind=excluded.kind, project=excluded.project,
                   status=COALESCE(excluded.status, agents.status), last_seen=excluded.last_seen""",
            (agent_id, name, machine, kind, project, status, now, now),
        )
        return self.get_agent(agent_id)

    def heartbeat(self, agent_id: str, status: str | None = None) -> dict[str, Any]:
        cur = self._x(
            "UPDATE agents SET last_seen=?, status=COALESCE(?, status) WHERE id=?",
            (time.time(), status, agent_id),
        )
        if cur.rowcount == 0:
            raise NotFound(f"unknown agent {agent_id!r}; call register first")
        return self.get_agent(agent_id)

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        rows = self._q("SELECT * FROM agents WHERE id=?", (agent_id,))
        if not rows:
            raise NotFound(f"unknown agent {agent_id!r}")
        return self._decorate_agent(rows[0], time.time())

    def list_agents(self, include_offline: bool = True) -> list[dict[str, Any]]:
        now = time.time()
        agents = [self._decorate_agent(a, now) for a in self._q("SELECT * FROM agents ORDER BY last_seen DESC")]
        if not include_offline:
            agents = [a for a in agents if a["online"]]
        for a in agents:
            a["active_tasks"] = [
                t["id"]
                for t in self._q(
                    "SELECT id FROM tasks WHERE assignee=? AND status IN ('claimed','in_progress','blocked')",
                    (a["id"],),
                )
            ]
        return agents

    def remove_agent(self, agent_id: str) -> None:
        """Forget an agent and release its unfinished tasks back to 'open'."""
        self.get_agent(agent_id)
        self._x(
            "UPDATE tasks SET assignee=NULL, status='open', updated_at=? "
            "WHERE assignee=? AND status IN ('claimed','in_progress','blocked')",
            (time.time(), agent_id),
        )
        self._x("DELETE FROM agents WHERE id=?", (agent_id,))

    # -- tasks -------------------------------------------------------------
    def create_task(
        self,
        title: str,
        description: str = "",
        project: str | None = None,
        priority: int = 2,
        created_by: str | None = None,
        assignee: str | None = None,
        target_machine: str | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        task_id = _short_id("t")
        status = "claimed" if assignee else "open"
        self._x(
            """INSERT INTO tasks (id, title, description, project, status, priority, created_by,
                                  assignee, target_machine, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, title, description, project, status, priority, created_by, assignee, target_machine, now, now),
        )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any]:
        rows = self._q("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not rows:
            raise NotFound(f"unknown task {task_id!r}")
        task = rows[0]
        task["notes"] = self._q(
            "SELECT author, body, created_at FROM task_notes WHERE task_id=? ORDER BY id", (task_id,)
        )
        return task

    def list_tasks(
        self,
        status: str | None = None,
        project: str | None = None,
        assignee: str | None = None,
        include_closed: bool = False,
    ) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM tasks WHERE 1=1", []
        if status:
            sql += " AND status=?"
            args.append(status)
        elif not include_closed:
            sql += " AND status NOT IN ('done','cancelled')"
        if project:
            sql += " AND project=?"
            args.append(project)
        if assignee:
            sql += " AND assignee=?"
            args.append(assignee)
        sql += " ORDER BY priority, created_at"
        return self._q(sql, tuple(args))

    def claim_task(self, task_id: str, agent_id: str) -> dict[str, Any]:
        """Atomically claim an open task. Fails if someone else got it first."""
        self.get_agent(agent_id)
        cur = self._x(
            "UPDATE tasks SET status='claimed', assignee=?, updated_at=? WHERE id=? AND status='open'",
            (agent_id, time.time(), task_id),
        )
        if cur.rowcount == 0:
            task = self.get_task(task_id)  # raises NotFound if missing
            if task["assignee"] == agent_id:
                return task
            raise Conflict(f"task {task_id} is {task['status']} (assignee: {task['assignee']})")
        return self.get_task(task_id)

    def update_task(
        self,
        task_id: str,
        author: str | None = None,
        status: str | None = None,
        note: str | None = None,
        assignee: str | None = None,
        title: str | None = None,
        description: str | None = None,
        priority: int | None = None,
    ) -> dict[str, Any]:
        self.get_task(task_id)
        if status and status not in TASK_STATUSES:
            raise ValueError(f"status must be one of {TASK_STATUSES}")
        sets, args = ["updated_at=?"], [time.time()]
        for col, val in (
            ("status", status),
            ("assignee", assignee),
            ("title", title),
            ("description", description),
            ("priority", priority),
        ):
            if val is not None:
                sets.append(f"{col}=?")
                args.append(val)
        if status == "open" and assignee is None:
            sets.append("assignee=NULL")  # releasing a task
        self._x(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", (*args, task_id))
        if note:
            self._x(
                "INSERT INTO task_notes (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                (task_id, author, note, time.time()),
            )
        return self.get_task(task_id)

    # -- messages ----------------------------------------------------------
    def send_message(self, sender: str | None, recipient: str, body: str) -> dict[str, Any]:
        if recipient != "*":
            self.get_agent(recipient)
        cur = self._x(
            "INSERT INTO messages (sender, recipient, body, created_at) VALUES (?, ?, ?, ?)",
            (sender, recipient, body, time.time()),
        )
        return {"id": cur.lastrowid, "sender": sender, "recipient": recipient, "body": body}

    def inbox(self, agent_id: str, unread_only: bool = True, mark_read: bool = True) -> list[dict[str, Any]]:
        self.get_agent(agent_id)
        sql = (
            "SELECT m.* FROM messages m WHERE (m.recipient=? OR (m.recipient='*' AND COALESCE(m.sender,'')!=?))"
        )
        if unread_only:
            sql += " AND NOT EXISTS (SELECT 1 FROM reads r WHERE r.agent_id=? AND r.message_id=m.id)"
            rows = self._q(sql + " ORDER BY m.id", (agent_id, agent_id, agent_id))
        else:
            rows = self._q(sql + " ORDER BY m.id", (agent_id, agent_id))
        if mark_read:
            for m in rows:
                self._x("INSERT OR IGNORE INTO reads (agent_id, message_id) VALUES (?, ?)", (agent_id, m["id"]))
        return rows

    # -- overview ----------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "agents": self.list_agents(),
            "tasks": self.list_tasks(),
            "recent_messages": self._q("SELECT * FROM messages ORDER BY id DESC LIMIT 20"),
        }

    def dump(self) -> str:
        return json.dumps(self.snapshot(), indent=2, default=str)
