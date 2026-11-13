import json
import subprocess

import pytest

from memyselfandi import claude_hooks
from memyselfandi.claude_hooks import connect, resolve_project

PROJECTS = [{"name": "myapp", "path": "/srv/hub/myapp"}, {"name": "api", "path": "/srv/hub/api"}]


def test_resolve_project(tmp_path):
    assert resolve_project("/srv/hub/myapp/src", PROJECTS, {}) == "myapp"          # on the hub machine
    repo = tmp_path / "code" / "api"
    (repo / "pkg").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    assert resolve_project(str(repo / "pkg"), PROJECTS, {}) == "api"               # clone with same name
    other = tmp_path / "work" / "my-app-checkout"
    other.mkdir(parents=True)
    assert resolve_project(str(other), PROJECTS, {}) is None
    assert resolve_project(str(other), PROJECTS, {str(tmp_path / "work"): "myapp"}) == "myapp"  # mapping


def fake_call():
    calls = []

    def call(cfg, tool, **args):
        calls.append((tool, args))
        return PROJECTS if tool == "list_projects" else {"id": args.get("agent_id")}
    return call, calls


def test_connect_registers_and_returns_protocol():
    call, calls = fake_call()
    info = connect({"machine": "laptop"}, call, "/srv/hub/api/x")
    assert info["project"] == "api" and info["agent_id"].startswith("cc-laptop-")
    assert f'agent_id="{info["agent_id"]}"' in info["instructions"] and "checkin" in info["instructions"]
    assert calls[-1][0] == "register"
    assert connect({"machine": "laptop"}, call, "/elsewhere", project="myapp")["project"] == "myapp"
    with pytest.raises(LookupError, match="(?i)projects: api, myapp"):
        connect({"machine": "laptop"}, call, "/elsewhere")


def test_install_adds_skill_and_removes_old_auto_hook(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    old_hook = {"type": "command", "command": f"python -m {claude_hooks.HOOK_MARKER}"}
    settings.write_text(json.dumps({"hooks": {"SessionStart": [
        {"matcher": "*", "hooks": [{"type": "command", "command": "bash other.sh"}]},
        {"matcher": "*", "hooks": [old_hook]}]}}))
    monkeypatch.setattr(claude_hooks, "CLAUDE_SETTINGS", settings)
    monkeypatch.setattr(claude_hooks, "SKILL_DIR", tmp_path / "skills" / "memyselfandi")
    monkeypatch.setattr(claude_hooks.shutil, "which", lambda _: "/bin/true")
    monkeypatch.setattr(claude_hooks.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0))
    cfg = {"url": "http://hub:7377", "token": None}
    claude_hooks.install(cfg)
    claude_hooks.install(cfg)
    d = json.loads(settings.read_text())
    assert [h["command"] for e in d["hooks"]["SessionStart"] for h in e["hooks"]] == ["bash other.sh"]
    assert d["permissions"]["allow"] == ["mcp__memyselfandi"]
    skill = (tmp_path / "skills" / "memyselfandi" / "SKILL.md").read_text()
    assert skill.startswith("---\nname: memyselfandi\n") and "-m memyselfandi.cli connect" in skill
    assert skill == claude_hooks.render_skill() and "{command}" not in skill
    claude_hooks.uninstall()
    assert not (tmp_path / "skills" / "memyselfandi").exists()
    assert "mcp__memyselfandi" not in json.loads(settings.read_text())["permissions"]["allow"]
