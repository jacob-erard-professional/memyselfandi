import sqlite3
import time

from memyselfandi.store import SCHEMA, SCHEMA_VERSION, Store
from memyselfandi.supervisor import Supervisor
from test_organizer import FakeRunner


def test_migrates_a_pre_migration_database(tmp_path):
    db = tmp_path / "old.db"
    old = sqlite3.connect(db)  # a database from before migrations existed (v1 schema, user_version 0)
    old.executescript(SCHEMA)
    old.execute("INSERT INTO projects VALUES ('app', '/tmp', '', 1, 0)")
    old.execute("INSERT INTO agents VALUES ('w', 'w', 'laptop', 'claude-code', 'app', NULL, 0, 0)")
    old.execute("INSERT INTO tickets (id, project, asker, kind, body, status, created_at, answered_at) "
                "VALUES ('q-done', 'app', 'w', 'question', 'x', 'answered', 10, 20)")
    old.execute("INSERT INTO tickets (id, project, asker, kind, body, created_at) "
                "VALUES ('q-open', 'app', 'w', 'question', 'y', 30)")
    old.commit()
    old.close()

    s = Store(db, hub_machine="pc")
    assert s.schema_version == SCHEMA_VERSION
    assert s.get_project("app")["folders"] == [{"machine": "pc", "path": "/tmp"}]  # old path -> hub folder
    assert s.get_ticket("q-done")["closed_at"] == 20 and s.get_ticket("q-done")["closed"]
    assert s.get_ticket("q-open")["closed_at"] is None
    assert [t["id"] for t in s.list_tickets("app")] == ["q-open"]
    assert Store(db).schema_version == SCHEMA_VERSION  # reopening is a no-op


def setup(store):
    store.add_project("app", "/tmp/app")
    store.add_project("other", "/tmp/other")
    store.register("org", "hub", "organizer", project="app", agent_id="org-app-1")
    w = store.register("w", "laptop", "claude-code", project="app")["id"]
    return w


def test_answer_and_cancel_close_tickets_and_nothing_deletes_them():
    s = Store(":memory:")
    w = setup(s)
    a = s.enqueue("app", w, "a")
    b = s.enqueue("app", w, "b")
    s.next_ticket("app", "org-app-1")
    s.answer_ticket(a["id"], "org-app-1", "ok")
    s.cancel_ticket(b["id"], w)
    assert s.get_ticket(a["id"])["closed"] and s.get_ticket(b["id"])["closed"]
    assert s.list_tickets("app") == []
    assert len(s.list_tickets("app", open_only=False)) == 2


def test_purge_only_removes_tickets_closed_over_a_year_ago():
    s = Store(":memory:")
    w = setup(s)
    old, recent, still_open = (s.enqueue("app", w, x) for x in ("old", "recent", "open"))
    for t in (old, recent):
        s.next_ticket("app", "org-app-1")
        s.answer_ticket(t["id"], "org-app-1", "ok")
    s._x("UPDATE tickets SET closed_at=? WHERE id=?", (time.time() - 366 * 86400, old["id"]))
    s._x("UPDATE tickets SET created_at=? WHERE id=?", (time.time() - 400 * 86400, still_open["id"]))
    assert s.purge_closed_tickets(365) == 1
    left = {t["id"] for t in s.list_tickets("app", open_only=False)}
    assert left == {recent["id"], still_open["id"]}


def test_delete_project_removes_only_that_project(tmp_path):
    s = Store(":memory:")
    w = setup(s)
    s.enqueue("app", w, "q")
    s.create_task("t", project="app")
    s.update_task(s.list_tasks(project="app")[0]["id"], w, note="n")
    s.update_ledger("app", "org-app-1", summary="sum", entry="e")
    s.register("org", "hub", "organizer", project="other", agent_id="org-other-1")
    s.enqueue("other", w, "keep me")
    s.create_task("keep", project="other")

    r = FakeRunner()
    sup = Supervisor(s, "http://x", None, tmp_path, runner=r)
    sup.tick(now=1000)
    assert {"org-app-1", "org-other-1"} <= set(r.windows)

    counts = s.delete_project("app")
    assert counts["tickets"] == 1 and counts["tasks"] == 1 and counts["task_notes"] == 1
    assert all(n == 0 for n in s.project_rows("app").values())
    assert [p["name"] for p in s.list_projects()] == ["other"]
    assert len(s.list_tickets("other")) == 1 and len(s.list_tasks(project="other")) == 1
    assert s.get_agent(w)["project"] is None  # the worker survives, untagged

    sup.tick(now=1010)
    assert "org-app-1" not in r.windows and "org-other-1" in r.windows


def test_supervisor_heartbeats_are_throttled(tmp_path):
    s = Store(":memory:")
    setup(s)
    sup = Supervisor(s, "http://x", None, tmp_path, runner=FakeRunner())
    sup.tick(now=1000)
    seen = s.get_agent("org-app-1")["last_seen"]
    for i in range(1, 30):  # ~90s of ticks
        sup.tick(now=1000 + 3 * i)
    assert s.get_agent("org-app-1")["last_seen"] == seen


def test_organizers_overview():
    s = Store(":memory:")
    w = setup(s)
    t = s.enqueue("app", w, "hi")
    s.next_ticket("app", "org-app-1")
    rows = {r["project"]: r for r in s.organizers()}
    assert rows["other"]["organizers"] == [] and rows["other"]["wanted"] == 1
    org = rows["app"]["organizers"][0]
    assert org["id"] == "org-app-1" and org["online"] and org["handling"] == t["id"]
