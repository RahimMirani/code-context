"""Global registry for context-agent projects and adapter configuration."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .constants import CONFIG_FILE, DEFAULT_CAP_BYTES, REGISTRY_DB_FILE
from .utils import ensure_dir, normalize_path, utc_now


class Registry:
    def __init__(self, home_dir: Path):
        self.home_dir = normalize_path(home_dir)
        ensure_dir(self.home_dir)
        self.db_path = self.home_dir / REGISTRY_DB_FILE
        self.config_path = self.home_dir / CONFIG_FILE
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _execute_retry(self, fn):
        retries = 8
        delay = 0.05
        for attempt in range(retries):
            try:
                return fn()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == retries - 1:
                    raise
                time.sleep(delay)
                delay *= 2
        return fn()

    def _init_db(self) -> None:
        def _init():
            with self._connect() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS projects (
                        path TEXT PRIMARY KEY,
                        display_name TEXT,
                        deleted_at TEXT,
                        recording_state TEXT NOT NULL DEFAULT 'stopped',
                        active_session_id INTEGER,
                        recorder_pid INTEGER,
                        db_path TEXT,
                        logs_path TEXT,
                        storage_cap_bytes INTEGER NOT NULL DEFAULT 524288000,
                        storage_used_bytes INTEGER NOT NULL DEFAULT 0,
                        vector_enabled INTEGER NOT NULL DEFAULT 0,
                        last_updated_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS adapter_configs (
                        adapter TEXT PRIMARY KEY,
                        log_path TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    """
                )
        self._execute_retry(_init)

    def upsert_project(
        self,
        project_path: Path,
        display_name: str | None,
        db_path: Path,
        logs_path: Path,
    ) -> None:
        path = str(normalize_path(project_path))
        now = utc_now()
        display = display_name.strip() if display_name else None

        def _write():
            with self._connect() as conn:
                existing = conn.execute(
                    "SELECT display_name, deleted_at, storage_cap_bytes FROM projects WHERE path = ?",
                    (path,),
                ).fetchone()
                if existing:
                    conn.execute(
                        """
                        UPDATE projects
                        SET display_name = COALESCE(?, display_name),
                            db_path = ?,
                            logs_path = ?,
                            updated_at = ?
                        WHERE path = ?
                        """,
                        (display, str(db_path), str(logs_path), now, path),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO projects (
                            path, display_name, deleted_at, recording_state,
                            active_session_id, recorder_pid, db_path, logs_path,
                            storage_cap_bytes, storage_used_bytes, vector_enabled,
                            last_updated_at, created_at, updated_at
                        ) VALUES (?, ?, NULL, 'stopped', NULL, NULL, ?, ?, ?, 0, 0, ?, ?, ?)
                        """,
                        (path, display, str(db_path), str(logs_path), DEFAULT_CAP_BYTES, now, now, now),
                    )

        self._execute_retry(_write)

    def get_project(self, project_path: Path):
        path = str(normalize_path(project_path))

        def _read():
            with self._connect() as conn:
                return conn.execute("SELECT * FROM projects WHERE path = ?", (path,)).fetchone()

        return self._execute_retry(_read)

    def list_projects(self, include_deleted: bool = False):
        def _list():
            with self._connect() as conn:
                if include_deleted:
                    return conn.execute("SELECT * FROM projects ORDER BY path").fetchall()
                return conn.execute(
                    "SELECT * FROM projects WHERE deleted_at IS NULL ORDER BY path"
                ).fetchall()

        return self._execute_retry(_list)

    def find_projects_by_name(self, name: str):
        target = name.strip()

        def _find():
            with self._connect() as conn:
                return conn.execute(
                    """
                    SELECT * FROM projects
                    WHERE deleted_at IS NULL AND display_name = ?
                    ORDER BY path
                    """,
                    (target,),
                ).fetchall()

        return self._execute_retry(_find)

    def set_recording_state(
        self,
        project_path: Path,
        state: str,
        session_id: int | None,
        recorder_pid: int | None,
    ) -> None:
        path = str(normalize_path(project_path))
        now = utc_now()

        def _write():
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE projects
                    SET recording_state = ?,
                        active_session_id = ?,
                        recorder_pid = ?,
                        updated_at = ?,
                        last_updated_at = COALESCE(last_updated_at, ?)
                    WHERE path = ?
                    """,
                    (state, session_id, recorder_pid, now, now, path),
                )

        self._execute_retry(_write)

    def set_project_deleted(self, project_path: Path, deleted: bool) -> None:
        path = str(normalize_path(project_path))
        now = utc_now()
        deleted_at = now if deleted else None

        def _write():
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE projects
                    SET deleted_at = ?,
                        recording_state = CASE WHEN ? IS NOT NULL THEN 'stopped' ELSE recording_state END,
                        active_session_id = CASE WHEN ? IS NOT NULL THEN NULL ELSE active_session_id END,
                        recorder_pid = CASE WHEN ? IS NOT NULL THEN NULL ELSE recorder_pid END,
                        updated_at = ?
                    WHERE path = ?
                    """,
                    (deleted_at, deleted_at, deleted_at, deleted_at, now, path),
                )

        self._execute_retry(_write)

    def remove_project(self, project_path: Path) -> None:
        path = str(normalize_path(project_path))

        def _delete():
            with self._connect() as conn:
                conn.execute("DELETE FROM projects WHERE path = ?", (path,))

        self._execute_retry(_delete)

    def update_storage(self, project_path: Path, used_bytes: int) -> None:
        path = str(normalize_path(project_path))
        now = utc_now()

        def _write():
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE projects
                    SET storage_used_bytes = ?,
                        updated_at = ?
                    WHERE path = ?
                    """,
                    (int(used_bytes), now, path),
                )

        self._execute_retry(_write)

    def set_vector_enabled(self, project_path: Path, enabled: bool) -> None:
        path = str(normalize_path(project_path))
        now = utc_now()

        def _write():
            with self._connect() as conn:
                conn.execute(
                    "UPDATE projects SET vector_enabled = ?, updated_at = ? WHERE path = ?",
                    (1 if enabled else 0, now, path),
                )

        self._execute_retry(_write)

    def set_adapter_log_path(self, adapter: str, log_path: Path) -> None:
        now = utc_now()
        adapter_name = adapter.lower().strip()
        path = str(normalize_path(log_path))

        def _write():
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO adapter_configs(adapter, log_path, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(adapter) DO UPDATE SET
                        log_path = excluded.log_path,
                        updated_at = excluded.updated_at
                    """,
                    (adapter_name, path, now),
                )

        self._execute_retry(_write)
        self._sync_config_toml()

    def get_adapter_configs(self) -> dict[str, str]:
        def _read():
            with self._connect() as conn:
                rows = conn.execute("SELECT adapter, log_path FROM adapter_configs").fetchall()
                return {row["adapter"]: row["log_path"] for row in rows}

        return self._execute_retry(_read)

    def _sync_config_toml(self) -> None:
        adapters = self.get_adapter_configs()
        lines = ["# context-agent configuration", ""]
        for adapter in sorted(adapters):
            lines.append(f"[adapters.{adapter}]")
            escaped = adapters[adapter].replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'log_path = "{escaped}"')
            lines.append("")
        self.config_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")

