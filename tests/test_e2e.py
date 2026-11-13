"""Spin up a real hub (no tmux) and play every role over real MCP: worker, organizer, council, human CLI."""

import asyncio
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

TOKEN = "test-token"


@pytest.fixture
def hub(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "MMI_TOKEN": TOKEN, "MMI_CONFIG": str(tmp_path / "cfg.json")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "memyselfandi.cli", "serve", "--host", "0.0.0.0", "--port", str(port),
         "--db", str(tmp_path / "hub.db"), "--no-supervise"],
        env=env,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(url + "/health", timeout=0.2)
            break
        except Exception:
            time.sleep(0.1)
    yield url, {**env, "MMI_URL": url, "MMI_MACHINE": "laptop"}
    proc.terminate()
    proc.wait()


class Agent:
    """One MCP session, like a Claude Code / Codex agent would hold."""

    def __init__(self, url):
        self.url = url

    async def __aenter__(self):
        self.http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"})
        self.client = Client(streamable_http_client(self.url + "/mcp", http_client=self.http))
        await self.client.__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self.client.__aexit__(*exc)
        await self.http.aclose()

    async def __call__(self, tool, **args):
        r = await self.client.call_tool(tool, args)
        if r.is_error:
            raise RuntimeError(r.content[0].text if r.content else "tool error")
        sc = r.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc


def mmi(env, *args):
    r = subprocess.run([sys.executable, "-m", "memyselfandi.cli", *args], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_unauthorized_rejected(hub):
    url, _ = hub
    req = urllib.request.Request(url + "/api/tool/list_agents", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 401


def test_worker_organizer_council_flow(hub, tmp_path):
    url, env = hub
    mmi(env, "project", "add", "app", "--path", str(tmp_path), "-d", "demo app")
    # Without tmux the supervisor is off; register the organizer identity the way it would.
    req = urllib.request.Request(
        url + "/api/admin/add_project", method="POST",
        data=b'{"name": "app", "path": "/tmp", "description": "demo app"}',
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    urllib.request.urlopen(req)

    async def run():
        from memyselfandi.store import Store
        Store(tmp_path / "hub.db").register("organizer #1", "hub", "organizer", project="app",
                                            agent_id="org-app-1")
        async with Agent(url) as worker, Agent(url) as org, Agent(url) as council:
            # worker on the laptop checks in and waits in line
            w = (await worker("register", name="laptop-claude", machine="laptop", project="app"))["id"]
            with pytest.raises(RuntimeError, match="assigned by the hub"):
                await worker("register", name="sneaky", machine="x", kind="organizer")
            t = await worker("ask_organizer", agent_id=w, project="app", kind="checkin",
                             body="ready for work", blocking=True)
            assert t["position"] == 1
            waiting = asyncio.create_task(worker("wait_for_reply", ticket_id=t["id"], timeout_s=20))

            # organizer takes it, briefs, assigns a task
            nxt = await org("organizer_next", agent_id="org-app-1", project="app")
            assert nxt["ticket"]["id"] == t["id"] and nxt["ticket"]["asker_info"]["machine"] == "laptop"
            task = await org("create_task", title="add login form", agent_id="org-app-1", project="app",
                             assignee=w)
            await org("answer_ticket", agent_id="org-app-1", ticket_id=t["id"],
                      reply=f"Do {task['id']}: add the login form.")
            await org("update_ledger", agent_id="org-app-1", project="app",
                      summary="Login form in progress (laptop-claude).", entry="assigned login form",
                      kind="decision")
            got = await waiting
            assert got["status"] == "answered" and task["id"] in got["reply"]

            # a non-blocking question gets delegated to a routine council member
            q = await worker("ask_organizer", agent_id=w, project="app", body="which CSS lib?")
            nxt = await org("organizer_next", agent_id="org-app-1", project="app")
            c = await org("summon_council", agent_id="org-app-1", project="app", role="frontend",
                          tier="routine", brief="pick a CSS lib", ticket_id=q["id"])
            assert c["model"] == "claude-sonnet-5-5"
            assert (await worker("check_ticket", ticket_id=q["id"]))["status"] == "delegated"
            await council("answer_ticket", agent_id=c["id"], ticket_id=q["id"], reply="use the existing one")
            rep = await council("council_report", agent_id=c["id"], report="answered CSS question")
            assert rep["kind"] == "council_report"
            assert (await worker("check_ticket", ticket_id=q["id"]))["reply"] == "use the existing one"
            nxt = await org("organizer_next", agent_id="org-app-1", project="app")
            assert nxt["ticket"]["kind"] == "council_report"
            await org("answer_ticket", agent_id="org-app-1", ticket_id=nxt["ticket"]["id"], reply="ack")
            with pytest.raises(RuntimeError, match="can't answer"):  # reasons reach the agent
                await worker("answer_ticket", agent_id=w, ticket_id=q["id"], reply="x")
            assert (await org("organizer_next", agent_id="org-app-1", project="app"))["ticket"] is None

    asyncio.run(run())

    # the human sees it all from the CLI
    assert "Login form in progress" in mmi(env, "ledger", "app")
    assert "app" in mmi(env, "status")
    assert "frontend" in mmi(env, "council", "app", "-a")
    assert "(queue empty)" in mmi(env, "queue", "app")
