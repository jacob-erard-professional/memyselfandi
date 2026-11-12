import pytest

from memyselfandi.store import Conflict, NotFound, Store


@pytest.fixture
def store():
    return Store(":memory:")


def test_register_and_board(store):
    pc = store.register("builder", "desktop", "claude-code", project="api")
    lap = store.register("reviewer", "laptop", "codex")
    ids = {a["id"] for a in store.list_agents()}
    assert ids == {pc["id"], lap["id"]}
    assert all(a["online"] for a in store.list_agents())
    # re-register keeps the id and updates fields
    again = store.register("builder", "desktop", "claude-code", project="web", agent_id=pc["id"])
    assert again["id"] == pc["id"] and again["project"] == "web"


def test_claim_is_exclusive(store):
    a = store.register("a", "desktop", "claude-code")["id"]
    b = store.register("b", "laptop", "codex")["id"]
    t = store.create_task("fix login", created_by=a)
    assert store.claim_task(t["id"], a)["assignee"] == a
    assert store.claim_task(t["id"], a)["assignee"] == a  # idempotent for the owner
    with pytest.raises(Conflict):
        store.claim_task(t["id"], b)
    assert store.list_agents()[0]["active_tasks"] or store.list_agents()[1]["active_tasks"]


def test_update_release_and_done(store):
    a = store.register("a", "desktop", "claude-code")["id"]
    t = store.create_task("x")["id"]
    store.claim_task(t, a)
    released = store.update_task(t, a, status="open", note="handing off")
    assert released["assignee"] is None and released["notes"][0]["body"] == "handing off"
    store.claim_task(t, a)
    store.update_task(t, a, status="done", note="shipped")
    assert store.list_tasks() == []
    assert store.list_tasks(include_closed=True)[0]["status"] == "done"
    with pytest.raises(ValueError):
        store.update_task(t, a, status="bogus")


def test_messages_direct_and_broadcast(store):
    a = store.register("a", "desktop", "claude-code")["id"]
    b = store.register("b", "laptop", "codex")["id"]
    store.send_message(a, b, "can you review t-1?")
    store.send_message(a, "*", "deploying in 5")
    inbox_b = store.inbox(b)
    assert [m["body"] for m in inbox_b] == ["can you review t-1?", "deploying in 5"]
    assert store.inbox(b) == []  # marked read
    assert store.inbox(a) == []  # sender doesn't get own broadcast
    with pytest.raises(NotFound):
        store.send_message(a, "ag-nope", "hi")


def test_remove_agent_releases_tasks(store):
    a = store.register("a", "desktop", "claude-code")["id"]
    t = store.create_task("x")["id"]
    store.claim_task(t, a)
    store.remove_agent(a)
    assert store.get_task(t)["status"] == "open"
