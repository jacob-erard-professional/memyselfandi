import json
import subprocess

from memyselfandi import claude_hooks
from memyselfandi.claude_hooks import resolve_project, session_start

PROJECTS = [{"name": "myapp", "path": "/srv/hub/myapp"}, {"name": "api", "path": "/srv/hub/api"}]


def test_resolve_project(tmp_path, monkeypatch):
    assert resolve_project("/srv/hub/myapp/src", PROJECTS, {}) == "myapp"          # on the hub machine
    repo = tmp_path / "code" / "api"
    (repo / "pkg").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    assert resolve_project(str(repo / "pkg"), PROJECTS, {}) == "api"               # clone with same name
    other = tmp_path / "work" / "my-app-checkout"
    other.mkdir(parents=True)
    assert resolve_project(str(other), PROJECTS, {}) is None
    assert resolve_project(str(other), PROJECTS, {str(tmp_path / "work"): "myapp"}) == "myapp"  # mapping


def test_session_start(monkeypatch):
    calls = []

    def call(cfg, tool, **args):
        calls.append((tool, args))
        return PROJECTS if tool == "list_projects" else {"id": args.get("agent_id")}

    cfg = {"machine": "laptop", "projects": {}}
    monkeypatch.delenv("MMI_ROLE", raising=False)
    out = json.loads(session_start(json.dumps({"cwd": "/srv/hub/api", "session_id": "abcdef1234",
                                               "source": "startup"}), cfg, call))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert 'agent_id="cc-laptop-abcdef12"' in ctx and 'project="api"' in ctx and "checkin" in ctx
    assert calls[-1][0] == "register" and calls[-1][1]["agent_id"] == "cc-laptop-abcdef12"

    assert session_start(json.dumps({"cwd": "/elsewhere"}), cfg, call) == ""     # not a project: silent
    monkeypatch.setenv("MMI_ROLE", "organizer")
    assert session_start(json.dumps({"cwd": "/srv/hub/api"}), cfg, call) == ""   # organizer: silent


def test_install_is_idempotent_and_keeps_other_hooks(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {"SessionStart": [
        {"matcher": "*", "hooks": [{"type": "command", "command": "bash other.sh"}]}]}}))
    monkeypatch.setattr(claude_hooks, "CLAUDE_SETTINGS", settings)
    cfg = {"url": "http://hub:7377", "token": None}
    monkeypatch.setattr(claude_hooks.shutil, "which", lambda _: "/bin/true")
    monkeypatch.setattr(claude_hooks.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0))
    claude_hooks.install(cfg)
    claude_hooks.install(cfg)
    d = json.loads(settings.read_text())
    cmds = [h["command"] for e in d["hooks"]["SessionStart"] for h in e["hooks"]]
    assert cmds[0] == "bash other.sh" and sum(claude_hooks.HOOK_MARKER in c for c in cmds) == 1
    assert d["permissions"]["allow"] == ["mcp__memyselfandi"]
    claude_hooks.uninstall()
    d = json.loads(settings.read_text())
    assert [h["command"] for e in d["hooks"]["SessionStart"] for h in e["hooks"]] == ["bash other.sh"]
