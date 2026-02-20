"""Project-local MCP and hook configuration helpers."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .utils import ensure_dir, normalize_path


CTX_SERVER_NAME = "ctx-memory"
CLAUDE_HOOK_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")


def _hook_command(project: Path, event: str) -> str:
    path = str(project).replace('"', '\\"')
    return f'ctx hook ingest --project-path "{path}" --event {event}'


def _legacy_hook_command(project: Path, event: str) -> str:
    return f"ctx hook ingest --project-path {project} --event {event}"


def _is_ctx_hook_command(value: str, project: Path, event: str) -> bool:
    if value in {_hook_command(project, event), _legacy_hook_command(project, event)}:
        return True
    return "ctx hook ingest --project-path" in value and f"--event {event}" in value


def _ctx_hook_entry(project: Path, event: str) -> dict:
    command = _hook_command(project, event)
    if event in {"PreToolUse", "PostToolUse"}:
        return {
            "matcher": "*",
            "hooks": [{"type": "command", "command": command}],
        }
    return {
        "hooks": [{"type": "command", "command": command}],
    }


def _entry_contains_ctx_hook(entry: object, project: Path, event: str) -> bool:
    if not isinstance(entry, dict):
        return False
    # Legacy invalid format:
    # {"type":"command","command":"ctx hook ingest ..."}
    if (
        entry.get("type") == "command"
        and isinstance(entry.get("command"), str)
        and _is_ctx_hook_command(entry.get("command"), project, event)
    ):
        return True
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return False
    for hook in hooks:
        if (
            isinstance(hook, dict)
            and hook.get("type") == "command"
            and isinstance(hook.get("command"), str)
            and _is_ctx_hook_command(hook.get("command"), project, event)
        ):
            return True
    return False


def _atomic_write_json(path: Path, payload: dict) -> None:
    ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(payload, indent=2, ensure_ascii=True) + "\n"
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _resolve_ctx_command() -> str:
    resolved = shutil.which("ctx")
    if resolved:
        return str(Path(resolved).resolve())
    return "ctx"


def _is_valid_ctx_command(command: object) -> bool:
    if not isinstance(command, str) or not command:
        return False
    if command == "ctx":
        return True
    return Path(command).name in {"ctx", "ctx.exe"}


def _read_json(path: Path, force: bool) -> dict:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        if force:
            return {}
        raise ValueError(f"Invalid JSON in {path}. Use --force to overwrite.")
    if isinstance(loaded, dict):
        return loaded
    if force:
        return {}
    raise ValueError(f"Expected JSON object in {path}. Use --force to overwrite.")


def ensure_gitignore_entry(project_path: Path, entry: str = ".context-memory/") -> bool:
    gitignore = normalize_path(project_path) / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(f"{entry}\n", encoding="utf-8")
        return True

    lines = gitignore.read_text(encoding="utf-8").splitlines()
    if entry in lines:
        return False
    content = gitignore.read_text(encoding="utf-8")
    if content and not content.endswith("\n"):
        content += "\n"
    content += f"{entry}\n"
    gitignore.write_text(content, encoding="utf-8")
    return True


def update_cursor_mcp_config(project_path: Path, force: bool = False) -> Path:
    project = normalize_path(project_path)
    config_path = project / ".cursor" / "mcp.json"
    payload = _read_json(config_path, force=force)
    mcp_servers = payload.get("mcpServers")
    if not isinstance(mcp_servers, dict):
        mcp_servers = {}
        payload["mcpServers"] = mcp_servers

    ctx_command = _resolve_ctx_command()
    mcp_servers[CTX_SERVER_NAME] = {
        "command": ctx_command,
        "args": ["mcp", "serve", "--project-path", str(project)],
    }
    _atomic_write_json(config_path, payload)
    return config_path


def update_claude_settings(project_path: Path, force: bool = False) -> Path:
    project = normalize_path(project_path)
    settings_path = project / ".claude" / "settings.local.json"
    payload = _read_json(settings_path, force=force)

    mcp_servers = payload.get("mcpServers")
    if not isinstance(mcp_servers, dict):
        mcp_servers = {}
        payload["mcpServers"] = mcp_servers
    ctx_command = _resolve_ctx_command()
    mcp_servers[CTX_SERVER_NAME] = {
        "command": ctx_command,
        "args": ["mcp", "serve", "--project-path", str(project)],
    }

    hooks = payload.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        payload["hooks"] = hooks
    for event in CLAUDE_HOOK_EVENTS:
        existing = hooks.get(event)
        if not isinstance(existing, list):
            existing = []
        filtered = [
            item
            for item in existing
            if not _entry_contains_ctx_hook(item, project, event)
        ]
        filtered.append(_ctx_hook_entry(project, event))
        hooks[event] = filtered

    _atomic_write_json(settings_path, payload)
    return settings_path


def inspect_cursor_mcp_config(project_path: Path) -> tuple[str, str]:
    project = normalize_path(project_path)
    path = project / ".cursor" / "mcp.json"
    if not path.exists():
        return ("unavailable", f"missing {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ("degraded", f"invalid JSON in {path}")
    servers = payload.get("mcpServers")
    if not isinstance(servers, dict):
        return ("degraded", "mcpServers missing or invalid")
    server = servers.get(CTX_SERVER_NAME)
    if not isinstance(server, dict):
        return ("degraded", f"{CTX_SERVER_NAME} not configured")
    command = server.get("command")
    args = server.get("args")
    expected_arg = str(project)
    if not _is_valid_ctx_command(command):
        return ("degraded", "ctx-memory command is not a valid ctx executable")
    if not isinstance(args, list) or expected_arg not in [str(x) for x in args]:
        return ("degraded", "ctx-memory args missing project path")
    return ("available", str(path))


def inspect_claude_settings(project_path: Path) -> tuple[str, str, tuple[str, str]]:
    project = normalize_path(project_path)
    path = project / ".claude" / "settings.local.json"
    if not path.exists():
        return ("unavailable", f"missing {path}", ("unavailable", f"missing {path}"))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return (
            "degraded",
            f"invalid JSON in {path}",
            ("degraded", f"invalid JSON in {path}"),
        )

    servers = payload.get("mcpServers")
    if not isinstance(servers, dict):
        mcp_status = ("degraded", "mcpServers missing or invalid")
    else:
        server = servers.get(CTX_SERVER_NAME)
        if not isinstance(server, dict):
            mcp_status = ("degraded", f"{CTX_SERVER_NAME} not configured")
        else:
            command = server.get("command")
            args = server.get("args")
            expected_arg = str(project)
            if not _is_valid_ctx_command(command):
                mcp_status = ("degraded", "ctx-memory command is not a valid ctx executable")
            elif not isinstance(args, list) or expected_arg not in [str(x) for x in args]:
                mcp_status = ("degraded", "ctx-memory args missing project path")
            else:
                mcp_status = ("available", str(path))

    hooks = payload.get("hooks")
    if not isinstance(hooks, dict):
        hook_status = ("degraded", "hooks missing or invalid")
    else:
        missing = []
        for event in CLAUDE_HOOK_EVENTS:
            entries = hooks.get(event)
            ok = False
            if isinstance(entries, list):
                for item in entries:
                    if _entry_contains_ctx_hook(item, project, event):
                        ok = True
                        break
            if not ok:
                missing.append(event)
        if missing:
            hook_status = ("degraded", f"missing hooks for: {', '.join(missing)}")
        else:
            hook_status = ("available", str(path))

    return mcp_status[0], mcp_status[1], hook_status


def resolve_ctx_executable() -> tuple[str, str]:
    resolved = shutil.which("ctx")
    if not resolved:
        return ("degraded", "ctx executable not found on PATH")
    return ("available", resolved)
