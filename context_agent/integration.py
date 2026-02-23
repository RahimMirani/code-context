"""Project-local MCP and hook configuration helpers."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .utils import ensure_dir, normalize_path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    tomllib = None  # type: ignore[assignment]


CTX_SERVER_NAME = "ctx-memory"
CLAUDE_HOOK_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")
RULES_MARKER_BY_TOOL = {
    "cursor": "<!-- ctx-memory-rules:cursor -->",
    "claude": "<!-- ctx-memory-rules:claude -->",
    "codex": "<!-- ctx-memory-rules:codex -->",
}
RULES_PATTERNS_BY_TOOL = {
    "cursor": (
        '{"client":"cursor"}',
        "append_event",
        'client: "cursor"',
    ),
    "claude": (
        "Use ctx-memory MCP only to read context:",
        "ctx hook ingest",
        "Do not use MCP for logging events.",
    ),
    "codex": (
        '{"client":"codex"}',
        "Do not search for memory.md or context files.",
        "Use ctx-memory MCP only for context.",
    ),
}
CURSOR_RULES_BLOCK = """<!-- ctx-memory-rules:cursor -->
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
"""
CLAUDE_RULES_BLOCK = """<!-- ctx-memory-rules:claude -->
Use ctx-memory MCP only to read context:
- At chat start, call `get_context` once.
- Do not use MCP for logging events.

For logging, use hooks only:
- After each assistant response, write a concise summary via `ctx hook ingest` with top-level JSON field `summary` (no raw transcript).
"""
CODEX_RULES_BLOCK = """<!-- ctx-memory-rules:codex -->
# Context memory policy (ctx-memory)

For this repository, always use MCP server `ctx-memory` so context is shared across chats/sessions.
Do not search for memory.md or context files.
Use ctx-memory MCP only for context.
First action every chat: ping + get_context (+ start_chat_session if needed).

## Session start (required)
1. Call `ping` with `{"client":"codex"}`.
2. Call `get_context` with `{"max_events":20,"include_effective_state":true}`.
3. If no active chat session is known, call `start_chat_session` with `{"client":"codex"}`.

## Per-turn logging (strictly required). It should be used every prompt.
1. After each user message, call `append_event` with:
   - `client: "codex"`
   - `event_type: "user_intent"`
   - concise summary of user ask
   - `files_touched`: `[]` unless user directly references files
2. After each assistant response, call `append_event` with:
   - `client: "codex"`
   - `event_type: "task_status"`
   - concise summary of what answer was given, what action was taken, and next step
   - `files_touched`: repo-relative paths changed in this turn, or `[]`
3. Do not skip post-response logging, even for short replies.

## Action logging (required)
1. When using tools, call `append_event` with:
   - `client: "codex"`
   - `event_type: "tool_use"`
   - `tool_name` and short `tool_result`
2. When a key choice is made, call `append_event` with:
   - `client: "codex"`
   - `event_type: "decision_made"`
   - summary of decision and why
3. When tests run or errors happen, log `test_result` / `error_seen`.

## Handoff/end (required)
1. Call `append_event` with:
   - `client: "codex"`
   - `event_type: "handoff"`
   - short summary of completed + pending work
2. If session id is available, call `stop_chat_session`.

## Constraints
1. Never store raw prompt text or full assistant responses.
2. Store only short factual summaries.
3. Always include `client: "codex"` in every `append_event` call (never `mcp:unknown`).
4. Prefer multiple small events over one long event.
5. If an MCP call fails, retry once and continue; do not silently skip logging.
"""
RULES_PATH_BY_TOOL = {
    "cursor": Path(".cursor/rules/overall.md"),
    "claude": Path(".claude/Claude.md"),
    "codex": Path("AGENTS.md"),
}
RULES_BLOCK_BY_TOOL = {
    "cursor": CURSOR_RULES_BLOCK,
    "claude": CLAUDE_RULES_BLOCK,
    "codex": CODEX_RULES_BLOCK,
}


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


def _read_toml_text(path: Path, force: bool) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return ""
    if tomllib is not None:
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            if force:
                return ""
            raise ValueError(f"Invalid TOML in {path}: {exc}. Use --force to overwrite.") from exc
    else:
        for line_no, raw_line in enumerate(text.splitlines(), start=1):
            line = re.sub(r"\s+#.*$", "", raw_line).strip()
            if not line:
                continue
            if line.startswith("["):
                if not line.endswith("]"):
                    if force:
                        return ""
                    raise ValueError(
                        f"Invalid TOML in {path}: invalid table header on line {line_no}. "
                        "Use --force to overwrite."
                    )
                continue
            if "=" not in line:
                if force:
                    return ""
                raise ValueError(
                    f"Invalid TOML in {path}: invalid key/value on line {line_no}. "
                    "Use --force to overwrite."
                )
            key, value = [item.strip() for item in line.split("=", 1)]
            if not key:
                if force:
                    return ""
                raise ValueError(
                    f"Invalid TOML in {path}: empty key on line {line_no}. "
                    "Use --force to overwrite."
                )
            if value.startswith('"') and not re.match(r'^"(?:[^"\\]|\\.)*"$', value):
                if force:
                    return ""
                raise ValueError(
                    f"Invalid TOML in {path}: invalid string value on line {line_no}. "
                    "Use --force to overwrite."
                )
            if value.startswith("'") and not (len(value) >= 2 and value.endswith("'")):
                if force:
                    return ""
                raise ValueError(
                    f"Invalid TOML in {path}: invalid literal string on line {line_no}. "
                    "Use --force to overwrite."
                )
            if value.startswith("[") and not value.endswith("]"):
                if force:
                    return ""
                raise ValueError(
                    f"Invalid TOML in {path}: invalid array value on line {line_no}. "
                    "Use --force to overwrite."
                )
    return text


def _toml_table_name(line: str) -> str | None:
    stripped = line.strip()
    if not stripped.startswith("[") or not stripped.endswith("]"):
        return None
    if stripped.startswith("[["):
        return None
    return stripped[1:-1].strip()


def _split_toml_dotted_name(value: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for char in value:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            current.append(char)
            continue
        if char == ".":
            token = "".join(current).strip()
            if token:
                tokens.append(token)
            current = []
            continue
        current.append(char)
    token = "".join(current).strip()
    if token:
        tokens.append(token)
    return tokens


def _normalize_toml_token(token: str) -> str:
    value = token.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _is_codex_ctx_server_table(table_name: str) -> bool:
    tokens = [_normalize_toml_token(token) for token in _split_toml_dotted_name(table_name)]
    return len(tokens) == 2 and tokens[0] == "mcp_servers" and tokens[1] == CTX_SERVER_NAME


def _toml_sections(lines: list[str]) -> list[tuple[int, int, str]]:
    headers: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        table_name = _toml_table_name(line)
        if table_name is not None:
            headers.append((index, table_name))
    sections: list[tuple[int, int, str]] = []
    for position, (start, table_name) in enumerate(headers):
        end = headers[position + 1][0] if position + 1 < len(headers) else len(lines)
        sections.append((start, end, table_name))
    return sections


def _parse_toml_string_value(value: str) -> str | None:
    stripped = value.strip()
    if not stripped:
        return None
    if stripped.startswith('"'):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, str) else None
    if stripped.startswith("'") and stripped.endswith("'") and len(stripped) >= 2:
        return stripped[1:-1]
    return None


def _split_toml_array_items(value: str) -> list[str] | None:
    stripped = value.strip()
    if not stripped.startswith("[") or not stripped.endswith("]"):
        return None
    inner = stripped[1:-1].strip()
    if not inner:
        return []

    items: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escape = False
    for char in inner:
        if quote:
            current.append(char)
            if quote == '"' and char == "\\" and not escape:
                escape = True
                continue
            if char == quote and not escape:
                quote = None
            escape = False
            continue
        if char in {'"', "'"}:
            quote = char
            current.append(char)
            continue
        if char == ",":
            item = "".join(current).strip()
            if item:
                items.append(item)
            current = []
            continue
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        items.append(tail)
    return items


def _upsert_codex_ctx_server_table(existing_text: str, project: Path) -> str:
    lines = existing_text.splitlines()
    sections = _toml_sections(lines)
    remove_ranges = [(start, end) for start, end, name in sections if _is_codex_ctx_server_table(name)]

    if remove_ranges:
        kept: list[str] = []
        cursor = 0
        for start, end in remove_ranges:
            kept.extend(lines[cursor:start])
            cursor = end
        kept.extend(lines[cursor:])
        lines = kept

    while lines and not lines[-1].strip():
        lines.pop()

    ctx_command = _resolve_ctx_command()
    args_json = json.dumps(["mcp", "serve", "--project-path", str(project)], ensure_ascii=True)
    command_json = json.dumps(ctx_command, ensure_ascii=True)

    if lines:
        lines.append("")
    lines.extend(
        [
            f'[mcp_servers."{CTX_SERVER_NAME}"]',
            f"command = {command_json}",
            f"args = {args_json}",
            "",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def _inspect_codex_ctx_table(text: str) -> tuple[str | None, list[str] | None]:
    lines = text.splitlines()
    sections = _toml_sections(lines)
    start = None
    end = None
    for section_start, section_end, section_name in sections:
        if _is_codex_ctx_server_table(section_name):
            start = section_start
            end = section_end
            break

    if start is None or end is None:
        return None, None

    command = None
    args = None
    for raw_line in lines[start + 1 : end]:
        line = re.sub(r"\s+#.*$", "", raw_line).strip()
        if not line or "=" not in line:
            continue
        key, value = [item.strip() for item in line.split("=", 1)]
        if key == "command":
            command = _parse_toml_string_value(value)
        elif key == "args":
            items = _split_toml_array_items(value)
            if items is None:
                continue
            parsed_items: list[str] = []
            for item in items:
                parsed = _parse_toml_string_value(item)
                if parsed is None:
                    parsed_items = []
                    break
                parsed_items.append(parsed)
            if parsed_items:
                args = parsed_items
    return command, args


def _has_required_rules(content: str, tool: str) -> bool:
    marker = RULES_MARKER_BY_TOOL[tool]
    if marker in content:
        return True
    required_patterns = RULES_PATTERNS_BY_TOOL[tool]
    return all(pattern in content for pattern in required_patterns)


def ensure_tool_rules(project_path: Path, tool: str) -> tuple[Path, bool]:
    tool_name = tool.strip().lower()
    if tool_name not in RULES_PATH_BY_TOOL:
        allowed = ", ".join(sorted(RULES_PATH_BY_TOOL))
        raise ValueError(f"Unsupported rules tool '{tool}'. Allowed: {allowed}")

    project = normalize_path(project_path)
    rel_path = RULES_PATH_BY_TOOL[tool_name]
    path = project / rel_path
    ensure_dir(path.parent)

    if path.exists():
        content = path.read_text(encoding="utf-8")
        if _has_required_rules(content, tool_name):
            return (path, False)
    else:
        content = ""

    block = RULES_BLOCK_BY_TOOL[tool_name].strip() + "\n"
    updated = content
    if updated and not updated.endswith("\n"):
        updated += "\n"
    if updated.strip():
        updated += "\n"
    updated += block
    path.write_text(updated, encoding="utf-8")
    return (path, True)


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


def update_codex_config(project_path: Path, force: bool = False) -> Path:
    project = normalize_path(project_path)
    config_path = project / ".codex" / "config.toml"
    existing = _read_toml_text(config_path, force=force)
    updated = _upsert_codex_ctx_server_table(existing, project)
    ensure_dir(config_path.parent)
    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.write_text(updated, encoding="utf-8")
    tmp.replace(config_path)
    return config_path


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


def inspect_codex_config(project_path: Path) -> tuple[str, str]:
    project = normalize_path(project_path)
    path = project / ".codex" / "config.toml"
    if not path.exists():
        return ("unavailable", f"missing {path}")
    try:
        text = _read_toml_text(path, force=False)
    except ValueError:
        return ("degraded", f"invalid TOML in {path}")

    command, args = _inspect_codex_ctx_table(text)
    if command is None:
        return ("degraded", f"{CTX_SERVER_NAME} not configured")
    expected_arg = str(project)
    if not _is_valid_ctx_command(command):
        return ("degraded", "ctx-memory command is not a valid ctx executable")
    if not isinstance(args, list) or expected_arg not in [str(x) for x in args]:
        return ("degraded", "ctx-memory args missing project path")
    return ("available", f"{path} (requires Codex project trust)")


def resolve_ctx_executable() -> tuple[str, str]:
    resolved = shutil.which("ctx")
    if not resolved:
        return ("degraded", "ctx executable not found on PATH")
    return ("available", resolved)
