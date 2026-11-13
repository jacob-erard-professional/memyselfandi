import os
import subprocess
import sys

import pytest

from memyselfandi.projects import _normalize, provision_project
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


def test_provision_uses_existing_folder_or_clones(tmp_path, remote):
    bare, laptop = remote
    s = Store(":memory:")
    p = provision_project(s, "here", tmp_path / "hubroot", path=str(laptop))
    assert p["action"] == "using" and p["path"] == str(laptop.resolve())

    # path not on the hub -> clone origin into projects_root/<name>
    p = provision_project(s, "myapp", tmp_path / "hubroot", path="/Users/me/code/myapp", git_url=str(bare))
    assert p["action"] == "cloned" and (tmp_path / "hubroot" / "myapp" / "README.md").exists()
    # adding again reuses the clone
    assert provision_project(s, "myapp", tmp_path / "hubroot", git_url=str(bare))["action"] == "using"
    # a folder that's a clone of something else is refused
    other = tmp_path / "remote" / "other.git"
    git("init", "-q", "--bare", str(other))
    with pytest.raises(ValueError, match="isn't a clone"):
        provision_project(s, "myapp", tmp_path / "hubroot", git_url=str(other))
    with pytest.raises(ValueError, match="doesn't exist on the hub"):
        provision_project(s, "nope", tmp_path / "hubroot", path="/does/not/exist")
    with pytest.raises(ValueError, match="project name"):
        provision_project(s, "bad name!", tmp_path / "hubroot", path=str(laptop))
    with pytest.raises(ValueError, match="couldn't clone"):
        provision_project(s, "ghost", tmp_path / "hubroot", git_url=str(tmp_path / "missing.git"))
    assert not (tmp_path / "hubroot" / "ghost").exists()


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


def test_project_add_from_a_laptop_repo_clones_onto_hub(hub, remote, tmp_path):  # noqa: F811
    url, env = hub
    bare, laptop = remote
    # CLI defaults from inside a repo: name = folder, path = repo root (it's on this 'hub'), origin as git URL.
    # (Cloning when the path isn't on the hub is covered by test_provision_uses_existing_folder_or_clones.)
    r = subprocess.run([sys.executable, "-m", "memyselfandi.cli", "project", "add", "--trust"], cwd=laptop,
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "project myapp" in r.stdout and "existing folder" in r.stdout and "--trust" in r.stdout
    r = subprocess.run([sys.executable, "-m", "memyselfandi.cli", "project", "list"], env=env,
                       capture_output=True, text=True)
    assert "myapp" in r.stdout and str(laptop) in r.stdout
