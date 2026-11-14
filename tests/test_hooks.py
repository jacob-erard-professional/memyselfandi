import json
import subprocess

import pytest

from memyselfandi import claude_hooks
from memyselfandi.claude_hooks import connect, resolve_project

PROJECTS = [
    {"name": "myapp", "folders": [{"machine": "pc", "path": "/srv/hub/myapp"}]},
    {"name": "api", "folders": [{"machine": "pc", "path": "/srv/hub/api"}]},
    {"name": "wellwatch", "folders": [{"machine": "pc", "path": "/home/me/WellWatch"},
                                      {"machine": "mac", "path": "/Users/me/code/ww"},
                                      {"machine": "mac", "path": "/Users/me/notes/wellwatch"}]},
    {"name": "ww-ui", "folders": [{"machine": "mac", "path": "/Users/me/code/ww/ui"}]},
]


def test_resolve_project_by_folder_on_this_machine():
    assert resolve_project("/srv/hub/myapp/src", PROJECTS, "pc") == "myapp"
    assert resolve_project("/srv/hub/myapp", PROJECTS, "pc") == "myapp"
    assert resolve_project("/srv/hub/myapp-old", PROJECTS, "pc") is None          # prefix isn't containment
    assert resolve_project("/srv/hub/myapp/src", PROJECTS, "mac") is None         # that folder is on the pc
    assert resolve_project("/home/me/WellWatch/work/ui", PROJECTS, "pc") == "wellwatch"
    assert resolve_project("/Users/me/notes/wellwatch/x", PROJECTS, "mac") == "wellwatch"  # any folder of it
    assert resolve_project("/Users/me/code/ww/ui/src", PROJECTS, "mac") == "ww-ui"  # deepest folder wins
    assert resolve_project("/Users/me/code/ww/api", PROJECTS, "mac") == "wellwatch"
    assert resolve_project("/tmp/api", PROJECTS, "pc") is None                     # names never match


def fake_call():
    calls = []

    def call(cfg, tool, **args):
        calls.append((tool, args))
        return PROJECTS if tool == "list_projects" else {"id": args.get("agent_id")}
    return call, calls


def test_connect_registers_and_returns_protocol():
    call, calls = fake_call()
    info = connect({"machine": "pc"}, call, "/srv/hub/api/x")
    assert info["project"] == "api" and info["agent_id"].startswith("cc-pc-")
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


def test_profile_gets_its_own_mcp_server_and_skill(monkeypatch):
    monkeypatch.setenv("MMI_PROFILE", "collab")
    assert claude_hooks.server_name() == "memyselfandi-collab"
    assert claude_hooks.skill_dir().name == "memyselfandi-collab"
    skill = claude_hooks.render_skill()
    assert "name: memyselfandi-collab" in skill and "connect --cwd" in skill and "--profile collab" in skill
    monkeypatch.delenv("MMI_PROFILE")
    assert claude_hooks.server_name() == "memyselfandi" and "--profile" not in claude_hooks.render_skill()
