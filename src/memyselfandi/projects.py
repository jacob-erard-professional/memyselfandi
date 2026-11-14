"""Provisioning a project on the hub: use the repo if it's already on this machine, otherwise clone it here.
Organizers read code on the hub machine, so a project that only exists on your laptop needs a copy."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .store import Store

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _normalize(url: str) -> str:
    """Compare git remotes loosely: https://github.com/a/b(.git), git@github.com:a/b(.git) -> github.com/a/b."""
    u = url.strip().removesuffix("/").removesuffix(".git")
    u = re.sub(r"^[a-z+]+://([^@/]+@)?", "", u)   # scheme + optional user@
    u = re.sub(r"^[^@/]+@([^:/]+):", r"\1/", u)    # scp-style git@host:path
    return u.lower()


def _origin(path: Path) -> str | None:
    r = subprocess.run(["git", "-C", str(path), "remote", "get-url", "origin"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _clone(git_url: str, target: Path) -> None:
    cmd = ["git"]
    if git_url.startswith("https://") and shutil.which("gh"):
        # use the GitHub CLI's login for private repos, without touching global git config
        cmd += ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"]
    target.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([*cmd, "clone", "--quiet", git_url, str(target)], capture_output=True, text=True,
                       timeout=600, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if r.returncode != 0:
        shutil.rmtree(target, ignore_errors=True)
        raise ValueError(f"couldn't clone {git_url} onto the hub: {r.stderr.strip()[-300:]}")


def provision_project(
    store: Store, name: str, projects_root: str | Path, folders: list[str] | None = None,
    machine: str | None = None, git_url: str | None = None, description: str | None = None,
    organizers: int | None = None, trust: bool | None = None,
) -> dict[str, Any]:
    """Create project `name` (if new) and attach folders to it. A project is a set of folders on any of your
    machines; git plays no part in it. Folders on the hub must exist here (the organizer reads them); folders on
    other machines are checked by that machine's CLI. `git_url` optionally clones a copy onto the hub too."""
    if not NAME_RE.match(name or ""):
        raise ValueError("project name must be letters, digits, '.', '_' or '-' (max 64), starting with a letter/digit")
    machine = machine or store.hub_machine
    on_hub = machine == store.hub_machine
    attach: list[tuple[str, str]] = []
    for f in folders or []:
        if on_hub:
            d = Path(f).expanduser()
            if not d.is_dir():
                raise ValueError(f"{f} doesn't exist on the hub ({store.hub_machine})")
            f = str(d.resolve())
        attach.append((machine, f))
    cloned = None
    if git_url:
        repo = (Path(projects_root).expanduser() / name).resolve()
        if repo.exists():
            existing = _origin(repo)
            if not existing or _normalize(existing) != _normalize(git_url):
                raise ValueError(f"{repo} already exists on the hub and isn't a clone of {git_url}")
        else:
            _clone(git_url, repo)
            cloned = str(repo)
        attach.append((store.hub_machine, str(repo)))
    project = store.add_project(name, description=description, organizers=organizers, auto_trust=trust)
    for m, f in attach:
        store.add_folder(name, f, m)
    return {**store.get_project(name), "attached": [{"machine": m, "path": f} for m, f in attach],
            "cloned": cloned}


def remove_folders(store: Store, name: str, folders: list[str], machine: str | None = None) -> dict[str, Any]:
    machine = machine or store.hub_machine
    removed = [f for f in folders if store.remove_folder(name, f, machine)]
    return {**store.get_project(name), "removed": removed}
