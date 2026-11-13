"""SQLite-backed shared state: agents, tasks, messages.

Everything lives in one file on the hub machine. All writes go through this
module so the rules (atomic claims, heartbeat staleness) live in one place.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

# An agent that hasn't sent a heartbeat in this many seconds is shown as offline.
STALE_AFTER_S = 15 * 60

TASK_STATUSES = ("open", "claimed", "in_progress", "blocked", "done", "cancelled")
TICKET_KINDS = ("checkin", "question", "report", "handoff", "council_report")

# Council tiers -> model. Overridable via hub config ("tiers": {...}).
DEFAULT_TIERS = {
    "routine": "claude-sonnet-5-5",  # routing, lookups, summaries, simple breakdowns
    "senior": "claude-opus-5",       # real design/debugging/planning within or across areas
    "elder": "claude-fable-5-2",     # only extreme, ground-breaking changes
}
ORGANIZER_MODEL = "claude-opus-5-5"

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
CREATE TABLE IF NOT EXISTS projects (
    name        TEXT PRIMARY KEY,
    path        TEXT NOT NULL,          -- repo path on the hub machine (organizer/council read code here)
    description TEXT NOT NULL DEFAULT '',
    organizers  INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    project     TEXT PRIMARY KEY REFERENCES projects(name),
    summary     TEXT NOT NULL DEFAULT '',  -- organizer-maintained "state of the project"
    updated_by  TEXT,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT NOT NULL,
    author      TEXT,
    kind        TEXT NOT NULL DEFAULT 'note',  -- note | decision | handoff | council
    body        TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
    id          TEXT PRIMARY KEY,
    project     TEXT NOT NULL REFERENCES projects(name),
    asker       TEXT NOT NULL,           -- agent id
    kind        TEXT NOT NULL,           -- checkin | question | report | handoff | council_report
    body        TEXT NOT NULL,
    blocking    INTEGER NOT NULL DEFAULT 0,  -- asker can't make any progress until answered
    task_id     TEXT,
    lane        INTEGER NOT NULL DEFAULT 1,  -- 0 = council reports (front of line), 1 = everyone else
    status      TEXT NOT NULL DEFAULT 'queued', -- queued | with_organizer | delegated | answered | cancelled
    handler     TEXT,                    -- organizer or council id currently holding it
    reply       TEXT,
    answered_by TEXT,
    created_at  REAL NOT NULL,
    picked_at   REAL,
    answered_at REAL
);
CREATE TABLE IF NOT EXISTS council (
    id          TEXT PRIMARY KEY,
    project     TEXT NOT NULL REFERENCES projects(name),
    role        TEXT NOT NULL,
    tier        TEXT NOT NULL,           -- routine | senior | elder
    model       TEXT NOT NULL,
    brief       TEXT NOT NULL,
    justification TEXT NOT NULL DEFAULT '',
    ticket_id   TEXT,
    summoned_by TEXT,
    status      TEXT NOT NULL DEFAULT 'pending', -- pending | running | done | failed
    report      TEXT,
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tickets_queue ON tickets(project, status, lane, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient);
"""


# Schema migrations, applied in order and tracked in PRAGMA user_version. SCHEMA above is version 1
# (idempotent, so databases created before migrations existed adopt it cleanly). Never edit a
# released migration; append a new one.
MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (2, "tickets are closed, never deleted: add closed_at; index tickets by project", [
        "ALTER TABLE tickets ADD COLUMN closed_at REAL",
        "UPDATE tickets SET closed_at = COALESCE(answered_at, picked_at, created_at) "
        "WHERE status IN ('answered', 'cancelled')",
        "CREATE INDEX IF NOT EXISTS idx_tickets_closed ON tickets(closed_at)",
        "CREATE INDEX IF NOT EXISTS idx_tickets_project ON tickets(project)",
    ]),
    (3, "per-person invite tokens that expire", [
        """CREATE TABLE invites (
            name         TEXT PRIMARY KEY,
            token_sha256 TEXT NOT NULL UNIQUE,
            created_at   REAL NOT NULL,
            expires_at   REAL NOT NULL,
            revoked_at   REAL
        )""",
    ]),
]
SCHEMA_VERSION = max([1, *(v for v, _, _ in MIGRATIONS)])

# Closed tickets older than this are purged (hub config "retention_days" overrides).
DEFAULT_RETENTION_DAYS = 365


def _short_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class NotFound(LookupError):
    pass


class Conflict(RuntimeError):
    pass


class Forbidden(PermissionError):
    pass


class Store:
    def __init__(self, path: str | Path, tiers: dict[str, str] | None = None):
        self.tiers = {**DEFAULT_TIERS, **(tiers or {})}
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"database is schema v{version}, newer than this code (v{SCHEMA_VERSION}); upgrade")
            if version < 1:
                self._db.executescript(SCHEMA)
                self._db.execute("PRAGMA user_version = 1")
                version = 1
            for v, _desc, statements in MIGRATIONS:
                if v <= version:
                    continue
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    for stmt in statements:
                        self._db.execute(stmt)
                    self._db.execute(f"PRAGMA user_version = {v}")
                    self._db.execute("COMMIT")
                except Exception:
                    self._db.execute("ROLLBACK")
                    raise
                version = v

    @property
    def schema_version(self) -> int:
        return self._q("PRAGMA user_version")[0]["user_version"]

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

    # -- projects ----------------------------------------------------------
    def add_project(self, name: str, path: str, description: str = "", organizers: int = 1) -> dict[str, Any]:
        now = time.time()
        self._x(
            """INSERT INTO projects (name, path, description, organizers, created_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET path=excluded.path, description=excluded.description,
                   organizers=excluded.organizers""",
            (name, path, description, organizers, now),
        )
        self._x("INSERT OR IGNORE INTO ledger (project, summary, updated_at) VALUES (?, '', ?)", (name, now))
        return self.get_project(name)

    def get_project(self, name: str) -> dict[str, Any]:
        rows = self._q("SELECT * FROM projects WHERE name=?", (name,))
        if not rows:
            raise NotFound(f"unknown project {name!r}; the user needs to run `mmi project add {name} --path ...`")
        return rows[0]

    def list_projects(self) -> list[dict[str, Any]]:
        projects = self._q("SELECT * FROM projects ORDER BY name")
        for p in projects:
            p["queued"] = self._q(
                "SELECT COUNT(*) n FROM tickets WHERE project=? AND status='queued'", (p["name"],)
            )[0]["n"]
        return projects

    def project_rows(self, name: str) -> dict[str, int]:
        """How many rows each table holds for a project (what delete_project would remove)."""
        q = lambda sql: self._q(sql, (name,))[0]["n"]  # noqa: E731
        return {
            "tickets": q("SELECT COUNT(*) n FROM tickets WHERE project=?"),
            "tasks": q("SELECT COUNT(*) n FROM tasks WHERE project=?"),
            "task_notes": q("SELECT COUNT(*) n FROM task_notes WHERE task_id IN (SELECT id FROM tasks WHERE project=?)"),
            "council": q("SELECT COUNT(*) n FROM council WHERE project=?"),
            "ledger_log": q("SELECT COUNT(*) n FROM ledger_log WHERE project=?"),
            "hub_agents": q("SELECT COUNT(*) n FROM agents WHERE project=? AND kind IN ('organizer','council')"),
        }

    def delete_project(self, name: str) -> dict[str, int]:
        """Permanently delete a project and every row tagged with it, in one transaction. Workers that were
        on the project keep their agent records (they're per-machine identities), minus the project tag."""
        self.get_project(name)
        counts = self.project_rows(name)
        statements = [
            "DELETE FROM task_notes WHERE task_id IN (SELECT id FROM tasks WHERE project=?)",
            "DELETE FROM tasks WHERE project=?",
            "DELETE FROM tickets WHERE project=?",
            "DELETE FROM council WHERE project=?",
            "DELETE FROM ledger_log WHERE project=?",
            "DELETE FROM ledger WHERE project=?",
            "DELETE FROM reads WHERE agent_id IN (SELECT id FROM agents WHERE project=? AND kind IN ('organizer','council'))",
            "DELETE FROM agents WHERE project=? AND kind IN ('organizer','council')",
            "UPDATE agents SET project=NULL WHERE project=?",
            "DELETE FROM projects WHERE name=?",
        ]
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for stmt in statements:
                    self._db.execute(stmt, (name,))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return counts

    def purge_closed_tickets(self, older_than_days: float = DEFAULT_RETENTION_DAYS) -> int:
        """Delete tickets that were closed more than `older_than_days` ago. Open tickets are never purged."""
        cutoff = time.time() - older_than_days * 86400
        return self._x("DELETE FROM tickets WHERE closed_at IS NOT NULL AND closed_at < ?", (cutoff,)).rowcount

    # -- ledger ------------------------------------------------------------
    def get_ledger(self, project: str, log_limit: int = 30) -> dict[str, Any]:
        self.get_project(project)
        led = self._q("SELECT * FROM ledger WHERE project=?", (project,))[0]
        led["log"] = list(reversed(self._q(
            "SELECT author, kind, body, created_at FROM ledger_log WHERE project=? ORDER BY id DESC LIMIT ?",
            (project, log_limit),
        )))
        return led

    def update_ledger(
        self, project: str, author: str | None, summary: str | None = None, entry: str | None = None,
        kind: str = "note",
    ) -> dict[str, Any]:
        self.get_project(project)
        now = time.time()
        if summary is not None:
            self._x("UPDATE ledger SET summary=?, updated_by=?, updated_at=? WHERE project=?",
                    (summary, author, now, project))
        if entry:
            self._x("INSERT INTO ledger_log (project, author, kind, body, created_at) VALUES (?, ?, ?, ?, ?)",
                    (project, author, kind, entry, now))
        return self.get_ledger(project, log_limit=5)

    # -- tickets (the organizer's queue) --------------------------------------
    def _require_kind(self, agent_id: str, kind: str, project: str | None = None) -> dict[str, Any]:
        agent = self.get_agent(agent_id)
        if agent["kind"] != kind or (project and agent["project"] != project):
            raise Forbidden(f"{agent_id} is not the {kind} for {project or 'this project'}")
        return agent

    def _position(self, ticket: dict[str, Any]) -> int | None:
        if ticket["status"] != "queued":
            return None
        return self._q(
            """SELECT COUNT(*) n FROM tickets WHERE project=? AND status='queued'
               AND (lane < ? OR (lane = ? AND created_at < ?))""",
            (ticket["project"], ticket["lane"], ticket["lane"], ticket["created_at"]),
        )[0]["n"] + 1

    def enqueue(
        self, project: str, asker: str, body: str, kind: str = "question", blocking: bool = False,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        self.get_project(project)
        self.get_agent(asker)
        if kind not in TICKET_KINDS:
            raise ValueError(f"kind must be one of {TICKET_KINDS}")
        lane = 0 if kind == "council_report" else 1
        tid = _short_id("q")
        self._x(
            """INSERT INTO tickets (id, project, asker, kind, body, blocking, task_id, lane, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (tid, project, asker, kind, body, int(blocking), task_id, lane, time.time()),
        )
        return self.get_ticket(tid)

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        rows = self._q("SELECT * FROM tickets WHERE id=?", (ticket_id,))
        if not rows:
            raise NotFound(f"unknown ticket {ticket_id!r}")
        t = rows[0]
        t["blocking"] = bool(t["blocking"])
        t["closed"] = t["closed_at"] is not None
        t["position"] = self._position(t)
        return t

    def list_tickets(self, project: str | None = None, open_only: bool = True) -> list[dict[str, Any]]:
        sql, args = "SELECT id FROM tickets WHERE 1=1", []
        if project:
            sql += " AND project=?"
            args.append(project)
        if open_only:
            sql += " AND closed_at IS NULL"
        sql += " ORDER BY lane, created_at"
        return [self.get_ticket(r["id"]) for r in self._q(sql, tuple(args))]

    def next_ticket(self, project: str, organizer_id: str) -> dict[str, Any] | None:
        """Organizer takes the next ticket in line. Atomic, so several organizers can share a queue."""
        self._require_kind(organizer_id, "organizer", project)
        while True:
            rows = self._q(
                "SELECT id FROM tickets WHERE project=? AND status='queued' ORDER BY lane, created_at LIMIT 1",
                (project,),
            )
            if not rows:
                return None
            cur = self._x(
                "UPDATE tickets SET status='with_organizer', handler=?, picked_at=? WHERE id=? AND status='queued'",
                (organizer_id, time.time(), rows[0]["id"]),
            )
            if cur.rowcount:
                return self.get_ticket(rows[0]["id"])

    def held_by(self, handler: str) -> list[dict[str, Any]]:
        return [self.get_ticket(r["id"]) for r in self._q(
            "SELECT id FROM tickets WHERE handler=? AND status IN ('with_organizer','delegated') ORDER BY picked_at",
            (handler,),
        )]

    def answer_ticket(self, ticket_id: str, by: str, reply: str) -> dict[str, Any]:
        t = self.get_ticket(ticket_id)
        agent = self.get_agent(by)
        is_handler = t["handler"] == by
        is_project_organizer = agent["kind"] == "organizer" and agent["project"] == t["project"]
        if t["status"] not in ("with_organizer", "delegated") or not (is_handler or is_project_organizer):
            raise Forbidden(f"{by} can't answer {ticket_id} (status {t['status']}, handler {t['handler']})")
        self._x(
            "UPDATE tickets SET status='answered', reply=?, answered_by=?, answered_at=?, closed_at=? WHERE id=?",
            (reply, by, now := time.time(), now, ticket_id),
        )
        return self.get_ticket(ticket_id)

    def requeue_ticket(self, ticket_id: str) -> dict[str, Any]:
        """Put a held ticket back in line (e.g. its organizer died)."""
        self._x("UPDATE tickets SET status='queued', handler=NULL, picked_at=NULL WHERE id=? "
                "AND status IN ('with_organizer','delegated')", (ticket_id,))
        return self.get_ticket(ticket_id)

    def cancel_ticket(self, ticket_id: str, by: str) -> dict[str, Any]:
        t = self.get_ticket(ticket_id)
        if t["asker"] != by:
            raise Forbidden("only the asker can cancel a ticket")
        self._x("UPDATE tickets SET status='cancelled', closed_at=? WHERE id=? AND closed_at IS NULL",
                (time.time(), ticket_id))
        return self.get_ticket(ticket_id)

    # -- council -----------------------------------------------------------
    def summon_council(
        self, project: str, organizer_id: str, role: str, tier: str, brief: str,
        justification: str = "", ticket_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_kind(organizer_id, "organizer", project)
        if tier not in self.tiers:
            raise ValueError(f"tier must be one of {tuple(self.tiers)}")
        if tier != "routine" and len(justification.strip()) < 20:
            raise ValueError(f"a {tier} council member needs a real justification (why a cheaper tier won't do)")
        if tier == "elder":
            busy = self._q("SELECT id FROM council WHERE project=? AND tier='elder' AND status IN ('pending','running')",
                           (project,))
            if busy:
                raise Conflict(f"an elder is already sitting on this project ({busy[0]['id']}); wait for its report")
        if ticket_id:
            t = self.get_ticket(ticket_id)
            if t["project"] != project or t["status"] != "with_organizer":
                raise Conflict(f"ticket {ticket_id} isn't currently held by an organizer of {project}")
        cid = _short_id("cn")
        now = time.time()
        self._x(
            """INSERT INTO council (id, project, role, tier, model, brief, justification, ticket_id, summoned_by,
                                    created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cid, project, role, tier, self.tiers[tier], brief, justification, ticket_id, organizer_id, now),
        )
        self.register(f"council:{role}", "hub", "council", project=project, status=f"{tier} - {role}", agent_id=cid)
        if ticket_id:
            self._x("UPDATE tickets SET status='delegated', handler=? WHERE id=?", (cid, ticket_id))
        self.update_ledger(project, organizer_id, entry=f"summoned {tier} council '{role}' ({cid}): {brief[:300]}"
                           + (f" | why: {justification[:300]}" if justification else ""), kind="council")
        return self.get_council(cid)

    def get_council(self, council_id: str) -> dict[str, Any]:
        rows = self._q("SELECT * FROM council WHERE id=?", (council_id,))
        if not rows:
            raise NotFound(f"unknown council member {council_id!r}")
        return rows[0]

    def list_council(self, project: str | None = None, active_only: bool = True) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM council WHERE 1=1", []
        if project:
            sql += " AND project=?"
            args.append(project)
        if active_only:
            sql += " AND status IN ('pending','running')"
        return self._q(sql + " ORDER BY created_at", tuple(args))

    def set_council_status(self, council_id: str, status: str) -> dict[str, Any]:
        col = {"running": "started_at", "done": "finished_at", "failed": "finished_at"}.get(status)
        sets = f"status=?{', ' + col + '=?' if col else ''}"
        self._x(f"UPDATE council SET {sets} WHERE id=?", (status, time.time(), council_id) if col else (status, council_id))
        return self.get_council(council_id)

    def council_report(self, council_id: str, report: str) -> dict[str, Any]:
        c = self.get_council(council_id)
        if c["status"] in ("done", "failed"):
            raise Conflict(f"{council_id} already reported")
        self._x("UPDATE council SET status='done', report=?, finished_at=? WHERE id=?",
                (report, time.time(), council_id))
        # A delegated ticket the council member never answered goes back to the organizer.
        if c["ticket_id"]:
            t = self.get_ticket(c["ticket_id"])
            if t["status"] == "delegated" and t["handler"] == council_id:
                self.requeue_ticket(t["id"])
        self.update_ledger(c["project"], council_id, entry=f"council '{c['role']}' reported: {report[:500]}",
                           kind="council")
        return self.enqueue(c["project"], council_id, report, kind="council_report", task_id=c["ticket_id"])

    # -- invites (time-limited access for other people) ---------------------
    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def create_invite(self, name: str, days: float) -> dict[str, Any]:
        """Issue (or re-issue) a token for `name` that stops working after `days`. Only the hash is stored,
        so the token is shown once."""
        if days <= 0:
            raise ValueError("days must be positive")
        token = "mmi_" + secrets.token_urlsafe(24)
        now = time.time()
        self._x(
            """INSERT INTO invites (name, token_sha256, created_at, expires_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET token_sha256=excluded.token_sha256, created_at=excluded.created_at,
                   expires_at=excluded.expires_at, revoked_at=NULL""",
            (name, self._hash(token), now, now + days * 86400),
        )
        return {**self._invite(name), "token": token}

    def _invite(self, name: str) -> dict[str, Any]:
        rows = self._q("SELECT name, created_at, expires_at, revoked_at FROM invites WHERE name=?", (name,))
        if not rows:
            raise NotFound(f"no invite for {name!r}")
        inv = rows[0]
        inv["active"] = inv["revoked_at"] is None and inv["expires_at"] > time.time()
        return inv

    def list_invites(self) -> list[dict[str, Any]]:
        return [self._invite(r["name"]) for r in self._q("SELECT name FROM invites ORDER BY expires_at")]

    def revoke_invite(self, name: str) -> dict[str, Any]:
        self._invite(name)
        self._x("UPDATE invites SET revoked_at=? WHERE name=? AND revoked_at IS NULL", (time.time(), name))
        return self._invite(name)

    def check_invite(self, token: str) -> str | None:
        """The invitee's name if `token` is a live invite token. Read-only (no disk write per request)."""
        rows = self._q("SELECT name FROM invites WHERE token_sha256=? AND revoked_at IS NULL AND expires_at > ?",
                       (self._hash(token), time.time()))
        return rows[0]["name"] if rows else None

    # -- overview ----------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "agents": self.list_agents(),
            "tasks": self.list_tasks(),
            "recent_messages": self._q("SELECT * FROM messages ORDER BY id DESC LIMIT 20"),
        }

    def dump(self) -> str:
        return json.dumps(self.snapshot(), indent=2, default=str)
