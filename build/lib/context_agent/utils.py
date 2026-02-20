"""Utilities for context-agent."""

from __future__ import annotations

import os
import re
import signal
import time
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def normalize_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def directory_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except OSError:
                continue
    return total


def human_bytes(size: int) -> str:
    value = float(size)
    units = ["B", "KB", "MB", "GB", "TB"]
    unit = units[0]
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            break
        value /= 1024.0
    return f"{value:.1f} {unit}"


def normalize_summary(summary: str, max_chars: int) -> str:
    collapsed = re.sub(r"\s+", " ", summary.strip())
    return collapsed[:max_chars]


def is_pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def wait_for_process_exit(pid: int, timeout_seconds: int) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not is_pid_alive(pid):
            return True
        time.sleep(0.2)
    return not is_pid_alive(pid)


def terminate_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return

