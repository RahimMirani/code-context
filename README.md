# Context Agent (Local Project Memory)

Tech stack:
- Python 3 CLI (`argparse`, `subprocess`, `sqlite3`)
- Local SQLite databases:
  - Project memory DB: `<project>/.context-memory/context.db`
  - Global registry DB: `~/.context-agent/registry.db` (override with `CTX_HOME`)
- Background recorder process for polling adapters/git/filesystem

## Easy global install

One-command installer:
- `bash /absolute/path/to/context_store/install.sh`

Recommended (isolated global CLI):
- `python3 -m pip install --user pipx`
- `python3 -m pipx ensurepath`
- `pipx install /absolute/path/to/context_store`

After this, use `ctx` globally from any terminal.

Alternative (user-site install):
- `python3 -m pip install --user /absolute/path/to/context_store`

If `ctx` is not found, add your user scripts directory to `PATH` and restart the terminal.

## Quick usage
- `ctx start --path /abs/project --name my-project --agent auto`
- `ctx status --path /abs/project`
- `ctx where --path /abs/project`
- `ctx stop --path /abs/project`
- `ctx delete --path /abs/project`
- `ctx purge --path /abs/project --force`

Undo/revert behavior:
- Saved file changes are tracked per file hash.
- If a file returns to a previously seen hash, `ctx` records a `revert` event.
- `ctx status` shows effective changed file count and latest revert timestamp.

Adapter setup:
- `ctx adapter configure cursor --log-path /path/to/cursor.log`
- `ctx adapter configure claude --log-path /path/to/claude.log`

Vector toggle:
- `ctx vector enable --path /abs/project`
