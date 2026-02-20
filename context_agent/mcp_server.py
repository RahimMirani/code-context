"""Minimal MCP stdio server for context-agent."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .constants import EVENT_TYPES
from .project_db import ProjectStore
from .utils import normalize_path, utc_now


@dataclass
class MCPError(Exception):
    code: int
    message: str


class MCPServer:
    def __init__(self, project_path: Path):
        self.project_path = normalize_path(project_path)
        self.store = ProjectStore(self.project_path)
        # Transport is auto-detected from client input:
        # - "lsp": Content-Length framed JSON-RPC
        # - "jsonl": one JSON object per line
        self._transport_mode = "auto"

    def _read_message(self) -> dict[str, Any] | None:
        first_line = sys.stdin.buffer.readline()
        if not first_line:
            return None

        line = first_line.strip()
        if line.startswith(b"Content-Length:"):
            if self._transport_mode == "auto":
                self._transport_mode = "lsp"
            try:
                length = int(line.split(b":", 1)[1].strip().decode("ascii"))
            except Exception as exc:  # noqa: BLE001
                raise MCPError(-32700, f"invalid Content-Length header: {exc}") from exc
            while True:
                header_line = sys.stdin.buffer.readline()
                if not header_line:
                    raise MCPError(-32700, "unexpected EOF while reading headers")
                if header_line in (b"\r\n", b"\n"):
                    break
            payload = sys.stdin.buffer.read(length)
            if not payload:
                return None
            try:
                return json.loads(payload.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise MCPError(-32700, f"invalid JSON payload: {exc}") from exc

        if self._transport_mode == "auto":
            self._transport_mode = "jsonl"
        try:
            return json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise MCPError(-32700, f"invalid JSON line payload: {exc}") from exc

    def _write_message(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        if self._transport_mode == "jsonl":
            sys.stdout.buffer.write(encoded + b"\n")
            sys.stdout.buffer.flush()
            return
        header = f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii")
        sys.stdout.buffer.write(header + encoded)
        sys.stdout.buffer.flush()

    def _jsonrpc_result(self, request_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _jsonrpc_error(self, request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def _active_session_id(self) -> int | None:
        session = self.store.get_active_session()
        if not session:
            return None
        return int(session["id"])

    def _tool_text_result(self, payload: dict[str, Any], is_error: bool = False) -> dict[str, Any]:
        text = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
        result = {
            "content": [{"type": "text", "text": text}],
            "isError": bool(is_error),
        }
        if not is_error:
            result["structuredContent"] = payload
        return result

    def _handle_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "get_context":
            max_events = int(arguments.get("max_events", 20))
            if max_events < 1:
                max_events = 1
            if max_events > 100:
                max_events = 100
            include_effective_state = bool(arguments.get("include_effective_state", True))
            snapshot = self.store.status_snapshot(recent_limit=max_events)
            project = snapshot["project"]
            events = [
                {
                    "event_type": row["event_type"],
                    "summary": row["summary"],
                    "source": row["source"],
                    "created_at": row["created_at"],
                    "is_effective": int(row["is_effective"] or 0),
                }
                for row in snapshot["events"]
            ]
            payload = {
                "project": str(self.project_path),
                "last_updated_at": project["last_updated_at"] if project else None,
                "recent_events": events,
                "open_items": [],
                "style_signals": [],
            }
            if include_effective_state:
                payload["effective_changed_files"] = snapshot.get("effective_changed_files", 0)
            return self._tool_text_result(payload)

        if name == "append_event":
            session_id = arguments.get("session_id")
            if session_id is None:
                session_id = self._active_session_id()
            if session_id is None:
                raise MCPError(-32010, "No active session. Run `ctx start` first.")

            event_type = str(arguments.get("event_type", "task_status")).strip()
            if event_type not in EVENT_TYPES:
                event_type = "task_status"

            summary = str(arguments.get("summary", "")).strip()
            if not summary:
                raise MCPError(-32602, "summary is required")

            files_touched = arguments.get("files_touched") or []
            if not isinstance(files_touched, list):
                raise MCPError(-32602, "files_touched must be an array")
            files_touched = [str(item) for item in files_touched if isinstance(item, str)]

            decision = bool(arguments.get("decision", False))
            tool_name = arguments.get("tool_name")
            tool_result = arguments.get("tool_result")
            client = str(arguments.get("client", "unknown")).lower()
            source = f"mcp:{client}" if client in {"cursor", "claude"} else "mcp:unknown"
            source_detail = arguments.get("source_detail")
            if source_detail:
                source = f"{source}:{str(source_detail)[:40]}"

            event_id = self.store.insert_event(
                session_id=int(session_id),
                event_type=event_type,
                summary=summary,
                files_touched=files_touched,
                source=source,
                tool_name=str(tool_name) if tool_name else None,
                tool_result=str(tool_result) if tool_result else None,
                decision_summary=summary if decision else None,
            )
            if client in {"cursor", "claude"}:
                self.store.update_source_status(int(session_id), f"mcp:{client}", "available", f"heartbeat {utc_now()}")
            return self._tool_text_result({"ok": True, "event_id": event_id, "session_id": int(session_id)})

        if name == "start_chat_session":
            client = str(arguments.get("client", "")).strip().lower()
            if client not in {"cursor", "claude"}:
                raise MCPError(-32602, "client must be 'cursor' or 'claude'")
            external_session_ref = arguments.get("external_session_ref")
            active = self.store.get_active_session()
            if active:
                session_id = int(active["id"])
                if external_session_ref:
                    self.store.set_session_external_ref(session_id, str(external_session_ref))
            else:
                session_id = self.store.create_session(
                    agent=client,
                    external_session_ref=str(external_session_ref) if external_session_ref else None,
                )
            self.store.update_source_status(session_id, f"mcp:{client}", "available", f"started {utc_now()}")
            return self._tool_text_result({"session_id": session_id})

        if name == "stop_chat_session":
            session_id = arguments.get("session_id")
            if session_id is None:
                raise MCPError(-32602, "session_id is required")
            self.store.set_session_state(int(session_id), "stopped")
            return self._tool_text_result({"stopped": True, "session_id": int(session_id)})

        if name == "ping":
            client = str(arguments.get("client", "")).strip().lower()
            if client not in {"cursor", "claude"}:
                raise MCPError(-32602, "client must be 'cursor' or 'claude'")
            session_id = self._active_session_id()
            if session_id is not None:
                self.store.update_source_status(session_id, f"mcp:{client}", "available", f"heartbeat {utc_now()}")
            return self._tool_text_result({"pong": True, "client": client, "session_id": session_id})

        raise MCPError(-32601, f"Unknown tool: {name}")

    def _tools_spec(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "get_context",
                "description": "Fetch project context summary from local memory.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "max_events": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                        "include_effective_state": {"type": "boolean", "default": True},
                    },
                },
            },
            {
                "name": "append_event",
                "description": "Append summarized event into project memory.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "event_type": {"type": "string"},
                        "summary": {"type": "string"},
                        "files_touched": {"type": "array", "items": {"type": "string"}},
                        "decision": {"type": "boolean", "default": False},
                        "tool_name": {"type": ["string", "null"]},
                        "tool_result": {"type": ["string", "null"]},
                        "source_detail": {"type": ["string", "null"]},
                        "client": {"type": ["string", "null"]},
                        "session_id": {"type": ["integer", "null"]},
                    },
                    "required": ["summary"],
                },
            },
            {
                "name": "start_chat_session",
                "description": "Start or attach to chat session for client.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "client": {"type": "string", "enum": ["cursor", "claude"]},
                        "external_session_ref": {"type": ["string", "null"]},
                    },
                    "required": ["client"],
                },
            },
            {
                "name": "stop_chat_session",
                "description": "Stop session by id.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"session_id": {"type": "integer"}},
                    "required": ["session_id"],
                },
            },
            {
                "name": "ping",
                "description": "Heartbeat for MCP diagnostics.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"client": {"type": "string", "enum": ["cursor", "claude"]}},
                    "required": ["client"],
                },
            },
        ]

    def _handle_request(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")
        params = request.get("params") or {}

        if method == "notifications/initialized":
            return None
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "ctx-memory", "version": "0.1.0"},
                "capabilities": {"tools": {}},
            }
            return self._jsonrpc_result(request_id, result)
        if method == "ping":
            return self._jsonrpc_result(request_id, {"ok": True})
        if method == "tools/list":
            return self._jsonrpc_result(request_id, {"tools": self._tools_spec()})
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(name, str):
                raise MCPError(-32602, "tools/call requires tool name")
            if not isinstance(arguments, dict):
                raise MCPError(-32602, "tools/call arguments must be object")
            return self._jsonrpc_result(request_id, self._handle_tool(name, arguments))

        raise MCPError(-32601, f"Method not found: {method}")

    def serve(self) -> int:
        while True:
            try:
                message = self._read_message()
                if message is None:
                    return 0
                response = self._handle_request(message)
                if response is not None and message.get("id") is not None:
                    self._write_message(response)
            except MCPError as exc:
                request_id = None
                try:
                    request_id = message.get("id") if isinstance(message, dict) else None
                except Exception:  # noqa: BLE001
                    request_id = None
                self._write_message(self._jsonrpc_error(request_id, exc.code, exc.message))
            except Exception as exc:  # noqa: BLE001
                self._write_message(self._jsonrpc_error(None, -32603, f"Internal error: {exc}"))
                return 1
