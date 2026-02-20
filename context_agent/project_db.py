"""Per-project SQLite storage and event logic."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .constants import (
    COMPACTION_THRESHOLD_RATIO,
    DELETED_FILE_HASH,
    DEFAULT_CAP_BYTES,
    DEDUPE_WINDOW_SECONDS,
    EVENT_TYPES,
    HIGH_VALUE_EVENT_TYPES,
    PROJECT_DB_FILE,
    PROJECT_LOG_DIR,
    PROJECT_MEMORY_DIR,
    SUMMARY_MAX_CHARS,
)
from .utils import directory_size_bytes, ensure_dir, normalize_path, normalize_summary, utc_now


class StorageCapError(RuntimeError):
    """Raised when memory quota is exceeded and compaction cannot recover enough space."""


def project_memory_paths(project_path: Path) -> tuple[Path, Path, Path]:
    project = normalize_path(project_path)
    root = project / PROJECT_MEMORY_DIR
    db_path = root / PROJECT_DB_FILE
    logs_path = root / PROJECT_LOG_DIR
    return root, db_path, logs_path


class ProjectStore:
    def __init__(self, project_path: Path):
        self.project_path = normalize_path(project_path)
        self.memory_root, self.db_path, self.logs_path = project_memory_paths(self.project_path)
        ensure_dir(self.memory_root)
        ensure_dir(self.logs_path)
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
            now = utc_now()
            with self._connect() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS projects (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        path TEXT UNIQUE NOT NULL,
                        display_name TEXT,
                        recording_state TEXT NOT NULL DEFAULT 'stopped',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        last_updated_at TEXT,
                        deleted_at TEXT,
                        storage_cap_bytes INTEGER NOT NULL DEFAULT 524288000,
                        storage_used_bytes INTEGER NOT NULL DEFAULT 0
                    );

                    CREATE TABLE IF NOT EXISTS sessions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        project_id INTEGER NOT NULL,
                        agent TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        stopped_at TEXT,
                        state TEXT NOT NULL,
                        external_session_ref TEXT,
                        last_updated_at TEXT,
                        FOREIGN KEY(project_id) REFERENCES projects(id)
                    );

                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        project_id INTEGER NOT NULL,
                        session_id INTEGER NOT NULL,
                        event_type TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        files_touched_json TEXT,
                        before_hash TEXT,
                        after_hash TEXT,
                        reverted_event_id INTEGER,
                        reverted_by_event_id INTEGER,
                        is_effective INTEGER NOT NULL DEFAULT 1,
                        summarized_at TEXT,
                        source TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        dedupe_hash TEXT NOT NULL,
                        FOREIGN KEY(project_id) REFERENCES projects(id),
                        FOREIGN KEY(session_id) REFERENCES sessions(id)
                    );

                    CREATE TABLE IF NOT EXISTS tool_usage (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        project_id INTEGER NOT NULL,
                        session_id INTEGER NOT NULL,
                        event_id INTEGER NOT NULL,
                        tool_name TEXT NOT NULL,
                        purpose TEXT,
                        result TEXT,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(project_id) REFERENCES projects(id),
                        FOREIGN KEY(session_id) REFERENCES sessions(id),
                        FOREIGN KEY(event_id) REFERENCES events(id)
                    );

                    CREATE TABLE IF NOT EXISTS decisions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        project_id INTEGER NOT NULL,
                        session_id INTEGER NOT NULL,
                        event_id INTEGER NOT NULL,
                        summary TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(project_id) REFERENCES projects(id),
                        FOREIGN KEY(session_id) REFERENCES sessions(id),
                        FOREIGN KEY(event_id) REFERENCES events(id)
                    );

                    CREATE TABLE IF NOT EXISTS open_tasks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        project_id INTEGER NOT NULL,
                        session_id INTEGER NOT NULL,
                        summary TEXT NOT NULL,
                        state TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(project_id) REFERENCES projects(id),
                        FOREIGN KEY(session_id) REFERENCES sessions(id)
                    );

                    CREATE TABLE IF NOT EXISTS rollups (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        project_id INTEGER NOT NULL,
                        period_start TEXT NOT NULL,
                        period_end TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(project_id) REFERENCES projects(id)
                    );

                    CREATE TABLE IF NOT EXISTS adapter_offsets (
                        session_id INTEGER NOT NULL,
                        adapter TEXT NOT NULL,
                        log_path TEXT NOT NULL,
                        byte_offset INTEGER NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(session_id, adapter, log_path),
                        FOREIGN KEY(session_id) REFERENCES sessions(id)
                    );

                    CREATE TABLE IF NOT EXISTS source_status (
                        session_id INTEGER NOT NULL,
                        source TEXT NOT NULL,
                        status TEXT NOT NULL,
                        detail TEXT,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(session_id, source),
                        FOREIGN KEY(session_id) REFERENCES sessions(id)
                    );

                    CREATE TABLE IF NOT EXISTS features (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS file_state (
                        path TEXT PRIMARY KEY,
                        current_hash TEXT NOT NULL,
                        baseline_hash TEXT NOT NULL,
                        last_event_id INTEGER,
                        is_clean INTEGER NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS file_hash_history (
                        path TEXT NOT NULL,
                        hash TEXT NOT NULL,
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        PRIMARY KEY(path, hash)
                    );

                    CREATE INDEX IF NOT EXISTS idx_events_session_created
                        ON events(session_id, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_events_dedupe_hash
                        ON events(dedupe_hash, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_events_type_created
                        ON events(event_type, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_events_revert_summary
                        ON events(event_type, summarized_at, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_file_state_clean
                        ON file_state(is_clean, updated_at DESC);
                    """
                )
                # Forward-compatible migration for older DBs.
                event_columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(events)").fetchall()
                }
                if "before_hash" not in event_columns:
                    conn.execute("ALTER TABLE events ADD COLUMN before_hash TEXT")
                if "after_hash" not in event_columns:
                    conn.execute("ALTER TABLE events ADD COLUMN after_hash TEXT")
                if "reverted_event_id" not in event_columns:
                    conn.execute("ALTER TABLE events ADD COLUMN reverted_event_id INTEGER")
                if "reverted_by_event_id" not in event_columns:
                    conn.execute("ALTER TABLE events ADD COLUMN reverted_by_event_id INTEGER")
                if "is_effective" not in event_columns:
                    conn.execute("ALTER TABLE events ADD COLUMN is_effective INTEGER NOT NULL DEFAULT 1")
                if "summarized_at" not in event_columns:
                    conn.execute("ALTER TABLE events ADD COLUMN summarized_at TEXT")

                conn.execute(
                    """
                    INSERT INTO projects(path, display_name, recording_state, created_at, updated_at,
                                         last_updated_at, deleted_at, storage_cap_bytes, storage_used_bytes)
                    VALUES (?, NULL, 'stopped', ?, ?, ?, NULL, ?, 0)
                    ON CONFLICT(path) DO NOTHING
                    """,
                    (str(self.project_path), now, now, now, DEFAULT_CAP_BYTES),
                )

        self._execute_retry(_init)

    def set_project_metadata(self, display_name: str | None, recording_state: str) -> None:
        now = utc_now()
        display = display_name.strip() if display_name else None

        def _write():
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE projects
                    SET display_name = COALESCE(?, display_name),
                        recording_state = ?,
                        updated_at = ?
                    WHERE path = ?
                    """,
                    (display, recording_state, now, str(self.project_path)),
                )

        self._execute_retry(_write)

    def set_project_deleted(self, deleted: bool) -> None:
        now = utc_now()
        deleted_at = now if deleted else None

        def _write():
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE projects
                    SET deleted_at = ?,
                        recording_state = CASE WHEN ? IS NULL THEN recording_state ELSE 'stopped' END,
                        updated_at = ?
                    WHERE path = ?
                    """,
                    (deleted_at, deleted_at, now, str(self.project_path)),
                )

        self._execute_retry(_write)

    def update_storage(self, used_bytes: int) -> None:
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
                    (int(used_bytes), now, str(self.project_path)),
                )

        self._execute_retry(_write)

    def get_project_row(self):
        def _read():
            with self._connect() as conn:
                return conn.execute("SELECT * FROM projects WHERE path = ?", (str(self.project_path),)).fetchone()

        return self._execute_retry(_read)

    def get_project_id(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT id FROM projects WHERE path = ?", (str(self.project_path),)).fetchone()
        if row is None:
            raise RuntimeError("project row missing")
        return int(row["id"])

    def create_session(self, agent: str, external_session_ref: str | None = None) -> int:
        now = utc_now()

        def _create():
            with self._connect() as conn:
                project_id = self.get_project_id(conn)
                cursor = conn.execute(
                    """
                    INSERT INTO sessions(project_id, agent, started_at, stopped_at, state, external_session_ref, last_updated_at)
                    VALUES (?, ?, ?, NULL, 'running', ?, ?)
                    """,
                    (project_id, agent, now, external_session_ref, now),
                )
                session_id = int(cursor.lastrowid)
                conn.execute(
                    """
                    UPDATE projects
                    SET recording_state = 'recording',
                        last_updated_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, project_id),
                )
                return session_id

        return self._execute_retry(_create)

    def get_session(self, session_id: int):
        def _read():
            with self._connect() as conn:
                return conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()

        return self._execute_retry(_read)

    def get_active_session(self):
        def _read():
            with self._connect() as conn:
                return conn.execute(
                    """
                    SELECT * FROM sessions
                    WHERE state = 'running'
                    ORDER BY started_at DESC
                    LIMIT 1
                    """
                ).fetchone()

        return self._execute_retry(_read)

    def set_session_state(self, session_id: int, state: str) -> None:
        now = utc_now()
        stop_at = now if state == "stopped" else None

        def _write():
            with self._connect() as conn:
                if stop_at:
                    conn.execute(
                        """
                        UPDATE sessions
                        SET state = ?, stopped_at = ?, last_updated_at = ?
                        WHERE id = ?
                        """,
                        (state, stop_at, now, session_id),
                    )
                    conn.execute(
                        """
                        UPDATE projects
                        SET recording_state = 'stopped', updated_at = ?, last_updated_at = ?
                        WHERE path = ?
                        """,
                        (now, now, str(self.project_path)),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE sessions
                        SET state = ?, last_updated_at = ?
                        WHERE id = ?
                        """,
                        (state, now, session_id),
                    )
                    if state == "stopping":
                        conn.execute(
                            """
                            UPDATE projects
                            SET recording_state = 'stopping', updated_at = ?
                            WHERE path = ?
                            """,
                            (now, str(self.project_path)),
                        )

        self._execute_retry(_write)

    def get_session_state(self, session_id: int) -> str | None:
        def _read():
            with self._connect() as conn:
                row = conn.execute("SELECT state FROM sessions WHERE id = ?", (session_id,)).fetchone()
                return row["state"] if row else None

        return self._execute_retry(_read)

    def set_feature(self, key: str, value: str) -> None:
        now = utc_now()

        def _write():
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO features(key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (key, value, now),
                )

        self._execute_retry(_write)

    def get_adapter_offset(self, session_id: int, adapter: str, log_path: str) -> int:
        def _read():
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT byte_offset FROM adapter_offsets
                    WHERE session_id = ? AND adapter = ? AND log_path = ?
                    """,
                    (session_id, adapter, log_path),
                ).fetchone()
                return int(row["byte_offset"]) if row else 0

        return self._execute_retry(_read)

    def set_adapter_offset(self, session_id: int, adapter: str, log_path: str, offset: int) -> None:
        now = utc_now()

        def _write():
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO adapter_offsets(session_id, adapter, log_path, byte_offset, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(session_id, adapter, log_path)
                    DO UPDATE SET byte_offset = excluded.byte_offset, updated_at = excluded.updated_at
                    """,
                    (session_id, adapter, log_path, int(offset), now),
                )

        self._execute_retry(_write)

    def update_source_status(self, session_id: int, source: str, status: str, detail: str | None) -> None:
        now = utc_now()

        def _write():
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO source_status(session_id, source, status, detail, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(session_id, source)
                    DO UPDATE SET status = excluded.status, detail = excluded.detail, updated_at = excluded.updated_at
                    """,
                    (session_id, source, status, detail, now),
                )

        self._execute_retry(_write)

    def initialize_file_state(self, snapshot_hashes: dict[str, str]) -> None:
        now = utc_now()

        def _write():
            with self._connect() as conn:
                for path, file_hash in snapshot_hashes.items():
                    conn.execute(
                        """
                        INSERT INTO file_state(path, current_hash, baseline_hash, last_event_id, is_clean, updated_at)
                        VALUES (?, ?, ?, NULL, 1, ?)
                        ON CONFLICT(path) DO NOTHING
                        """,
                        (path, file_hash, file_hash, now),
                    )
                    conn.execute(
                        """
                        INSERT INTO file_hash_history(path, hash, first_seen_at, last_seen_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(path, hash)
                        DO UPDATE SET last_seen_at = excluded.last_seen_at
                        """,
                        (path, file_hash, now, now),
                    )

        self._execute_retry(_write)

    def _upsert_hash_history(
        self,
        conn: sqlite3.Connection,
        path: str,
        file_hash: str,
        now: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO file_hash_history(path, hash, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(path, hash)
            DO UPDATE SET last_seen_at = excluded.last_seen_at
            """,
            (path, file_hash, now, now),
        )

    def _is_seen_hash(self, conn: sqlite3.Connection, path: str, file_hash: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM file_hash_history WHERE path = ? AND hash = ? LIMIT 1",
            (path, file_hash),
        ).fetchone()
        return row is not None

    def _append_event_log(self, payload: dict) -> None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target = self.logs_path / f"events-{day}.jsonl"
        line = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def _storage_usage(self) -> int:
        return directory_size_bytes(self.memory_root)

    def _project_cap(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT storage_cap_bytes FROM projects WHERE path = ?",
            (str(self.project_path),),
        ).fetchone()
        if row is None:
            return DEFAULT_CAP_BYTES
        return int(row["storage_cap_bytes"] or DEFAULT_CAP_BYTES)

    def compact(self, conn: sqlite3.Connection) -> None:
        threshold = datetime.now(timezone.utc) - timedelta(days=1)
        threshold_iso = threshold.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        placeholders = ",".join("?" for _ in HIGH_VALUE_EVENT_TYPES)
        rows = conn.execute(
            f"""
            SELECT id, event_type, created_at
            FROM events
            WHERE event_type NOT IN ({placeholders})
              AND created_at < ?
            ORDER BY created_at
            LIMIT 3000
            """,
            (*HIGH_VALUE_EVENT_TYPES, threshold_iso),
        ).fetchall()
        if not rows:
            return

        counts = Counter(row["event_type"] for row in rows)
        period_start = rows[0]["created_at"]
        period_end = rows[-1]["created_at"]
        counts_summary = ", ".join(f"{k}:{v}" for k, v in sorted(counts.items()))
        summary = f"Compacted {len(rows)} low-value events ({counts_summary})."
        project_id = self.get_project_id(conn)
        now = utc_now()
        conn.execute(
            """
            INSERT INTO rollups(project_id, period_start, period_end, summary, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (project_id, period_start, period_end, summary, now),
        )
        ids = [row["id"] for row in rows]
        placeholder_ids = ",".join("?" for _ in ids)
        conn.execute(f"DELETE FROM events WHERE id IN ({placeholder_ids})", ids)

    def _enforce_quota(self, conn: sqlite3.Connection) -> None:
        cap = self._project_cap(conn)
        used = self._storage_usage()
        if used >= int(cap * COMPACTION_THRESHOLD_RATIO):
            self.compact(conn)
            conn.commit()
            conn.execute("VACUUM")
            used = self._storage_usage()
        conn.execute(
            """
            UPDATE projects
            SET storage_used_bytes = ?, updated_at = ?
            WHERE path = ?
            """,
            (used, utc_now(), str(self.project_path)),
        )
        if used >= cap:
            raise StorageCapError(
                f"Project storage cap exceeded ({used} bytes >= {cap} bytes)."
            )

    def _insert_event_with_conn(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: int,
        event_type: str,
        summary: str,
        files_touched: list[str] | None,
        source: str,
        now: str,
        tool_name: str | None = None,
        tool_purpose: str | None = None,
        tool_result: str | None = None,
        decision_summary: str | None = None,
        before_hash: str | None = None,
        after_hash: str | None = None,
        reverted_event_id: int | None = None,
        is_effective: int = 1,
    ) -> int:
        raw_type = (event_type or "task_status").strip()
        safe_type = raw_type if raw_type in EVENT_TYPES else "task_status"
        safe_summary = normalize_summary(summary, SUMMARY_MAX_CHARS)
        if not safe_summary:
            raise ValueError("summary cannot be empty")

        files: list[str] = []
        if files_touched:
            sanitized = set()
            for item in files_touched:
                raw = str(item).strip()
                if not raw:
                    continue
                path_obj = Path(raw)
                if path_obj.is_absolute():
                    sanitized.add(path_obj.as_posix())
                    continue
                try:
                    resolved = normalize_path(self.project_path / raw)
                    rel = resolved.relative_to(self.project_path)
                    sanitized.add(rel.as_posix())
                except Exception:
                    sanitized.add(path_obj.as_posix())
            files = sorted(sanitized)
        files_json = json.dumps(files, separators=(",", ":"), ensure_ascii=True)
        dedupe_basis = (
            f"{safe_type}|{safe_summary.lower()}|{','.join(files)}|"
            f"{before_hash or ''}|{after_hash or ''}|{reverted_event_id or ''}|{is_effective}"
        )
        dedupe_hash = hashlib.sha256(dedupe_basis.encode("utf-8")).hexdigest()
        now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
        project_id = self.get_project_id(conn)
        existing = conn.execute(
            """
            SELECT id, created_at
            FROM events
            WHERE session_id = ? AND dedupe_hash = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (session_id, dedupe_hash),
        ).fetchone()
        if existing:
            created = datetime.fromisoformat(existing["created_at"].replace("Z", "+00:00"))
            if (now_dt - created).total_seconds() <= DEDUPE_WINDOW_SECONDS:
                conn.execute(
                    "UPDATE events SET updated_at = ? WHERE id = ?",
                    (now, existing["id"]),
                )
                conn.execute(
                    "UPDATE sessions SET last_updated_at = ? WHERE id = ?",
                    (now, session_id),
                )
                conn.execute(
                    """
                    UPDATE projects
                    SET last_updated_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, project_id),
                )
                return int(existing["id"])

        cursor = conn.execute(
            """
            INSERT INTO events(project_id, session_id, event_type, summary, files_touched_json,
                               before_hash, after_hash, reverted_event_id, reverted_by_event_id,
                               is_effective, source, created_at, updated_at, dedupe_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                session_id,
                safe_type,
                safe_summary,
                files_json,
                before_hash,
                after_hash,
                reverted_event_id,
                int(is_effective),
                source,
                now,
                now,
                dedupe_hash,
            ),
        )
        event_id = int(cursor.lastrowid)
        if tool_name:
            conn.execute(
                """
                INSERT INTO tool_usage(project_id, session_id, event_id, tool_name, purpose, result, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (project_id, session_id, event_id, tool_name, tool_purpose, tool_result, now),
            )
        if safe_type == "decision_made" or decision_summary:
            conn.execute(
                """
                INSERT INTO decisions(project_id, session_id, event_id, summary, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (project_id, session_id, event_id, decision_summary or safe_summary, now),
            )
        conn.execute(
            "UPDATE sessions SET last_updated_at = ? WHERE id = ?",
            (now, session_id),
        )
        conn.execute(
            """
            UPDATE projects
            SET last_updated_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, now, project_id),
        )
        payload = {
            "event_id": event_id,
            "session_id": session_id,
            "event_type": safe_type,
            "summary": safe_summary,
            "files_touched": files,
            "before_hash": before_hash,
            "after_hash": after_hash,
            "reverted_event_id": reverted_event_id,
            "source": source,
            "created_at": now,
        }
        self._append_event_log(payload)
        used = self._storage_usage()
        conn.execute(
            """
            UPDATE projects
            SET storage_used_bytes = ?, updated_at = ?
            WHERE id = ?
            """,
            (used, now, project_id),
        )
        return event_id

    def insert_event(
        self,
        session_id: int,
        event_type: str,
        summary: str,
        files_touched: list[str] | None,
        source: str,
        tool_name: str | None = None,
        tool_purpose: str | None = None,
        tool_result: str | None = None,
        decision_summary: str | None = None,
        before_hash: str | None = None,
        after_hash: str | None = None,
        reverted_event_id: int | None = None,
        is_effective: int = 1,
    ) -> int:
        now = utc_now()

        def _insert():
            with self._connect() as conn:
                self._enforce_quota(conn)
                return self._insert_event_with_conn(
                    conn,
                    session_id=session_id,
                    event_type=event_type,
                    summary=summary,
                    files_touched=files_touched,
                    source=source,
                    now=now,
                    tool_name=tool_name,
                    tool_purpose=tool_purpose,
                    tool_result=tool_result,
                    decision_summary=decision_summary,
                    before_hash=before_hash,
                    after_hash=after_hash,
                    reverted_event_id=reverted_event_id,
                    is_effective=is_effective,
                )

        return self._execute_retry(_insert)

    def record_file_transition(
        self,
        session_id: int,
        source: str,
        path: str,
        after_hash: str,
    ) -> int | None:
        file_path = str(Path(path).as_posix())
        safe_after_hash = after_hash or DELETED_FILE_HASH
        now = utc_now()

        def _insert():
            with self._connect() as conn:
                self._enforce_quota(conn)
                state = conn.execute(
                    """
                    SELECT current_hash, baseline_hash, last_event_id
                    FROM file_state
                    WHERE path = ?
                    """,
                    (file_path,),
                ).fetchone()
                if state:
                    before_hash = state["current_hash"]
                    baseline_hash = state["baseline_hash"]
                    previous_event_id = int(state["last_event_id"]) if state["last_event_id"] else None
                else:
                    before_hash = DELETED_FILE_HASH
                    baseline_hash = DELETED_FILE_HASH
                    previous_event_id = None

                if before_hash == safe_after_hash:
                    return None

                seen_hash_before = self._is_seen_hash(conn, file_path, safe_after_hash)
                is_revert = bool(seen_hash_before)
                is_clean = int(safe_after_hash == baseline_hash)
                if is_revert:
                    if is_clean:
                        summary = f"Last changes were reverted for {file_path}; file returned to baseline."
                    else:
                        summary = f"Last changes were reverted for {file_path}; file returned to a previous state."
                    event_type = "revert"
                else:
                    summary = f"File changed: {file_path}."
                    event_type = "code_change"

                event_id = self._insert_event_with_conn(
                    conn,
                    session_id=session_id,
                    event_type=event_type,
                    summary=summary,
                    files_touched=[file_path],
                    source=source,
                    now=now,
                    before_hash=before_hash,
                    after_hash=safe_after_hash,
                    reverted_event_id=previous_event_id if is_revert else None,
                    is_effective=1,
                )

                if previous_event_id:
                    conn.execute(
                        """
                        UPDATE events
                        SET is_effective = 0,
                            reverted_by_event_id = CASE WHEN ? THEN ? ELSE reverted_by_event_id END
                        WHERE id = ?
                        """,
                        (1 if is_revert else 0, event_id if is_revert else None, previous_event_id),
                    )

                conn.execute(
                    """
                    INSERT INTO file_state(path, current_hash, baseline_hash, last_event_id, is_clean, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        current_hash = excluded.current_hash,
                        last_event_id = excluded.last_event_id,
                        is_clean = excluded.is_clean,
                        updated_at = excluded.updated_at
                    """,
                    (file_path, safe_after_hash, baseline_hash, event_id, is_clean, now),
                )
                self._upsert_hash_history(conn, file_path, safe_after_hash, now)
                return event_id

        return self._execute_retry(_insert)

    def status_snapshot(self, recent_limit: int = 5) -> dict:
        def _read():
            with self._connect() as conn:
                project = conn.execute(
                    "SELECT * FROM projects WHERE path = ?",
                    (str(self.project_path),),
                ).fetchone()
                active_session = conn.execute(
                    """
                    SELECT * FROM sessions
                    WHERE state = 'running'
                    ORDER BY started_at DESC
                    LIMIT 1
                    """
                ).fetchone()
                latest_session_id = None
                if active_session:
                    latest_session_id = int(active_session["id"])
                else:
                    last_session = conn.execute(
                        "SELECT id FROM sessions ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    if last_session:
                        latest_session_id = int(last_session["id"])

                source_rows = []
                if latest_session_id is not None:
                    source_rows = conn.execute(
                        """
                        SELECT source, status, detail, updated_at
                        FROM source_status
                        WHERE session_id = ?
                        ORDER BY source
                        """,
                        (latest_session_id,),
                    ).fetchall()
                events = conn.execute(
                    """
                    SELECT event_type, summary, source, created_at, is_effective
                    FROM events
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (recent_limit,),
                ).fetchall()
                last_revert = conn.execute(
                    """
                    SELECT created_at, summary
                    FROM events
                    WHERE event_type = 'revert'
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
                changed_files = conn.execute(
                    "SELECT COUNT(*) AS c FROM file_state WHERE is_clean = 0"
                ).fetchone()
                return {
                    "project": project,
                    "active_session": active_session,
                    "source_status": source_rows,
                    "events": events,
                    "last_revert": last_revert,
                    "effective_changed_files": int(changed_files["c"]) if changed_files else 0,
                    "storage_used_bytes": self._storage_usage(),
                }

        return self._execute_retry(_read)
