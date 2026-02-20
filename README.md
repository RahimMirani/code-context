# Context Agent (Local Project Memory)

Tech stack:
- Python 3 CLI (`argparse`, `subprocess`, `sqlite3`)
- Project-local SQLite memory in `.context-memory/`
- Global registry in `~/.context-agent/registry.db` (override with `CTX_HOME`)
- MCP stdio server for Cursor/Claude integration

## Install

One-command installer:
- `bash /absolute/path/to/context_store/install.sh`

Recommended global install:
- `python3 -m pip install --user pipx`
- `python3 -m pipx ensurepath`
- `pipx install /absolute/path/to/context_store`

Alternative:
- `python3 -m pip install --user /absolute/path/to/context_store`

## First-time repo setup

Inside the repo you want to track:
- `ctx init`
- `ctx start`

If you upgraded from an older version that wrote legacy Claude hook format, run:
- `ctx init --force`

`ctx init` does:
- writes `.cursor/mcp.json` with `ctx-memory` MCP server
- writes `.claude/settings.local.json` with `ctx-memory` MCP + Claude hook commands
- ensures `.context-memory/` is in `.gitignore`

## Daily usage

- `ctx status`
- `ctx where`
- `ctx doctor`
- `ctx sessions`
- `ctx resume --session-id <id>`
- `ctx delete --session-id <id>`
- `ctx stop`

You can still use explicit path mode:
- `ctx start --path /abs/project --name my-project --agent auto`

## MCP + Hook commands

- `ctx mcp serve --project-path /abs/project`
- `ctx hook ingest --project-path /abs/project --event UserPromptSubmit`

These are usually launched by Cursor/Claude, not manually.

## Fallback adapter mode (optional)

If you want file-based ingestion fallback:
- `ctx adapter configure cursor --log-path /path/to/cursor.log`
- `ctx adapter configure claude --log-path /path/to/claude.log`

## Features

- Summary-only storage (no raw prompt/response persistence)
- Revert tracking via file-hash state
- Soft delete + purge
- Shared memory across sessions/models for same repo
