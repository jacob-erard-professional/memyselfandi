---
name: {server}
description: Connect this session to the memyselfandi organizer for the current project, to check in for a briefing and assignment, ask the organizer a question, or hand off work to the next agent. Use only when the user asks (e.g. "check in with the organizer", "connect to memyselfandi", "hand this off", /memyselfandi).
---

# memyselfandi

The user wants this session to work through the project's organizer, an agent that coordinates their agents
across machines and remembers where work left off.

1. If you're already connected in this session (you have a `cc-...` agent id), skip to step 3.
2. Connect. Run this command, adding `--project <name>` if the user named a project:

   ```bash
   {command} connect --cwd "$PWD"
   ```

   It prints your agent id, the project, and the protocol. If it says the directory isn't a registered
   project, tell the user which projects exist and ask which one (or whether to skip).
3. Follow the printed protocol with the `{server}` MCP tools. Usually: check in, wait for the
   organizer's reply, and relay the briefing to the user.

If the user invoked this to hand off or report, connect if needed, then ask_organizer(kind="handoff") with
what's done, what's left, and where.
