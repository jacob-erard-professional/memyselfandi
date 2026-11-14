import os
import subprocess
import sys

import pytest

from memyselfandi.projects import _normalize, provision_project, remove_folders
from memyselfandi.store import Store
from memyselfandi.supervisor import Supervisor
from test_e2e import hub  # noqa: F401
from test_organizer import FakeRunner


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture
def remote(tmp_path):
    """A bare 'GitHub' repo plus a laptop-style clone of it."""
    bare = tmp_path / "remote" / "myapp.git"
    git("init", "-q", "--bare", str(bare))
    laptop = tmp_path / "laptop" / "myapp"
    git("clone", "-q", str(bare), str(laptop))
    (laptop / "README.md").write_text("# myapp\n")
    git("add", ".", cwd=laptop)
    git("commit", "-qm", "init", cwd=laptop)
    git("push", "-q", "origin", "HEAD", cwd=laptop)
    return bare, laptop


def test_normalize_remotes():
    a = _normalize("https://github.com/Me/App.git")
    assert a == _normalize("git@github.com:me/app") == _normalize("ssh://git@github.com/me/app.git/")


def test_provision_attaches_folders_on_any_machine_and_optionally_clones(tmp_path, remote):
    bare, laptop = remote
    s = Store(":memory:", hub_machine="pc")
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    p = provision_project(s, "proj", tmp_path / "hubroot", folders=[str(a)])
    assert p["hub_folders"] == [str(a.resolve())] and p["path"] == str(a.resolve())
    # adding more folders, including on another machine, keeps one project (and its settings)
    provision_project(s, "proj", tmp_path / "hubroot", folders=[str(b)], description="d", trust=True)
    p = provision_project(s, "proj", tmp_path / "hubroot", folders=["/Users/me/proj-ui"], machine="mac")
    assert {(f["machine"], f["path"]) for f in p["folders"]} == {
        ("pc", str(a.resolve())), ("pc", str(b.resolve())), ("mac", "/Users/me/proj-ui")}
    assert p["description"] == "d" and p["auto_trust"] == 1 and len(p["hub_folders"]) == 2
    # a project with no folders yet is fine
    assert provision_project(s, "empty", tmp_path / "hubroot")["folders"] == []
    # --git clones onto the hub as one more folder; re-adding reuses the clone
    p = provision_project(s, "myapp", tmp_path / "hubroot", git_url=str(bare))
    assert p["cloned"] and (tmp_path / "hubroot" / "myapp" / "README.md").exists()
    assert provision_project(s, "myapp", tmp_path / "hubroot", git_url=str(bare))["cloned"] is None
    other = tmp_path / "remote" / "other.git"
    git("init", "-q", "--bare", str(other))
    with pytest.raises(ValueError, match="isn't a clone"):
        provision_project(s, "myapp", tmp_path / "hubroot", git_url=str(other))
    with pytest.raises(ValueError, match="doesn't exist on the hub"):
        provision_project(s, "nope", tmp_path / "hubroot", folders=["/does/not/exist"])
    with pytest.raises(ValueError, match="project name"):
        provision_project(s, "bad name!", tmp_path / "hubroot", folders=[str(a)])
    with pytest.raises(ValueError, match="couldn't clone"):
        provision_project(s, "ghost", tmp_path / "hubroot", git_url=str(tmp_path / "missing.git"))
    assert not (tmp_path / "hubroot" / "ghost").exists()
    # removing folders
    p = remove_folders(s, "proj", [str(a.resolve())])
    assert p["removed"] == [str(a.resolve())] and p["path"] == str(b.resolve())


def test_auto_trust_only_with_consent(tmp_path):
    s = Store(":memory:")
    s.add_project("trusted", str(tmp_path), auto_trust=True)
    s.add_project("cautious", str(tmp_path))
    r = FakeRunner()
    sup = Supervisor(s, "http://x", None, tmp_path / "run", runner=r)
    sup.tick(now=1000)
    r.screens = {"org-trusted-1": "Yes, I trust this folder", "org-cautious-1": "Yes, I trust this folder"}
    sup.tick(now=1010)
    assert r.pressed == [("org-trusted-1", ("Down", "Enter"))]
    assert s.get_agent("org-cautious-1")["status"].startswith("BLOCKED")


def test_project_add_cli_uses_the_current_folder(hub, tmp_path):  # noqa: F811
    url, env = hub
    folder = tmp_path / "plain-folder"   # not a git repo: doesn't matter
    folder.mkdir()
    run = lambda *a, cwd=None: subprocess.run([sys.executable, "-m", "memyselfandi.cli", *a], cwd=cwd, env=env,  # noqa: E731
                                              capture_output=True, text=True)
    r = run("project", "add", "ww", "--trust", cwd=folder)
    assert r.returncode == 0, r.stderr
    # the CLI here is machine "laptop", not the hub: the folder is recorded as the laptop's
    assert "project ww" in r.stdout and f"laptop: {folder}" in r.stdout and "no folders on the hub" in r.stdout
    assert str(folder) in run("project", "show", "ww").stdout
    r = run("project", "remove-folder", "ww", cwd=folder)
    assert r.returncode == 0 and "removed" in r.stdout


def test_supervisor_scopes_reads_to_hub_folders_and_restarts_on_change(tmp_path):
    s = Store(":memory:", hub_machine="pc")
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    s.add_project("ww", str(a))
    s.add_folder("ww", "/Users/me/ww", "mac")
    r = FakeRunner()
    sup = Supervisor(s, "http://x", None, tmp_path / "run", runner=r)
    sup.tick(now=1000)
    argv = r.windows["org-ww-1"]
    tools = argv[argv.index("--allowedTools") + 1]
    assert tools == f"mcp__memyselfandi,Read(/{a.resolve()}/**)"   # not the mac folder
    system = (tmp_path / "run" / "org-ww-1.system.md").read_text()
    assert str(a) in system and "/Users/me/ww` on mac" in system
    s.add_folder("ww", str(b))
    sup.tick(now=1100)
    tools = r.windows["org-ww-1"][r.windows["org-ww-1"].index("--allowedTools") + 1]
    assert tools.count("Read(") == 2 and r.killed.count("org-ww-1") == 1


def test_project_without_hub_folders_runs_in_its_own_empty_home(tmp_path):
    s = Store(":memory:", hub_machine="pc")
    s.add_project("remote-only", "/Users/me/x", machine="mac")
    r = FakeRunner()
    sup = Supervisor(s, "http://x", None, tmp_path / "run", runner=r)
    sup.tick(now=1000)
    argv = r.windows["org-remote-only-1"]
    assert argv[argv.index("--allowedTools") + 1] == "mcp__memyselfandi"
    assert r.cwds["org-remote-only-1"] == str(tmp_path / "run" / "homes" / "remote-only")
