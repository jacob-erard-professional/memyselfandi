"""`mmi join`: approve-by-code, machine tokens, linking projects from the projects home."""

import json
import os
import subprocess
import sys
import time

from memyselfandi.claude_hooks import resolve_project
from test_e2e import TOKEN, hub  # noqa: F401
from test_invites import req


def test_join_by_code_links_projects_and_gets_a_revocable_token(hub, tmp_path):  # noqa: F811
    url, env = hub
    # the hub has a project whose usual folder name is "WellWatch"
    hub_copy = tmp_path / "pc" / "WellWatch"
    hub_copy.mkdir(parents=True)
    assert req(url, "/api/admin/add_project", TOKEN, json.dumps(
        {"name": "wellwatch", "folders": [str(hub_copy)], "machine": "laptop", "home_dir": "WellWatch"}).encode())[0] == 200
    # a new machine: its own config, no token, a projects home with WellWatch in it
    home = tmp_path / "mac" / "code"
    (home / "WellWatch").mkdir(parents=True)
    cfg = tmp_path / "mac.json"
    new_env = {k: v for k, v in env.items() if k not in ("MMI_TOKEN", "MMI_URL", "MMI_MACHINE")}
    new_env["MMI_CONFIG"] = str(cfg)
    proc = subprocess.Popen([sys.executable, "-m", "memyselfandi.cli", "join", url, "--machine", "macbook",
                             "--projects-home", str(home), "--no-claude", "--no-clone"],
                            env=new_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for _ in range(100):  # wait for it to ask
        code, waiting = req(url, "/api/admin/list_joins", TOKEN)
        if waiting:
            break
        time.sleep(0.1)
    assert waiting[0]["machine"] == "macbook"
    # nobody else can approve: no token, an invite token
    assert req(url, "/api/admin/approve_join", "nope", json.dumps({"code": waiting[0]["code"]}).encode())[0] == 401
    assert req(url, "/api/admin/approve_join", TOKEN, json.dumps({"code": waiting[0]["code"]}).encode())[0] == 200
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    assert waiting[0]["code"] in out and "approved" in out
    assert f"linked: {home / 'WellWatch'}" in out
    mac = json.loads(cfg.read_text())
    assert mac["token"].startswith("mmd_") and mac["machine"] == "macbook"
    # the machine token works, has full access, and shows up; revoking cuts it off
    assert req(url, "/api/tool/list_projects", mac["token"])[0] == 200
    assert req(url, "/api/admin/list_machines", mac["token"])[0] == 200
    _, projects = req(url, "/api/tool/list_projects", mac["token"])
    ww = next(p for p in projects if p["name"] == "wellwatch")
    assert {"machine": "macbook", "path": str((home / "WellWatch").resolve())} in ww["folders"]
    # running join again is a no-op (already joined)
    r = subprocess.run([sys.executable, "-m", "memyselfandi.cli", "join", url, "--no-claude", "--no-clone"],
                       env=new_env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "already joined" in r.stdout
    assert req(url, "/api/admin/revoke_machine", TOKEN, b'{"name": "macbook"}')[0] == 200
    assert req(url, "/api/tool/list_projects", mac["token"])[0] == 401


def test_join_codes_are_single_use_and_poll_needs_the_secret(hub):  # noqa: F811
    url, _ = hub
    _, r = req(url, "/api/join/start", "", b'{"machine": "box"}')
    assert req(url, "/api/join/poll", "", b'{"poll": "guess"}')[0] == 404
    assert req(url, "/api/join/poll", "", json.dumps({"poll": r["poll"]}).encode())[1]["status"] == "pending"
    assert req(url, "/api/admin/approve_join", TOKEN, json.dumps({"code": r["code"]}).encode())[0] == 200
    code, got = req(url, "/api/join/poll", "", json.dumps({"poll": r["poll"]}).encode())
    assert code == 200 and got["token"].startswith("mmd_")
    assert req(url, "/api/join/poll", "", json.dumps({"poll": r["poll"]}).encode())[0] == 404   # once only
    assert req(url, "/api/admin/approve_join", TOKEN, json.dumps({"code": r["code"]}).encode())[0] == 400
    assert req(url, "/api/join/start", "", b'{"machine": "bad name!"}')[0] == 400
    for i in range(5):
        req(url, "/api/join/start", "", json.dumps({"machine": f"m{i}"}).encode())
    assert req(url, "/api/join/start", "", b'{"machine": "one-too-many"}')[0] == 429


def test_resolve_by_projects_home():
    projects = [{"name": "ww", "home_dir": "WellWatch", "folders": []},
                {"name": "ui", "home_dir": None, "folders": [{"machine": "mac", "path": "/Users/me/code/WellWatch/ui"}]}]
    assert resolve_project("/Users/me/code/WellWatch/src", projects, "mac", "/Users/me/code") == "ww"
    assert resolve_project("/Users/me/code/WellWatch/ui/x", projects, "mac", "/Users/me/code") == "ui"  # deeper
    assert resolve_project("/Users/me/other/WellWatch", projects, "mac", "/Users/me/code") is None


def test_joins_can_be_disabled():
    import pytest

    from memyselfandi.hub import Joins
    from memyselfandi.store import NotFound, Store

    with pytest.raises(NotFound, match="allow_join"):
        Joins(Store(":memory:"), enabled=False).start("box")


def test_projects_remember_their_folder_name_and_clone_url(tmp_path):
    from memyselfandi.projects import backfill_sources, provision_project
    from memyselfandi.store import Store
    from test_projects import git

    root = tmp_path / "projects"
    repo = root / "WellWatch"
    git("init", "-q", str(repo))
    git("-C", str(repo), "remote", "add", "origin", "git@github.com:me/WellWatch.git")
    s = Store(":memory:", hub_machine="pc")
    s.add_project("old", str(repo))                   # added before v8: nothing recorded
    backfill_sources(s, root)
    p = s.get_project("old")
    assert p["home_dir"] == "WellWatch" and p["git_url"] == "git@github.com:me/WellWatch.git"
    # a folder on another machine, reported relative to its projects home
    p = provision_project(s, "mac-only", root, folders=["/Users/me/code/thing"], machine="mac", home_dir="thing")
    assert p["home_dir"] == "thing"
    assert provision_project(s, "evil", root, folders=["/x"], machine="mac", home_dir="../../etc")["home_dir"] is None
