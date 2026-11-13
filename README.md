# memyselfandi

One organizer per project that every agent on every machine reports to. Workers (Claude Code, Codex,
on your PC or laptop) queue up to talk to it, get briefed with exactly where the last agent left off,
and get their next assignment. The organizer can summon a council of specialists to take work off its
plate, and they hand work back out.

```
 workers (PC, laptop, any session)                 desktop PC: the hub (`mmi up`)
 ┌──────────────────────┐   MCP over tailscale   ┌──────────────────────────────────────────────┐
 │ Claude Code / Codex  │ ── ask_organizer ────► │ queue (per project): first come first served │
 │  "done with X, next?"│ ◄─ wait_for_reply ──── │   council reports go to the front            │
 └──────────────────────┘                        │                                              │
                                                 │ ORGANIZER  (Opus 5.5, tmux window)           │
 you: `mmi status / ask / ledger / attach`       │   ledger = project memory/continuity         │
                                                 │   briefs, assigns tasks, delegates           │
                                                 │        │ summon_council(tier, brief)           │
                                                 │        ▼                                     │
                                                 │ COUNCIL (temporary, own tmux windows)        │
                                                 │   routine = Sonnet 5.5   (routing, lookups)  │
                                                 │   senior  = Opus 5       (design, debugging) │
                                                 │   elder   = Fable 5.2    (extreme changes)   │
                                                 └──────────────────────────────────────────────┘
```

## How it works

- **Projects** are registered on the hub with a repo path (`mmi project add`). Each gets 1+ organizers.
- **Workers** opt in with the `/memyselfandi` skill. Then they follow the protocol:
  1. `register`
  2. `ask_organizer(kind="checkin")` and wait for the reply, which is the briefing plus an assignment
  3. Work.
     - **Questions:** `ask_organizer(kind="question", blocking=…)`.
     - **Blocking:** if the answer is the *only* thing that unblocks them, they `wait_for_reply`.
     - **Non-blocking:** otherwise they keep doing independent work and `check_ticket` later.
  4. `report` or `handoff` with what's done and what's left, so the next agent (on any machine) continues
     from there.
- **The organizer** is a Claude Code session (Opus 5.5) in tmux. It runs with read-only tools plus the
  hub's tools. It doesn't write code; it decides, briefs, assigns tasks, and keeps the **ledger**: a
  living project summary plus a log of decisions and handoffs. It's woken only when its queue has
  something in it, so an idle organizer costs nothing.
- **Council members** are summoned by the organizer per its charter
  (`src/memyselfandi/prompts/organizer.md`). The organizer picks the cheapest tier that will do the job:
  - **Routine / senior / elder:** the tier decides the model.
  - **Justification:** senior and elder require a written justification.
  - **One elder:** only one elder can sit per project at a time.
  - **Logged:** every summons goes to the ledger.

  A member can answer the ticket it was delegated directly and create tasks. It finishes with
  `council_report`, which jumps the queue back to the organizer. Then its window is closed.
- **The supervisor** (inside the hub) keeps organizer windows alive. When a window dies, the supervisor
  puts any ticket it held back in line. It also launches and dismisses council members: they fail after
  exiting without a report or after 90 minutes.

## Setup

### Hub (desktop PC)

```bash
cd ~/projects/memyselfandi && uv sync
uv run mmi config --new-hub-token            # token in ~/.config/memyselfandi/config.json
uv run mmi up                                # hub + supervisor in tmux session "mmi"
uv run mmi project add myapp --path ~/projects/myapp -d "what this project is"
uv run mmi attach org-myapp-1                # first time per repo: accept Claude Code's folder-trust prompt
uv run mmi install-claude                    # this machine's Claude Code: MCP server + /memyselfandi skill
```

Optional `~/.config/memyselfandi/config.json` keys:
- `"tiers": {"routine": "...", "senior": "...", "elder": "..."}` overrides the council models.
- `MMI_NOTIFY_CMD` (env) is run with a message when an elder is summoned or a session is stuck on the
  trust prompt. Example: `MMI_NOTIFY_CMD=slack-notify`.

For an always-on hub, see `deploy/memyselfandi-hub.service`. It runs `mmi serve`, and the supervisor still
creates the `mmi` tmux session for the agents.

### Every other machine (laptop, ...)

```bash
git clone <this repo> && cd memyselfandi && uv sync
uv run mmi config --url http://jacob-erard-desktop:7377 --token <`mmi config --show-token` on the PC>
uv run mmi install-claude                    # Claude Code (`mmi mcp-setup` prints the Codex config block)
```

### Connecting a session (opt-in)

Sessions **don't** go through the organizer unless you ask. In any Claude Code session, run `/memyselfandi`
or say *"check in with the organizer"*. The skill runs `mmi connect`, which:
- figures out the project from the directory, or takes the name you give it
- registers the session as a worker
- hands it the protocol: check in, wait for the briefing, work, then hand off before finishing

A directory matches a project in one of three ways:
- it's under the project's path on the hub
- its git repo folder has the project's name
- it's listed in the `"projects": {"/local/path": "name"}` mapping in `~/.config/memyselfandi/config.json`

### Day to day

```bash
mmi status                     # projects + queue depth, online agents, open tasks
mmi ledger myapp               # where the project stands
mmi queue myapp                # who's waiting in line
mmi council myapp -a           # who's been summoned, at what tier
mmi ask myapp -w "prioritize the auth bug over the UI work"   # you can queue tickets too
mmi attach                     # watch the organizer/council sessions (tmux)
mmi project delete myapp       # wipe a project and everything tagged with it
```

## Storage

Everything lives in one SQLite file on the hub: `~/.local/share/memyselfandi/hub.db` (or `MMI_DB`).
Other machines only talk to it over HTTP; nothing is synced.

- **Tickets are never deleted by agents.** Answering or cancelling one marks it closed (`closed_at`).
- **Retention:** the hub purges tickets that were closed more than 365 days ago, at startup and then daily.
  Change it with `"retention_days"` in the config. Open tickets are never purged.
- **Per-project data:** every row belongs to a project via its `project` column. `mmi project delete <name>`
  shows what it will remove and asks for confirmation. It then deletes that project's tickets, tasks, notes,
  council, ledger, and organizer/council identities, and closes their tmux windows.
- **Migrations:** the schema version lives in `PRAGMA user_version`. Migrations live in `store.py`
  (`MIGRATIONS`) and run automatically when the hub opens the database.
- **Disk writes:** writes happen only when something changes: a ticket, a task, the ledger, or a
  heartbeat throttled to every 2 minutes. Waiting and polling only read.

## Development

`uv run pytest`. The e2e test drives a real hub over MCP in every role. Supervisor tests use a fake tmux.

- `store.py`: state and rules (queue, ledger, council, tasks)
- `hub.py`: MCP tools and the HTTP API
- `supervisor.py`: tmux sessions
- `cli.py`: the `mmi` command
- `prompts/`: the organizer and council charters

## Roadmap

- Prompt a handoff when a connected session ends
- Recycle long-running organizer sessions (fresh context, rebuilt from the ledger)
- Offline mode for the laptop when the hub is unreachable
- Small web dashboard for the ledger and queue
