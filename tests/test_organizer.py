import pytest

from memyselfandi.store import Conflict, Forbidden, Store
from memyselfandi.supervisor import Supervisor


@pytest.fixture
def store():
    s = Store(":memory:")
    s.add_project("app", "/tmp/app", "demo")
    s.register("organizer #1", "hub", "organizer", project="app", agent_id="org-app-1")
    return s


def worker(store, name="w", machine="laptop"):
    return store.register(name, machine, "claude-code", project="app")["id"]


def test_queue_is_fifo_with_council_reports_first(store):
    a, b = worker(store, "a"), worker(store, "b")
    t1 = store.enqueue("app", a, "first")
    t2 = store.enqueue("app", b, "second", blocking=True)
    assert (t1["position"], t2["position"]) == (1, 2)
    store.register("council:x", "hub", "council", project="app", agent_id="cn-x")
    rep = store.enqueue("app", "cn-x", "report", kind="council_report")
    assert rep["position"] == 1 and store.get_ticket(t1["id"])["position"] == 2
    order = [store.next_ticket("app", "org-app-1")["id"] for _ in range(3)]
    assert order == [rep["id"], t1["id"], t2["id"]]
    assert store.next_ticket("app", "org-app-1") is None


def test_only_organizer_takes_and_answers(store):
    a = worker(store)
    t = store.enqueue("app", a, "what next?", kind="checkin")
    with pytest.raises(Forbidden):
        store.next_ticket("app", a)
    got = store.next_ticket("app", "org-app-1")
    with pytest.raises(Forbidden):
        store.answer_ticket(got["id"], a, "self-answer")
    done = store.answer_ticket(got["id"], "org-app-1", "do t-1")
    assert done["status"] == "answered" and done["reply"] == "do t-1" and t["id"] == done["id"]


def test_council_delegation_and_report(store):
    a = worker(store)
    t = store.enqueue("app", a, "how should auth work?", blocking=True)
    store.next_ticket("app", "org-app-1")
    with pytest.raises(ValueError):  # senior needs a justification
        store.summon_council("app", "org-app-1", "auth", "senior", "design auth", ticket_id=t["id"])
    c = store.summon_council("app", "org-app-1", "auth", "senior", "design auth",
                             justification="cross-cutting security design; routine tier is not enough",
                             ticket_id=t["id"])
    assert c["model"] == "claude-opus-5" and store.get_ticket(t["id"])["status"] == "delegated"
    store.answer_ticket(t["id"], c["id"], "use sessions; see t-2")
    rep = store.council_report(c["id"], "decided sessions; created t-2")
    assert rep["kind"] == "council_report" and rep["position"] == 1
    assert store.get_council(c["id"])["status"] == "done"
    assert "council" in {e["kind"] for e in store.get_ledger("app")["log"]}


def test_unanswered_delegation_returns_to_queue(store):
    a = worker(store)
    t = store.enqueue("app", a, "q")
    store.next_ticket("app", "org-app-1")
    c = store.summon_council("app", "org-app-1", "router", "routine", "route it", ticket_id=t["id"])
    store.council_report(c["id"], "couldn't figure it out")
    assert store.get_ticket(t["id"])["status"] == "queued"


def test_one_elder_at_a_time(store):
    why = "re-architecting the whole storage layer; irreversible"
    store.summon_council("app", "org-app-1", "arch", "elder", "rethink storage", justification=why)
    with pytest.raises(Conflict):
        store.summon_council("app", "org-app-1", "arch2", "elder", "again", justification=why)


class FakeRunner:
    def __init__(self):
        self.windows: dict[str, list[str]] = {}
        self.sent: list[tuple[str, str]] = []
        self.screens: dict[str, str] = {}

    def has_window(self, name):
        return name in self.windows

    def launch(self, name, cwd, argv):
        self.windows[name] = argv

    def send(self, name, text):
        self.sent.append((name, text))

    def kill(self, name):
        self.windows.pop(name, None)

    def capture(self, name):
        return self.screens.get(name, "")


def test_supervisor_launches_wakes_and_manages_council(store, tmp_path):
    r = FakeRunner()
    sup = Supervisor(store, "http://127.0.0.1:1", "tok", tmp_path, runner=r, claude_bin="claude")
    sup.tick(now=1000)
    argv = r.windows["org-app-1"]
    assert argv[0] == "claude" and "claude-opus-5-5" in argv and "dontAsk" in argv
    assert "organizer_next" in argv[1]  # positional prompt comes first
    assert '"Bearer tok"' in (tmp_path / "mcp.json").read_text()

    # empty queue -> no nudge; queued ticket after startup grace -> nudge once
    sup.tick(now=1030)
    assert r.sent == []
    a = worker(store)
    store.enqueue("app", a, "hello", kind="checkin")
    sup.tick(now=1060)
    sup.tick(now=1061)
    assert len(r.sent) == 1 and "organizer_next" in r.sent[0][1]

    # organizer dies while holding a ticket -> relaunch + ticket back in line
    t = store.next_ticket("app", "org-app-1")
    r.kill("org-app-1")
    sup.tick(now=1100)
    assert "org-app-1" in r.windows and store.get_ticket(t["id"])["status"] == "queued"

    # council: pending -> launched with tier model; exits without report -> failed + organizer told
    t = store.next_ticket("app", "org-app-1")
    c = store.summon_council("app", "org-app-1", "router", "routine", "route", ticket_id=t["id"])
    sup.tick(now=1200)
    assert "claude-sonnet-5-5" in r.windows[c["id"]]
    assert store.get_council(c["id"])["status"] == "running"
    r.kill(c["id"])
    sup.tick(now=1210)
    assert store.get_council(c["id"])["status"] == "failed"
    kinds = [x["kind"] for x in store.list_tickets("app") if x["status"] == "queued"]
    assert kinds[0] == "council_report" and store.get_ticket(t["id"])["status"] == "queued"


def test_supervisor_flags_trust_prompt_instead_of_answering(store, tmp_path):
    r = FakeRunner()
    sup = Supervisor(store, "http://127.0.0.1:1", None, tmp_path, runner=r)
    sup.tick(now=1000)
    r.screens["org-app-1"] = "Is this a project you trust?  Yes, I trust this folder"
    store.enqueue("app", worker(store), "hi", kind="checkin")
    sup.tick(now=1100)
    assert r.sent == []  # never types into the trust dialog
    assert store.get_agent("org-app-1")["status"].startswith("BLOCKED")
    r.screens["org-app-1"] = ""
    sup.tick(now=1200)
    assert len(r.sent) == 1
