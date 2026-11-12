"""Spin up a real hub with a token and drive it via MCP (as an agent would) and via the CLI."""

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

TOKEN = "test-token"


@pytest.fixture
def hub(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "MMI_TOKEN": TOKEN, "MMI_CONFIG": str(tmp_path / "cfg.json")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "memyselfandi.cli", "serve", "--host", "0.0.0.0", "--port", str(port),
         "--db", str(tmp_path / "hub.db")],
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


def mcp_call(url, calls):
    async def run():
        out = []
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}) as http:
            async with Client(streamable_http_client(url + "/mcp", http_client=http)) as c:
                names = {t.name for t in (await c.list_tools()).tools}
                assert {"register", "claim_task", "send_message", "get_board"} <= names
                for name, args in calls:
                    r = await c.call_tool(name, args)
                    out.append((r.is_error, r.structured_content, r.content))
        return out

    return asyncio.run(run())


def mmi(env, *args):
    return subprocess.run([sys.executable, "-m", "memyselfandi.cli", *args], env=env, capture_output=True, text=True)


def test_unauthorized_rejected(hub):
    url, _ = hub
    req = urllib.request.Request(url + "/api/tool/list_agents", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 401


def test_agent_over_mcp_and_human_over_cli(hub):
    url, env = hub
    # an agent on the desktop registers and creates a task
    (_, reg, _), (_, task, _) = mcp_call(url, [
        ("register", {"name": "builder", "machine": "desktop", "kind": "claude-code", "project": "api",
                      "agent_id": "ag-desk1"}),
        ("create_task", {"title": "write migration", "agent_id": "ag-desk1", "target_machine": "laptop"}),
    ])
    assert reg["id"] == "ag-desk1"
    task_id = task["id"]

    # the human on the laptop sees it, messages the agent
    r = mmi(env, "status")
    assert r.returncode == 0, r.stderr
    assert "builder" in r.stdout and "write migration" in r.stdout
    assert mmi(env, "send", "ag-desk1", "please", "pick", "up", task_id).returncode == 0

    # a codex agent on the laptop claims it; the desktop agent can't steal it
    results = mcp_call(url, [
        ("register", {"name": "laptop-codex", "machine": "laptop", "kind": "codex", "agent_id": "ag-lap1"}),
        ("claim_task", {"task_id": task_id, "agent_id": "ag-lap1"}),
        ("claim_task", {"task_id": task_id, "agent_id": "ag-desk1"}),
        ("read_inbox", {"agent_id": "ag-desk1"}),
    ])
    assert results[1][1]["assignee"] == "ag-lap1"
    assert results[2][0] is True  # conflict surfaced as a tool error
    inbox = results[3][1]
    inbox = inbox.get("result", inbox) if isinstance(inbox, dict) else inbox
    assert "please pick up" in json.dumps(inbox)

    assert mmi(env, "done", task_id, "-n", "merged").returncode == 0
    assert "write migration" not in mmi(env, "tasks").stdout
    assert "merged" in mmi(env, "show", task_id).stdout
