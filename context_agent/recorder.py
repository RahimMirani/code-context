"""Background recorder process for context-agent."""

from __future__ import annotations

import hashlib
import json
import signal
import subprocess
import time
from pathlib import Path

from .constants import DELETED_FILE_HASH, SUPPORTED_ADAPTERS
from .project_db import ProjectStore, StorageCapError
from .registry import Registry
from .utils import normalize_path


EXCLUDED_DIRS = {
    ".git",
    ".context-memory",
    ".venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".idea",
    ".vscode",
}


class Recorder:
    def __init__(
        self,
        project_path: Path,
        session_id: int,
        agent: str,
        registry_home: Path,
        interval_seconds: float = 2.0,
    ):
        self.project_path = normalize_path(project_path)
        self.session_id = session_id
        self.agent = agent
        self.registry = Registry(registry_home)
        self.store = ProjectStore(self.project_path)
        self.interval_seconds = interval_seconds
        self.stop_requested = False
        self.last_git_snapshot: tuple[str, str] | None = None
        self.last_file_snapshot: dict[str, str] | None = None

    def request_stop(self, *_args) -> None:
        self.stop_requested = True

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)

        self.store.update_source_status(self.session_id, "git", "unknown", "awaiting first scan")
        self.store.update_source_status(self.session_id, "filesystem", "unknown", "awaiting first scan")
        for adapter in SUPPORTED_ADAPTERS:
            self._update_adapter_availability(adapter)

        while not self.stop_requested:
            state = self.store.get_session_state(self.session_id)
            if state in {"stopping", "stopped", None}:
                break
            self._poll_adapters()
            self._poll_git()
            self._poll_filesystem()
            time.sleep(self.interval_seconds)

        try:
            self.store.insert_event(
                session_id=self.session_id,
                event_type="handoff",
                summary="Recorder stopped cleanly.",
                files_touched=[],
                source="recorder",
            )
        except Exception:
            pass
        self.store.set_session_state(self.session_id, "stopped")
        self.registry.set_recording_state(self.project_path, "stopped", None, None)
        return 0

    def _poll_adapters(self) -> None:
        for adapter in SUPPORTED_ADAPTERS:
            self._poll_adapter(adapter)

    def _update_adapter_availability(self, adapter: str) -> None:
        adapters = self.registry.get_adapter_configs()
        configured = adapters.get(adapter)
        if not configured:
            self.store.update_source_status(
                self.session_id, f"adapter:{adapter}", "unavailable", "not configured"
            )
            return
        path = normalize_path(configured)
        if not path.exists():
            self.store.update_source_status(
                self.session_id, f"adapter:{adapter}", "unavailable", f"missing log path: {path}"
            )
            return
        self.store.update_source_status(
            self.session_id, f"adapter:{adapter}", "available", str(path)
        )

    def _poll_adapter(self, adapter: str) -> None:
        adapters = self.registry.get_adapter_configs()
        configured = adapters.get(adapter)
        if not configured:
            return
        log_path = normalize_path(configured)
        if not log_path.exists() or not log_path.is_file():
            self.store.update_source_status(
                self.session_id, f"adapter:{adapter}", "unavailable", f"missing log file: {log_path}"
            )
            return

        self.store.update_source_status(self.session_id, f"adapter:{adapter}", "available", str(log_path))
        current_offset = self.store.get_adapter_offset(self.session_id, adapter, str(log_path))
        try:
            with log_path.open("rb") as handle:
                handle.seek(current_offset)
                data = handle.read()
                new_offset = handle.tell()
        except OSError as exc:
            self.store.update_source_status(
                self.session_id, f"adapter:{adapter}", "degraded", f"read error: {exc}"
            )
            return

        if not data:
            return

        text = data.decode("utf-8", errors="ignore")
        for raw_line in text.splitlines():
            parsed = self._parse_adapter_line(adapter, raw_line)
            if not parsed:
                continue
            try:
                self.store.insert_event(
                    session_id=self.session_id,
                    event_type=parsed["event_type"],
                    summary=parsed["summary"],
                    files_touched=parsed.get("files_touched"),
                    source=f"adapter:{adapter}",
                    tool_name=parsed.get("tool_name"),
                    tool_purpose=parsed.get("tool_purpose"),
                    tool_result=parsed.get("tool_result"),
                    decision_summary=parsed.get("decision_summary"),
                )
            except StorageCapError:
                self.store.update_source_status(
                    self.session_id,
                    f"adapter:{adapter}",
                    "degraded",
                    "storage cap reached; event dropped",
                )
                return

        self.store.set_adapter_offset(self.session_id, adapter, str(log_path), new_offset)

    def _parse_adapter_line(self, adapter: str, line: str) -> dict | None:
        text = line.strip()
        if not text:
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None

        if isinstance(payload, dict):
            summary = (
                payload.get("summary")
                or payload.get("message")
                or payload.get("content")
                or payload.get("text")
            )
            if not isinstance(summary, str) or not summary.strip():
                return None
            event_type = payload.get("event_type")
            if not isinstance(event_type, str):
                if payload.get("tool_name"):
                    event_type = "tool_use"
                elif payload.get("decision") is True:
                    event_type = "decision_made"
                else:
                    event_type = "task_status"
            files = payload.get("files_touched")
            if not isinstance(files, list):
                files = []
            parsed = {
                "event_type": event_type,
                "summary": summary,
                "files_touched": [str(item) for item in files if isinstance(item, str)],
            }
            if isinstance(payload.get("tool_name"), str):
                parsed["tool_name"] = payload["tool_name"]
                parsed["tool_purpose"] = str(payload.get("purpose", ""))
                parsed["tool_result"] = str(payload.get("result", ""))
            if payload.get("decision") is True and not parsed.get("event_type") == "decision_made":
                parsed["event_type"] = "decision_made"
            if isinstance(payload.get("decision_summary"), str):
                parsed["decision_summary"] = payload["decision_summary"]
            return parsed

        lowered = text.lower()
        if lowered.startswith("user:"):
            event_type = "user_intent"
            summary = text[5:].strip()
        elif lowered.startswith(("assistant:", "claude:", "cursor:", "codex:", "agent:")):
            event_type = "agent_plan"
            summary = text.split(":", 1)[1].strip() if ":" in text else text
        else:
            event_type = "task_status"
            summary = text
        if not summary:
            return None
        return {"event_type": event_type, "summary": summary, "files_touched": []}

    def _poll_git(self) -> None:
        git_check = subprocess.run(
            ["git", "-C", str(self.project_path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
        )
        if git_check.returncode != 0:
            self.store.update_source_status(
                self.session_id, "git", "unavailable", "project is not a git repository"
            )
            return

        status_run = subprocess.run(
            ["git", "-C", str(self.project_path), "status", "--porcelain"],
            capture_output=True,
            text=True,
        )
        head_run = subprocess.run(
            ["git", "-C", str(self.project_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        )
        if status_run.returncode != 0:
            self.store.update_source_status(self.session_id, "git", "degraded", "git status failed")
            return

        status_output = status_run.stdout.strip()
        head = head_run.stdout.strip() if head_run.returncode == 0 else "NO_HEAD"
        snapshot = (head, status_output)
        self.store.update_source_status(self.session_id, "git", "available", f"head={head[:12]}")
        if self.last_git_snapshot is None:
            self.last_git_snapshot = snapshot
            return
        if snapshot == self.last_git_snapshot:
            return

        previous_status = self.last_git_snapshot[1] if self.last_git_snapshot else ""
        files = []
        for line in status_output.splitlines():
            if not line:
                continue
            # Format: XY path
            if len(line) >= 4:
                files.append(line[3:].strip())
        if files:
            file_preview = ", ".join(files[:5])
            suffix = "..." if len(files) > 5 else ""
            summary = f"Git change detected in {len(files)} file(s): {file_preview}{suffix}."
            try:
                self.store.insert_event(
                    session_id=self.session_id,
                    event_type="code_change",
                    summary=summary,
                    files_touched=files,
                    source="git",
                )
            except StorageCapError:
                self.store.update_source_status(
                    self.session_id, "git", "degraded", "storage cap reached; git event dropped"
                )
        elif previous_status:
            try:
                self.store.insert_event(
                    session_id=self.session_id,
                    event_type="revert",
                    summary="Git working tree reverted to clean state.",
                    files_touched=[],
                    source="git",
                )
            except StorageCapError:
                self.store.update_source_status(
                    self.session_id, "git", "degraded", "storage cap reached; git revert event dropped"
                )
        self.last_git_snapshot = snapshot

    def _file_hash(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 64)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def _scan_files(self) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        for path in self.project_path.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(self.project_path)
            if any(part in EXCLUDED_DIRS for part in rel.parts):
                continue
            try:
                snapshot[rel.as_posix()] = self._file_hash(path)
            except OSError:
                continue
        return snapshot

    def _poll_filesystem(self) -> None:
        current = self._scan_files()
        self.store.update_source_status(self.session_id, "filesystem", "available", "scan ok")
        if self.last_file_snapshot is None:
            self.last_file_snapshot = current
            self.store.initialize_file_state(current)
            return

        added = [path for path in current if path not in self.last_file_snapshot]
        removed = [path for path in self.last_file_snapshot if path not in current]
        modified = [
            path
            for path, file_hash in current.items()
            if path in self.last_file_snapshot and self.last_file_snapshot[path] != file_hash
        ]
        changed = added + modified + removed
        if not changed:
            self.last_file_snapshot = current
            return

        for path in changed:
            after_hash = current.get(path)
            if after_hash is None:
                after_hash = DELETED_FILE_HASH
            try:
                self.store.record_file_transition(
                    session_id=self.session_id,
                    source="filesystem",
                    path=path,
                    after_hash=after_hash,
                )
            except StorageCapError:
                self.store.update_source_status(
                    self.session_id, "filesystem", "degraded", "storage cap reached; file event dropped"
                )
                break
        self.last_file_snapshot = current
