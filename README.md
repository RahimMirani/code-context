# Context Agent (Local Project Memory)

Tech stack:
- Python 3 CLI (`argparse`, `subprocess`, `sqlite3`)
- Project-local SQLite memory in `.context-memory/`
- Global registry in `~/.context-agent/registry.db` (override with `CTX_HOME`)
- MCP stdio server for Cursor/Claude/Codex integration

## Install globally (from GitHub)

Install `ctx` globally so it works in any repo:
- `python3 -m pip install --user pipx`
- `python3 -m pipx ensurepath`
- `pipx install git+https://github.com/RahimMirani/code-context.git`

Upgrade:
- `pipx install --force git+https://github.com/RahimMirani/code-context.git`

Check install:
- `which ctx`
- `ctx --help`

Where to run these:
- Run install commands in any terminal directory (not tied to a specific repo).
- Run project commands (`ctx init`, `ctx start`, `ctx stop`, etc.) inside the target project root.

If you see `ctx: command not found`:
- Why: `ctx` was installed, but your shell `PATH` was not reloaded yet.
- Fix:
  - `exec zsh -l`
  - `which ctx`
  - `ctx --help`
- If still not found, run directly once:
  - `~/.local/bin/ctx --help`
- If needed, add this to `~/.zshrc`, then reload:
  - `export PATH="$HOME/.local/bin:$HOME/Library/Python/3.9/bin:$PATH"`
  - `exec zsh -l`

Claude MCP add command pitfall:
- If you use multiline `claude mcp add ...` commands, a missing trailing `\` can break argument parsing.
- Symptom: `--project-path` gets no value and the path is executed as a command (`permission denied`).
- Safe fix:
  - Remove broken entry: `claude mcp remove ctx-memory`
  - Re-add in one line (recommended):
    - `claude mcp add ctx-memory /Users/aliuraishmirani/.local/pipx/venvs/context-agent-local/bin/ctx -- mcp serve --project-path /Users/aliuraishmirani/agentbay-dashboard/AgentBay-Dashboard-V2`

## Configure in a project (minimal workflow)

In the project you want to record:
- `ctx init`
- `ctx start`

When `ctx start` (or `ctx resume`) runs:
- It automatically stores a repo snapshot event in context memory (root path, top-level entries, key files, approximate file count).

When you want to stop recording:
- `ctx stop`

Resume an older session:
- `ctx resume --session-id <number>`

Delete a session:
- `ctx delete --session-id <number>`

What `ctx init` configures:
- `.cursor/mcp.json`
- `.claude/settings.local.json`
- `.codex/config.toml`
- `.cursor/rules/overall.md` (ctx-memory policy block)
- `.claude/Claude.md` (ctx-memory policy block)
- `AGENTS.md` (Codex ctx-memory policy block)
- `.gitignore` entry for `.context-memory/`

Rules behavior:
- If a rules file exists but does not already contain the ctx-memory rules, `ctx init` appends the rules block (it does not overwrite custom content).
- Rules are idempotent: rerunning `ctx init` does not duplicate the same ctx-memory rules block.

Codex note:
- Codex must trust the project for project-scoped `.codex/config.toml` to be loaded.

## Codex MCP debug checklist

If Codex is not connecting to `ctx-memory` in `AgentBay-Dashboard-V2`, run:

1. Initialize integration in that exact repo:
   - `ctx init --path /Users/aliuraishmirani/agentbay-dashboard/AgentBay-Dashboard-V2`
2. Ensure Codex MCP server is registered:
   - `codex mcp list`
3. If `ctx-memory` is missing in `codex mcp list`, add it once:
   - `codex mcp add ctx-memory -- /Users/aliuraishmirani/.local/bin/ctx mcp serve --project-path /Users/aliuraishmirani/agentbay-dashboard/AgentBay-Dashboard-V2`
4. Start a ctx session for Codex:
   - `ctx start --path /Users/aliuraishmirani/agentbay-dashboard/AgentBay-Dashboard-V2 --agent codex`
5. Open (or reopen) Codex in the same repo path and send one prompt.
6. Verify heartbeat:
   - `ctx doctor --path /Users/aliuraishmirani/agentbay-dashboard/AgentBay-Dashboard-V2 --json`
   - Expect `checks.codex_mcp.status` to move to `connected`.
7. If still `awaiting MCP heartbeat`:
   - confirm `ctx status` shows `Recording: recording`
   - confirm Codex workspace path is exactly `/Users/aliuraishmirani/agentbay-dashboard/AgentBay-Dashboard-V2`
   - restart Codex once, then re-check `ctx doctor`.

## Command reference

Core:
- `ctx start [--name <display_name>] [--path <project_path>] [--agent cursor|claude|codex|auto]`
- `ctx stop [--path <project_path>]`
- `ctx status [--path <project_path>]`
- `ctx where [--path <project_path>]`
- `ctx sessions [--path <project_path>]`
- `ctx resume --session-id <id> [--path <project_path>]`
- `ctx delete --session-id <id> [--path <project_path>]`
- `ctx delete [--path <project_path>]` (soft-delete project memory)
- `ctx purge [--path <project_path>] --force` (hard delete)
- `ctx list`
- `ctx doctor [--path <project_path>] [--json]`
- `ctx rules <cursor|claude|codex> [--path <project_path>]` (ensure rules for one specific tool)

Adapters (fallback mode):
- `ctx adapter configure cursor --log-path <path>`
- `ctx adapter configure claude --log-path <path>`
- `ctx adapter configure codex --log-path <path>`

MCP/hook runtime:
- `ctx mcp serve --project-path <project_path>`
- `ctx hook ingest --project-path <project_path> --event <event_name>`

Feature flag:
- `ctx vector enable [--path <project_path>]`

## Inspect context DB

From repo root:
- `sqlite3 .context-memory/context.db`

Useful queries:
```sql
.tables
.schema events
SELECT id,created_at,event_type,source,summary FROM events ORDER BY id DESC LIMIT 30;
SELECT id,created_at,tool_name,purpose,result FROM tool_usage ORDER BY id DESC LIMIT 20;
SELECT id,created_at,summary FROM decisions ORDER BY id DESC LIMIT 20;
```

## Cursor policy file

Path:
- `.cursor/rules/overall.md`

Important:
- Tell Cursor to follow these rules on every message in the chat.

Suggested content:

```md
# Context memory policy (ctx-memory)

For this repository, always use MCP server `ctx-memory` so context is shared across chats/sessions.

## Session start (required)
1. Call `ping` with `{"client":"cursor"}`.
2. Call `get_context` with `{"max_events":20,"include_effective_state":true}`.
3. If no active chat session is known, call `start_chat_session` with `{"client":"cursor"}`.

## Per-turn logging (strictly required). It should be used every prompt.
1. After each user message, call `append_event` with:
   - `client: "cursor"`
   - `event_type: "user_intent"`
   - concise summary of user ask
   - `files_touched`: `[]` unless user directly references files
2. After each assistant response, call `append_event` with:
   - `client: "cursor"`
   - `event_type: "task_status"`
   - concise summary of what answer was given, what action was taken, and next step
   - `files_touched`: repo-relative paths changed in this turn, or `[]`
3. Do not skip post-response logging, even for short replies.

## Action logging (required)
1. When using tools, call `append_event` with:
   - `client: "cursor"`
   - `event_type: "tool_use"`
   - `tool_name` and short `tool_result`
2. When a key choice is made, call `append_event` with:
   - `client: "cursor"`
   - `event_type: "decision_made"`
   - summary of decision and why
3. When tests run or errors happen, log `test_result` / `error_seen`.

## Handoff/end (required)
1. Call `append_event` with:
   - `client: "cursor"`
   - `event_type: "handoff"`
   - short summary of completed + pending work
2. If session id is available, call `stop_chat_session`.

## Constraints
1. Never store raw prompt text or full assistant responses.
2. Store only short factual summaries.
3. Always include `client: "cursor"` in every `append_event` call (never `mcp:unknown`).
4. Prefer multiple small events over one long event.
5. If an MCP call fails, retry once and continue; do not silently skip logging.
```

## Claude policy file

Path:
- `.claude/Claude.md`

Important:
- Tell Claude to follow these rules on every message in the chat.

Suggested content:

```md
Use ctx-memory MCP only to read context:
- At chat start, call `get_context` once.
- Do not use MCP for logging events.

For logging, use hooks only:
- After each assistant response, write a concise summary via `ctx hook ingest` with top-level JSON field `summary` (no raw transcript).
```

## Codex policy file (recommended)

Path:
- `AGENTS.md` in your project root (Codex reads it automatically for that repo).

Required:
- Not strictly required for MCP connectivity.

Recommended:
- Add rules so Codex consistently reads and writes context events.

Suggested content:

```md
Use ctx-memory MCP for this repo.

At chat start:
1. Call `ping` with `{"client":"codex"}`.
2. Call `get_context` with `{"max_events":20,"include_effective_state":true}`.
3. If no active session is known, call `start_chat_session` with `{"client":"codex"}`.

Per turn:
1. After user message, call `append_event` with:
   - `client: "codex"`
   - `event_type: "user_intent"`
   - concise summary
2. After assistant message, call `append_event` with:
   - `client: "codex"`
   - `event_type: "task_status"`
   - concise summary

Use `tool_use`, `decision_made`, `test_result`, `error_seen` when relevant.
At handoff, append `handoff` and stop session if session id is available.
```

## Features

- Summary-only storage (no raw prompt/response persistence)
- Revert tracking via file-hash state
- Soft delete + purge
- Shared memory across sessions/models for same repo
