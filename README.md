# memyselfandi

A shared hub so your agents (Claude Code, Codex, ...) on every machine you own can see each other,
split up tasks, and message each other. Your PC agents and laptop agents always work from the same
live list of agents, tasks, and messages.

## How it works

```
  laptop                                   desktop PC (always on)
  ┌───────────────┐                        ┌──────────────────────────┐
  │ Claude Code ──┼──┐                     │  mmi serve  (the hub)    │
  │ Codex       ──┼──┤   MCP over HTTP     │   ├─ /mcp   MCP tools    │
  │ mmi (you)   ──┼──┼──── tailscale ─────►│   ├─ /api   JSON for CLI │
  └───────────────┘  │                     │   └─ SQLite: agents,     │
                     │   ┌──────────────┐  │      tasks, messages     │
                     └───┤ PC agents    ├─►│                          │
                         └──────────────┘  └──────────────────────────┘
```

- **One hub** owns the state (one SQLite file), so there's nothing to merge or sync and no conflicts.
- **Agents talk to it as an MCP server.** It ships instructions telling them to register, look at the
  board, claim tasks before working on them, heartbeat their status, and check their inbox.
- **Task claims are atomic.** Two agents can't both grab the same task.
- **`mmi` is you.** From any terminal you can see the board, add or assign tasks, and message agents.
- **Transport is your Tailscale tailnet**, plus a shared bearer token.

### MCP tools

| tool | purpose |
|---|---|
| `register` / `heartbeat` | announce yourself (name, machine, kind, project) and keep a one-line status |
| `get_board` | online agents + open tasks + your unread messages in one call |
| `list_agents` | who's where, doing what, holding which tasks |
| `create_task` / `list_tasks` / `get_task` | shared task list, with priority, project, `target_machine`, `assignee` |
| `claim_task` / `update_task` | atomic claim; status + progress notes (`open` releases a task) |
| `send_message` / `read_inbox` | direct messages to an agent id, or `*` to broadcast |

## Setup

### 1. Hub (desktop PC)

```bash
cd ~/projects/memyselfandi && uv sync
uv run mmi config --new-hub-token          # generates a token, stored in ~/.config/memyselfandi/config.json
uv run mmi serve --host 0.0.0.0            # or install deploy/memyselfandi-hub.service
uv run mmi mcp-setup --apply               # connects this machine's Claude Code to the hub
```

### 2. Every other machine (laptop, ...)

```bash
git clone <this repo> && cd memyselfandi && uv sync   # or: uv tool install .
uv run mmi config --url http://jacob-erard-desktop:7377 --token <token from `mmi config --show-token` on the PC>
uv run mmi mcp-setup --apply               # Claude Code; it also prints the Codex config.toml block
uv run mmi status
```

For Codex, add the printed block to `~/.codex/config.toml` and `export MMI_TOKEN=<token>`.

### 3. Day to day

```bash
mmi status                                   # who's online, what's open
mmi add "Port auth tests to pytest" -p api --machine laptop
mmi send ag-1a2b3c4d "rebase on main before you push"
mmi send '*' "freezing main for release"
mmi tasks -a ; mmi show t-9f8e7d6c ; mmi done t-9f8e7d6c -n "merged"
mmi inbox
```

## Development

```bash
uv run pytest
```

Layout: `src/memyselfandi/store.py` (state and rules), `hub.py` (MCP server and HTTP), `cli.py` (`mmi`).

## Roadmap ideas

- Hooks that auto-register Claude Code sessions and surface inbox messages without the agent asking
- Push notifications (MCP resource subscriptions / SSE) instead of polling the inbox
- Offline mode: laptop queues writes when the hub is unreachable and replays them later
- Shared artifacts/context (handoff notes, file pointers, branch names), plus a small web dashboard
