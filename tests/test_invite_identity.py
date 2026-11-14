"""Invite tokens act only as their own agents, and are rate limited. Attacked over both MCP and the REST API."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from memyselfandi.store import Store
from test_e2e import TOKEN
from test_invites import req

LIMITS = {"calls_per_minute": 40, "tickets_per_hour": 3, "ticket_chars": 100, "agents": 2, "messages_per_hour": 3}


@pytest.fixture
def collab(tmp_path):
    """A hub with tight invite limits, a project, its organizer identity, and two invitees."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"invite_limits": LIMITS}))
    env = {**os.environ, "MMI_TOKEN": TOKEN, "MMI_CONFIG": str(cfg)}
    proc = subprocess.Popen([sys.executable, "-m", "memyselfandi.cli", "serve", "--host", "127.0.0.1", "--port",
                             str(port), "--db", str(tmp_path / "hub.db"), "--no-supervise"], env=env)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(url + "/health", timeout=0.2)
            break
        except Exception:
            time.sleep(0.1)
    assert req(url, "/api/admin/add_project", TOKEN, b'{"name": "app"}')[0] == 200
    Store(tmp_path / "hub.db").register("organizer #1", "hub", "organizer", project="app", agent_id="org-app-1")
    tokens = {n: req(url, "/api/admin/create_invite", TOKEN, json.dumps({"name": n, "days": 1}).encode())[1]["token"]
              for n in ("alice", "bob")}
    yield url, tokens
    proc.terminate()
    proc.wait()


def tool(url, token, _tool, **args):
    return req(url, f"/api/tool/{_tool}", token, json.dumps(args).encode())


def test_invitee_can_act_only_as_its_own_agents(collab):
    url, tok = collab
    alice, bob = tok["alice"], tok["bob"]
    code, a = tool(url, alice, "register", name="alice-cc", machine="alice-laptop", project="app")
    assert code == 200
    a = a["id"]
    b = tool(url, bob, "register", name="bob-cc", machine="bob-pc", project="app")[1]["id"]

    # everything organizer/council-only is out of reach: their ids aren't the invitee's
    for tname, args in [
        ("organizer_next", {"agent_id": "org-app-1", "project": "app"}),
        ("update_ledger", {"agent_id": "org-app-1", "project": "app", "summary": "pwned"}),
        ("summon_council", {"agent_id": "org-app-1", "project": "app", "role": "x", "tier": "elder",
                            "brief": "x", "justification": "x" * 40}),
        ("answer_ticket", {"agent_id": "org-app-1", "ticket_id": "q-x", "reply": "x"}),
        ("council_report", {"agent_id": "org-app-1", "report": "x"}),
    ]:
        code, err = tool(url, alice, tname, **args)
        assert code == 403 and "through the organizer" in err["error"], tname
    # nor can alice impersonate bob, or take over his id by re-registering it
    assert tool(url, alice, "ask_organizer", agent_id=b, project="app", body="hi")[0] == 403
    assert tool(url, alice, "read_inbox", agent_id=b)[0] == 403
    assert tool(url, alice, "register", name="x", machine="x", agent_id=b)[0] == 403
    assert tool(url, alice, "register", name="x", machine="x", agent_id="org-app-1")[0] == 403
    # they can't reach the user's agents or the board at all: no messages, tasks, agent/queue/ledger listings
    for tname, args in [
        ("create_task", {"title": "x", "agent_id": a, "project": "app"}),
        ("claim_task", {"task_id": "t-x", "agent_id": a}),
        ("list_agents", {}), ("list_tasks", {}), ("get_board", {}), ("get_ledger", {"project": "app"}),
        ("list_queue", {"project": "app"}), ("list_council", {"project": "app"}),
    ]:
        code, err = tool(url, alice, tname, **args)
        assert code == 403 and "through the organizer" in err["error"], tname
    # as herself, through the organizer: fine
    t = tool(url, alice, "ask_organizer", agent_id=a, project="app", body="what's the status?")
    assert t[0] == 200
    assert tool(url, alice, "check_ticket", ticket_id=t[1]["id"])[0] == 200
    assert tool(url, alice, "register", name="alice-cc", machine="alice-laptop", agent_id=a)[0] == 200
    # ...but not bob's tickets
    tb = tool(url, bob, "ask_organizer", agent_id=b, project="app", body="bob's question")[1]["id"]
    assert tool(url, alice, "check_ticket", ticket_id=tb)[0] == 403
    assert tool(url, alice, "wait_for_reply", ticket_id=tb, timeout_s=1)[0] == 403
    assert tool(url, alice, "cancel_ticket", agent_id=a, ticket_id=tb)[0] == 403
    # the hub token still can do everything
    assert tool(url, TOKEN, "organizer_next", agent_id="org-app-1", project="app")[0] == 200

    # tasks: only ones the organizer assigned to her; status + notes, nothing else
    t_a = tool(url, TOKEN, "create_task", title="for alice", agent_id="org-app-1", project="app", assignee=a)[1]["id"]
    t_other = tool(url, TOKEN, "create_task", title="for someone else", agent_id="org-app-1", project="app")[1]["id"]
    assert tool(url, alice, "get_task", task_id=t_a)[0] == 200
    assert tool(url, alice, "update_task", task_id=t_a, agent_id=a, status="in_progress", note="on it")[0] == 200
    assert tool(url, alice, "update_task", task_id=t_a, agent_id=a, assignee=b)[0] == 403
    assert tool(url, alice, "get_task", task_id=t_other)[0] == 403
    assert tool(url, alice, "update_task", task_id=t_other, agent_id=a, note="x")[0] == 403

    # the organizer messages her directly and the whole project; she sees the unread count on her ticket
    assert tool(url, TOKEN, "send_message", agent_id="org-app-1", to=a, body="you're next")[0] == 200
    assert tool(url, TOKEN, "send_message", agent_id="org-app-1", to="@app", body="API changed")[0] == 200
    assert tool(url, alice, "check_ticket", ticket_id=t[1]["id"])[1]["unread_messages"] == 2
    inbox = tool(url, alice, "read_inbox", agent_id=a)[1]
    assert [m["body"] for m in inbox] == ["you're next", "API changed"]
    assert [m["body"] for m in tool(url, bob, "read_inbox", agent_id=b)[1]] == ["API changed"]
    # agents on the project message each other, labelled by the hub
    names = {x["id"]: x["from"] for x in tool(url, alice, "project_agents", agent_id=a)[1]}
    assert names[b] == "invitee bob" and names["org-app-1"] == "the organizer"
    assert tool(url, alice, "send_message", agent_id=a, to=b, body="I'm on the login form")[0] == 200
    got = tool(url, bob, "read_inbox", agent_id=b)[1]
    assert got[-1]["body"] == "I'm on the login form" and got[-1]["from"] == "invitee alice" \
        and got[-1]["trust"] == "advice"
    assert tool(url, alice, "send_message", agent_id=a, to="org-app-1", body="fyi")[0] == 200
    assert tool(url, TOKEN, "read_inbox", agent_id="org-app-1")[1][-1]["trust"] == "advice"
    # not beyond the project, not broadcasts
    assert req(url, "/api/admin/add_project", TOKEN, b'{"name": "other"}')[0] == 200
    elsewhere = tool(url, TOKEN, "register", name="x", machine="pc", project="other")[1]["id"]
    assert tool(url, alice, "send_message", agent_id=a, to=elsewhere, body="x")[0] == 403
    assert tool(url, alice, "send_message", agent_id=a, to="@app", body="x")[0] == 403
    assert tool(url, alice, "send_message", agent_id=a, to=b, body="x" * 101)[0] == 403
    # 3 messages an hour per invite: she's sent 2
    assert tool(url, alice, "send_message", agent_id=a, to=b, body="third")[0] == 200
    code, err = tool(url, alice, "send_message", agent_id=a, to=b, body="fourth")
    assert code == 403 and "an hour" in err["error"]

    # a worker can't broadcast to the project; only its organizer/council can
    w = tool(url, TOKEN, "register", name="mine", machine="pc", project="app")[1]["id"]
    assert tool(url, TOKEN, "send_message", agent_id=w, to="@app", body="x")[0] == 403

    # agent limit per invite (2): alice has 1, may make one more
    assert tool(url, alice, "register", name="second", machine="m")[0] == 200
    code, err = tool(url, alice, "register", name="third", machine="m")
    assert code == 403 and "limit" in err["error"]

    # snapshot is the whole database: hub token only
    r = urllib.request.Request(url + "/api/snapshot", headers={"Authorization": f"Bearer {alice}"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(r)
    assert e.value.code == 403


def test_invite_limits(collab):
    url, tok = collab
    alice = tok["alice"]
    a = tool(url, alice, "register", name="alice-cc", machine="m", project="app")[1]["id"]
    code, err = tool(url, alice, "ask_organizer", agent_id=a, project="app", body="x" * 101)
    assert code == 403 and "too long" in err["error"]
    for _ in range(3):
        assert tool(url, alice, "ask_organizer", agent_id=a, project="app", body="q")[0] == 200
    code, err = tool(url, alice, "ask_organizer", agent_id=a, project="app", body="q")
    assert code == 403 and "an hour" in err["error"]
    # calls per minute (40): she's used 6; burn the rest
    codes = [tool(url, alice, "list_projects")[0] for _ in range(40)]
    assert 403 in codes and codes.count(200) == 40 - 6
    # bob has his own budget; the hub token has none
    assert tool(url, tok["bob"], "list_projects")[0] == 200
    assert all(tool(url, TOKEN, "list_projects")[0] == 200 for _ in range(50))


def test_same_rules_over_mcp(collab):
    url, tok = collab

    async def run():
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {tok['alice']}"})
        async with Client(streamable_http_client(url + "/mcp", http_client=http)) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert "organizer_next" in names
            schema = next(t for t in (await c.list_tools()).tools if t.name == "register").input_schema
            assert "ctx" not in schema.get("properties", {})          # the injected context isn't a parameter
            r = await c.call_tool("organizer_next", {"agent_id": "org-app-1", "project": "app"})
            assert r.is_error and "through the organizer" in r.content[0].text
            r = await c.call_tool("register", {"name": "alice-cc", "machine": "m", "project": "app"})
            assert not r.is_error
            me = r.structured_content.get("id") or r.structured_content["result"]["id"]
            r = await c.call_tool("ask_organizer", {"agent_id": me, "project": "app", "body": "hello"})
            assert not r.is_error
        await http.aclose()

    asyncio.run(run())
